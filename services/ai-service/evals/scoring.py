"""Deterministic checks for RCA / runbook output against a case's expectations.

Expectation keys (all optional):
  component       expected service; must be the top-ranked hypothesis'
                  affected component (primary cause text only if no hypotheses)
  cause_any       at least one keyword must appear in primary cause + summary
  cause_none      none of these keywords may appear in the primary cause
                  (catches confidently wrong root causes)
  min_confidence  RCA confidence must be >= this
  max_confidence  RCA confidence must be <= this (calibration on thin evidence)
  runbook_any     at least one keyword must appear in the runbook text
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.schemas.rca import RCAResult
from app.schemas.runbook import RunbookResult

KNOWN_EXPECTATIONS = {
    "component",
    "cause_any",
    "cause_none",
    "min_confidence",
    "max_confidence",
    "runbook_any",
}


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class CaseScore:
    case: str
    checks: List[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def score(self) -> float:
        return sum(c.passed for c in self.checks) / len(self.checks) if self.checks else 1.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "case": self.case,
            "passed": self.passed,
            "score": round(self.score, 3),
            "checks": [c.__dict__ for c in self.checks],
        }


def _mentions(text: str, keywords: List[str]) -> List[str]:
    lowered = text.lower()
    return [k for k in keywords if k.lower() in lowered]


def runbook_text(runbook: RunbookResult) -> str:
    parts = [runbook.title, runbook.summary]
    for step in runbook.steps:
        parts += [step.action, step.command_or_check or "", step.expected_result or ""]
    parts += [a.action for a in runbook.remediation_actions]
    return " ".join(p for p in parts if p)


def score_case(
    case: str,
    expect: Dict[str, Any],
    rca: RCAResult,
    runbook: Optional[RunbookResult] = None,
) -> CaseScore:
    unknown = set(expect) - KNOWN_EXPECTATIONS
    if unknown:
        raise ValueError(f"Case {case}: unknown expectation keys {sorted(unknown)}")

    score = CaseScore(case=case)
    cause_text = f"{rca.primary_cause} {rca.summary}"

    if "component" in expect:
        want = expect["component"].lower()
        top = sorted(rca.hypotheses, key=lambda h: h.rank)[0] if rca.hypotheses else None
        if top is not None and top.affected_component:
            # Blame must land on the component; merely mentioning it is not enough.
            got = top.affected_component
            ok = want in got.lower()
        else:
            got = f"(no hypothesis component) primary cause: {rca.primary_cause}"
            ok = want in rca.primary_cause.lower()
        score.checks.append(Check("component", ok, f"expected {want!r}; got {got!r}"))

    if "cause_any" in expect:
        hits = _mentions(cause_text, expect["cause_any"])
        score.checks.append(Check(
            "cause_any", bool(hits),
            f"matched {hits}" if hits else f"none of {expect['cause_any']} in primary cause/summary",
        ))

    if "cause_none" in expect:
        hits = _mentions(rca.primary_cause, expect["cause_none"])
        score.checks.append(Check(
            "cause_none", not hits,
            f"distractors named: {hits}" if hits else "no distractors named",
        ))

    if "min_confidence" in expect:
        ok = rca.confidence >= expect["min_confidence"]
        score.checks.append(Check(
            "min_confidence", ok, f"confidence={rca.confidence} (min {expect['min_confidence']})"
        ))

    if "max_confidence" in expect:
        ok = rca.confidence <= expect["max_confidence"]
        score.checks.append(Check(
            "max_confidence", ok, f"confidence={rca.confidence} (max {expect['max_confidence']})"
        ))

    if "runbook_any" in expect and runbook is not None:
        hits = _mentions(runbook_text(runbook), expect["runbook_any"])
        score.checks.append(Check(
            "runbook_any", bool(hits),
            f"matched {hits}" if hits else f"none of {expect['runbook_any']} in runbook",
        ))

    return score
