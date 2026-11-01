from datetime import datetime, timezone
from typing import List

import pytest

from app.agent.graph import run_investigation
from app.providers.base import AIProviderError
from app.providers.mock_provider import MockAIProvider
from app.providers.resilient_provider import ResilientAIProvider
from app.schemas.agent import AgentAction, AgentInvestigateRequest
from app.schemas.incident import BuildContextRequest
from app.schemas.telemetry import LogRecord, Resource, SeverityLevel
from app.services import incident_store
from app.services.context_builder import build_context


class ScriptedProvider(MockAIProvider):
    """Returns scripted AgentActions; delegates RCA/runbook to the mock."""

    name = "scripted"

    def __init__(self, actions: List[AgentAction]):
        self.actions = list(actions)
        self.prompts: List[str] = []

    async def generate_structured(self, prompt, response_model, system_instruction=None):
        if response_model is AgentAction:
            self.prompts.append(prompt)
            return self.actions.pop(0)
        return await super().generate_structured(prompt, response_model, system_instruction)


def _seed_incident() -> str:
    incident_store.clear()
    now = datetime.now(timezone.utc)
    incident = build_context(BuildContextRequest(
        title="Payment timeout",
        affected_services=["payment-service"],
        logs=[LogRecord(
            timestamp=now,
            severity=SeverityLevel.ERROR,
            body="Payment FAILED gateway timeout",
            resource=Resource(service_name="payment-service"),
        )],
    ))
    return incident.incident_id


def act(tool, **kw) -> AgentAction:
    return AgentAction(thought=f"use {tool}", tool=tool, **kw)


@pytest.mark.asyncio
async def test_planner_runs_model_chosen_tools_and_finishes():
    iid = _seed_incident()
    provider = ScriptedProvider([
        act("list_incidents"),
        act("get_incident", incident_id=iid),
        act("search_evidence", query="timeout"),
        act("run_rca"),
        act("finish", final_answer="Gateway timeout in payment-service caused the failures."),
    ])
    resp = await run_investigation(
        AgentInvestigateRequest(question="Why are payments failing?", mode="planner"), provider
    )

    assert resp.mode == "planner"
    assert resp.provider == "scripted"
    assert resp.incident_id == iid
    assert [s.tool for s in resp.steps] == ["list_incidents", "get_incident", "search_evidence", "run_rca"]
    assert all(s.success for s in resp.steps)
    assert resp.steps[0].thought == "use list_incidents"
    assert resp.summary == "Gateway timeout in payment-service caused the failures."
    assert resp.findings["evidence"]["logs"][0]["body"].startswith("Payment FAILED")
    assert resp.findings["rca"]["primary_cause"]
    # Earlier tool results are fed back to the model on later steps
    assert "Payment FAILED gateway timeout" in provider.prompts[-1]


@pytest.mark.asyncio
async def test_planner_respects_max_steps():
    iid = _seed_incident()
    provider = ScriptedProvider([act("search_evidence", query=q) for q in ("a", "b", "c")])
    resp = await run_investigation(
        AgentInvestigateRequest(incident_id=iid, max_steps=2, mode="planner"), provider
    )
    assert len(resp.steps) == 2
    assert len(provider.actions) == 1
    assert "stopped after 2 step(s)" in resp.summary


@pytest.mark.asyncio
async def test_planner_dedupes_repeat_calls_and_blocks_disabled_tools():
    iid = _seed_incident()
    provider = ScriptedProvider([
        act("correlate"),
        act("correlate"),
        act("run_rca"),
        act("finish", final_answer="done"),
    ])
    resp = await run_investigation(
        AgentInvestigateRequest(incident_id=iid, run_rca=False, mode="planner"), provider
    )
    assert "duplicate call" in resp.steps[1].output_summary
    assert resp.steps[2].success is False
    assert "not available" in resp.steps[2].output_summary
    assert "rca" not in resp.findings
    assert "run_rca" not in provider.prompts[0]


class _Down(MockAIProvider):
    name = "claude-down"

    async def generate_structured(self, prompt, response_model, system_instruction=None):
        raise AIProviderError("outage")


@pytest.mark.asyncio
async def test_planner_falls_back_to_fixed_pipeline_when_model_unavailable():
    iid = _seed_incident()
    provider = ResilientAIProvider(_Down(), max_retries=0, retry_backoff_seconds=0)
    resp = await run_investigation(
        AgentInvestigateRequest(incident_id=iid, mode="auto"), provider
    )
    assert resp.mode == "fixed"
    assert any(s.tool == "run_rca" for s in resp.steps)


@pytest.mark.asyncio
async def test_auto_mode_uses_fixed_pipeline_with_mock():
    iid = _seed_incident()
    resp = await run_investigation(AgentInvestigateRequest(incident_id=iid), MockAIProvider())
    assert resp.mode == "fixed"
