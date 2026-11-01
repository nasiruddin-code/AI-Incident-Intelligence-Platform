import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.providers.factory import get_ai_provider
from app.providers.mock_provider import MockAIProvider
from app.schemas.agent import AgentAction
from app.schemas.incident import BuildContextRequest
from app.schemas.telemetry import LogRecord, Resource, SeverityLevel
from app.services import incident_store
from app.services.context_builder import build_context

URL = "/api/v1/agent/investigate/stream"


def parse_sse(text: str):
    events = []
    for block in text.split("\n\n"):
        lines = [l for l in block.splitlines() if l and not l.startswith(":")]
        if not lines:
            continue
        fields = dict(l.split(": ", 1) for l in lines)
        events.append((fields["event"], json.loads(fields["data"])))
    return events


def stream(client, body):
    with client.stream("POST", URL, json=body) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        return parse_sse("".join(r.iter_text()))


@pytest.fixture()
def incident_id():
    incident_store.clear()
    return build_context(BuildContextRequest(
        title="Payment timeout",
        affected_services=["payment-service"],
        logs=[LogRecord(timestamp=datetime.now(timezone.utc), severity=SeverityLevel.ERROR,
                        body="Payment FAILED gateway timeout", resource=Resource(service_name="payment-service"))],
    )).incident_id


def test_fixed_pipeline_streams_steps_then_result(client, incident_id):
    events = stream(client, {"incident_id": incident_id, "question": "why?"})
    names = [e for e, _ in events]

    assert names[0] == "mode" and events[0][1]["mode"] == "fixed"
    assert names[-1] == "result"
    steps = [d for e, d in events if e == "step"]
    assert [s["tool"] for s in steps][:2] == ["get_incident", "search_evidence"]
    assert {"correlate", "run_rca", "generate_runbook"} <= {s["tool"] for s in steps}

    result = events[-1][1]
    assert result["incident_id"] == incident_id and result["summary"]
    assert len(result["steps"]) == len(steps)  # every recorded step was streamed


def test_streamed_result_matches_plain_endpoint(client, incident_id):
    plain = client.post("/api/v1/agent/investigate", json={"incident_id": incident_id}).json()
    streamed = stream(client, {"incident_id": incident_id})[-1][1]
    assert [s["tool"] for s in streamed["steps"]] == [s["tool"] for s in plain["steps"]]
    assert streamed["mode"] == plain["mode"]


class Scripted(MockAIProvider):
    name = "scripted"

    def __init__(self, actions):
        self.actions = list(actions)

    async def generate_structured(self, prompt, response_model, system_instruction=None):
        if response_model is AgentAction:
            return self.actions.pop(0)
        return await super().generate_structured(prompt, response_model, system_instruction)


def test_planner_streams_planning_and_thoughts(client, incident_id):
    provider = Scripted([
        AgentAction(thought="Check the incident first", tool="get_incident", incident_id=incident_id),
        AgentAction(thought="Look for timeouts", tool="search_evidence", query="timeout"),
        AgentAction(thought="Enough evidence", tool="finish", final_answer="Gateway timeout."),
    ])
    client.app.dependency_overrides[get_ai_provider] = lambda: provider
    events = stream(client, {"incident_id": incident_id, "mode": "planner"})

    names = [e for e, _ in events]
    assert names == ["mode", "planning", "step", "planning", "step", "planning", "result"]
    steps = [d for e, d in events if e == "step"]
    assert [s["thought"] for s in steps] == ["Check the incident first", "Look for timeouts"]
    assert events[1][1] == {"step": 1, "max_steps": 8}
    assert events[-1][1]["summary"] == "Gateway timeout." and events[-1][1]["mode"] == "planner"


def test_failure_is_sent_as_error_event(client, incident_id):
    async def boom(*args, **kwargs):
        raise RuntimeError("store unavailable")

    with patch("app.api.v1.endpoints.agent.run_investigation", boom):
        events = stream(client, {"incident_id": incident_id})
    assert events == [("error", {"detail": "store unavailable"})]


def test_stream_requires_ai_scope(client, monkeypatch, incident_id):
    from app.core.config import APIKeySpec, get_settings

    monkeypatch.setattr(get_settings(), "ENABLE_AUTH", True)
    monkeypatch.setattr(get_settings(), "API_KEYS", {"viewer": APIKeySpec(key="viewer-key-0123456789", scopes=["read"])})
    r = client.post(URL, json={"incident_id": incident_id}, headers={"X-API-Key": "viewer-key-0123456789"})
    assert r.status_code == 403
