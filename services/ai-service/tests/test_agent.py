from datetime import datetime, timezone

from app.services import incident_store


def _now():
    return datetime.now(timezone.utc).isoformat()


def _resource(name):
    return {
        "service_name": name,
        "service_version": "0.1.0",
        "environment": "test",
        "attributes": {},
    }


def test_agent_investigate(client):
    incident_store.clear()
    now = _now()
    r = client.post(
        "/api/v1/incidents/context",
        json={
            "title": "Payment timeout",
            "severity": "ERROR",
            "affected_services": ["payment-service"],
            "logs": [
                {
                    "timestamp": now,
                    "severity": "ERROR",
                    "body": "Payment FAILED gateway timeout",
                    "attributes": {},
                    "resource": _resource("payment-service"),
                    "trace_id": "t-agent-1",
                    "span_id": "s1",
                }
            ],
            "traces": [
                {
                    "trace_id": "t-agent-1",
                    "span_id": "s1",
                    "name": "payment.process",
                    "start_time": now,
                    "end_time": now,
                    "status": "ERROR",
                    "attributes": {},
                    "resource": _resource("payment-service"),
                }
            ],
            "metrics": [
                {
                    "name": "payments_total",
                    "type": "COUNTER",
                    "value": 5,
                    "timestamp": now,
                    "attributes": {"status": "failed"},
                    "resource": _resource("payment-service"),
                }
            ],
        },
    )
    assert r.status_code == 201
    iid = r.json()["incident_id"]

    resp = client.post(
        "/api/v1/agent/investigate",
        json={
            "incident_id": iid,
            "question": "What caused the payment timeout?",
            "run_correlation": True,
            "run_rca": True,
            "run_runbook": True,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["incident_id"] == iid
    assert len(data["steps"]) >= 3
    assert data["summary"]
    assert "rca" in data["findings"] or any(s["tool"] == "run_rca" for s in data["steps"])


def test_agent_no_incidents(client):
    incident_store.clear()
    resp = client.post("/api/v1/agent/investigate", json={})
    assert resp.status_code == 200
    data = resp.json()
    assert "No incidents" in data["summary"] or data["incident_id"] is None
