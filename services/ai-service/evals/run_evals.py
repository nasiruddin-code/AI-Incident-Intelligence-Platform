"""Run the golden-incident eval set against the configured AI provider.

    cd services/ai-service
    python -m evals.run_evals                     # all cases, provider from .env
    python -m evals.run_evals --case order_db_pool_exhaustion --repeat 3
    python -m evals.run_evals --json eval-report.json --min-pass-rate 0.8

Each case is built into an incident (in-memory store), correlated, then
RCA (and runbook unless --no-runbook) runs with the real prompts and the
configured provider. Outputs are scored by evals/scoring.py.

Exit codes: 0 pass rate >= --min-pass-rate, 1 below it, 2 run invalid
(an answer came from the mock fallback, so the model was not evaluated).

With a real provider this makes billed API calls: roughly 2 calls per case
per repeat. The response cache is disabled so every run measures the model.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

# Isolate eval runs: in-memory incidents and no response cache. Must be set
# before app settings are first loaded.
os.environ["DATABASE_URL"] = ""
os.environ["AI_CACHE_TTL_SECONDS"] = "0"

from app.core.ai_metrics import ai_metrics  # noqa: E402
from app.prompts.rca import RCA_PROMPT  # noqa: E402
from app.prompts.runbook import RUNBOOK_PROMPT  # noqa: E402
from app.providers.factory import get_ai_provider  # noqa: E402
from app.schemas.incident import BuildContextRequest  # noqa: E402
from app.services import incident_store  # noqa: E402
from app.services.context_builder import build_context  # noqa: E402
from app.services.correlation import correlate_incident  # noqa: E402
from app.services.rca_engine import RCAEngine  # noqa: E402
from app.services.runbook_engine import RunbookEngine  # noqa: E402
from evals.scoring import score_case  # noqa: E402

CASES_DIR = Path(__file__).parent / "cases"


def load_cases(cases_dir: Path = CASES_DIR, only: List[str] | None = None) -> List[Dict[str, Any]]:
    cases = []
    for path in sorted(cases_dir.glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        case.setdefault("name", path.stem)
        if only and case["name"] not in only:
            continue
        BuildContextRequest.model_validate(case["incident"])  # fail fast on bad case files
        cases.append(case)
    if only and len(cases) != len(set(only)):
        found = {c["name"] for c in cases}
        raise SystemExit(f"Unknown case(s): {sorted(set(only) - found)}")
    return cases


async def run_case(case: Dict[str, Any], provider, with_runbook: bool) -> Dict[str, Any]:
    incident = build_context(BuildContextRequest.model_validate(case["incident"]))
    correlation = correlate_incident(incident.incident_id)
    rca = await RCAEngine(provider).analyze(incident.incident_id, correlation=correlation)
    runbook = await RunbookEngine(provider).generate(incident.incident_id) if with_runbook else None

    score = score_case(case["name"], case["expect"], rca.rca, runbook.runbook if runbook else None)
    usages = [r.usage for r in (rca, runbook) if r is not None and r.usage is not None]
    return {
        **score.as_dict(),
        "provider": rca.provider,
        "degraded": rca.degraded or bool(runbook and runbook.degraded),
        "primary_cause": rca.rca.primary_cause,
        "confidence": rca.rca.confidence,
        "input_tokens": sum(u.input_tokens for u in usages),
        "output_tokens": sum(u.output_tokens for u in usages),
        "latency_ms": round(sum(u.latency_ms for u in usages), 1),
    }


async def run(cases: List[Dict[str, Any]], repeat: int, with_runbook: bool) -> Dict[str, Any]:
    provider = get_ai_provider()
    cost_before = sum(ai_metrics.cost_usd.values())
    results = []
    for case in cases:
        for i in range(repeat):
            incident_store.clear()
            result = await run_case(case, provider, with_runbook)
            result["repeat"] = i + 1
            results.append(result)

    passed = sum(r["passed"] for r in results)
    in_tok = sum(r["input_tokens"] for r in results)
    out_tok = sum(r["output_tokens"] for r in results)
    return {
        "provider": provider.name,
        "prompts": {"rca": RCA_PROMPT.name, "runbook": RUNBOOK_PROMPT.name if with_runbook else None},
        "runs": len(results),
        "passed": passed,
        "pass_rate": round(passed / len(results), 3) if results else 0.0,
        "mean_score": round(sum(r["score"] for r in results) / len(results), 3) if results else 0.0,
        "degraded_runs": sum(r["degraded"] for r in results),
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        # From AI_MODEL_PRICES; 0 for models without a price entry
        "estimated_cost_usd": round(sum(ai_metrics.cost_usd.values()) - cost_before, 4),
        "results": results,
    }


def print_report(report: Dict[str, Any]) -> None:
    print(f"\nProvider: {report['provider']}   prompts: {report['prompts']}")
    print(f"{'case':34} {'run':>3}  {'result':6} {'score':>5}  {'conf':>4}  failed checks")
    for r in report["results"]:
        failed = "; ".join(f"{c['name']}: {c['detail']}" for c in r["checks"] if not c["passed"])
        print(
            f"{r['case'][:34]:34} {r['repeat']:>3}  {'PASS' if r['passed'] else 'FAIL':6} "
            f"{r['score']:>5.2f}  {r['confidence']:>4.2f}  {failed}"
        )
    print(
        f"\nPassed {report['passed']}/{report['runs']} (pass rate {report['pass_rate']:.0%}, "
        f"mean check score {report['mean_score']:.2f}); "
        f"tokens in/out {report['input_tokens']}/{report['output_tokens']}, "
        f"estimated cost ${report['estimated_cost_usd']:.4f}"
    )
    if report["degraded_runs"]:
        print(f"WARNING: {report['degraded_runs']} run(s) answered by the mock fallback — not a model evaluation.")


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--case", action="append", help="Run only this case (repeatable)")
    parser.add_argument("--repeat", type=int, default=1, help="Runs per case, to see variance")
    parser.add_argument("--no-runbook", action="store_true", help="Score RCA only (half the calls)")
    parser.add_argument("--json", type=Path, help="Write the full report to this file")
    parser.add_argument("--min-pass-rate", type=float, default=0.8)
    args = parser.parse_args(argv)

    cases = load_cases(only=args.case)
    report = asyncio.run(run(cases, args.repeat, not args.no_runbook))
    print_report(report)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Report written to {args.json}")

    if report["degraded_runs"]:
        return 2
    return 0 if report["pass_rate"] >= args.min_pass_rate else 1


if __name__ == "__main__":
    sys.exit(main())
