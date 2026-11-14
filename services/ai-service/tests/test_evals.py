"""CI coverage for the golden-incident eval set (uses the mock provider: no API calls)."""
import pytest

from app.schemas.rca import RCAHypothesis, RCAResult
from evals.run_evals import load_cases, run
from evals.scoring import KNOWN_EXPECTATIONS, score_case


def _rca(cause, component, confidence=0.8, summary=""):
    return RCAResult(
        summary=summary or cause,
        primary_cause=cause,
        hypotheses=[RCAHypothesis(rank=1, cause=cause, confidence=confidence, affected_component=component)],
        confidence=confidence,
    )


def test_case_files_are_valid():
    cases = load_cases()
    assert len(cases) >= 6
    for case in cases:
        assert case["expect"], case["name"]
        assert set(case["expect"]) <= KNOWN_EXPECTATIONS, case["name"]


def test_scoring_accepts_correct_answer():
    expect = {"component": "order-service", "cause_any": ["pool"], "cause_none": ["gateway"], "min_confidence": 0.5}
    score = score_case("db", expect, _rca("DB connection pool exhausted in order-service", "order-service"))
    assert score.passed and score.score == 1.0


def test_scoring_rejects_mention_without_blame():
    expect = {"component": "order-service"}
    rca = _rca("Payment failures propagating to order-service", "payment-service")
    assert not score_case("db", expect, rca).passed


def test_scoring_catches_distractor_and_overconfidence():
    expect = {"cause_none": ["gateway"], "max_confidence": 0.6}
    score = score_case("x", expect, _rca("Payment gateway timeout", "payment-service", confidence=0.9))
    assert [c.passed for c in score.checks] == [False, False]


def test_scoring_rejects_unknown_expectation_keys():
    with pytest.raises(ValueError):
        score_case("x", {"typo_key": 1}, _rca("c", "s"))


@pytest.mark.asyncio
async def test_harness_runs_all_cases_and_discriminates():
    report = await run(load_cases(), repeat=1, with_runbook=True)

    assert report["runs"] == len(load_cases())
    assert report["prompts"] == {"rca": "rca_v2", "runbook": "runbook_v1"}
    assert report["degraded_runs"] == 0
    by_case = {r["case"]: r for r in report["results"]}
    # The keyword-matching mock gets the easy case right...
    assert by_case["payment_gateway_timeout"]["passed"]
    # ...and the eval catches it blaming the wrong service for a DB pool outage.
    db = by_case["order_db_pool_exhaustion"]
    assert not db["passed"]
    assert not next(c for c in db["checks"] if c["name"] == "component")["passed"]
