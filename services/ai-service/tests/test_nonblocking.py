"""Synchronous store/DB work must never run on the event loop thread."""
import asyncio
from unittest.mock import patch

import pytest

from app.services import incident_store

STORE_FUNCS = ("get", "save", "list_page", "list_summaries", "find_open_by_metadata")


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


@pytest.fixture()
def loop_calls():
    """Wrap store functions; record any call made from the event loop thread."""
    offenders = []
    originals = {name: getattr(incident_store, name) for name in STORE_FUNCS}

    def wrap(name):
        def wrapper(*args, **kwargs):
            if _on_event_loop():
                offenders.append(name)
            return originals[name](*args, **kwargs)
        return wrapper

    with patch.multiple(incident_store, **{name: wrap(name) for name in STORE_FUNCS}):
        yield offenders


def test_incident_api_and_ai_flows_keep_store_off_event_loop(client, loop_calls):
    incident_store.clear()
    iid = client.post("/api/v1/incidents", json={"title": "x", "severity": "ERROR"}).json()["incident_id"]
    client.get("/api/v1/incidents")
    client.get("/api/v1/incidents/summaries")
    client.get(f"/api/v1/incidents/{iid}")
    client.post("/api/v1/incidents/context", json={"incident_id": iid, "notes": "n"})
    client.post(f"/api/v1/incidents/{iid}/correlate")
    client.post(f"/api/v1/incidents/{iid}/rca")
    client.post(f"/api/v1/incidents/{iid}/runbook")
    client.patch(f"/api/v1/incidents/{iid}/status?new_status=INVESTIGATING")
    client.post("/api/v1/agent/investigate", json={"incident_id": iid})
    client.post("/api/v1/e2e/run", json={"check_observability": False})

    assert loop_calls == [], f"store called on the event loop: {sorted(set(loop_calls))}"
