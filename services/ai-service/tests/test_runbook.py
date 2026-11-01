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


def test_runbook_generation(client):
    incident_store.clear()
    now = _now()
    r = client.post(
        "/api/v1/incidents/context",
        json={
            "title": "Payment gateway timeout",
            "severity": "ERROR",
            "affected_services": ["order-service", "payment-service"],
            "logs": [
                {
                    "timestamp": now,
                    "severity": "ERROR",
                    "body": "Payment FAILED gateway timeout",
                    "attributes": {},
                    "resource": _resource("payment-service"),
                    "trace_id": "t1",
                    "span_id": "s1",
                }
            ],
            "traces": [
                {
                    "trace_id": "t1",
                    "span_id": "s1",
                    "name": "payment.process",
                    "start_time": now,
                    "end_time": now,
                    "status": "ERROR",
                    "attributes": {},
                    "resource": _resource("payment-service"),
                }
            ],
        },
    )
    assert r.status_code == 201
    iid = r.json()["incident_id"]

    # Optional: run RCA first so runbook sees primary cause
    client.post(f"/api/v1/incidents/{iid}/rca")

    rb = client.post(f"/api/v1/incidents/{iid}/runbook")
    assert rb.status_code == 200
    data = rb.json()
    assert data["incident_id"] == iid
    assert data["provider"] == "mock"
    assert data["runbook"]["title"]
    assert len(data["runbook"]["steps"]) >= 1
    assert data["runbook"]["steps"][0]["order"] == 1
    assert len(data["runbook"]["remediation_actions"]) >= 1
    assert 0 <= data["runbook"]["confidence"] <= 1
    assert data["runbook"]["verification_checks"]


def test_runbook_not_found(client):
    incident_store.clear()
    r = client.post("/api/v1/incidents/missing/runbook")
    assert r.status_code == 404
