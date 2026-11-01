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


def test_rca_on_incident(client):
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
                    "body": "Payment FAILED (injected) payment gateway timeout",
                    "attributes": {},
                    "resource": _resource("payment-service"),
                    "trace_id": "trace-rca-001",
                    "span_id": "span-1",
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
            "traces": [
                {
                    "trace_id": "trace-rca-001",
                    "span_id": "span-1",
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

    rca = client.post(f"/api/v1/incidents/{iid}/rca")
    assert rca.status_code == 200
    data = rca.json()
    assert data["incident_id"] == iid
    assert data["provider"] == "mock"
    assert data["rca"]["primary_cause"]
    assert data["rca"]["summary"]
    assert 0 <= data["rca"]["confidence"] <= 1
    assert len(data["rca"]["hypotheses"]) >= 1
    assert data["rca"]["hypotheses"][0]["rank"] == 1
    assert data["rca"]["recommended_next_steps"]


def test_rca_not_found(client):
    incident_store.clear()
    r = client.post("/api/v1/incidents/missing/rca")
    assert r.status_code == 404
