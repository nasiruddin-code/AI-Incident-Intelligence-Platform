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


def test_correlate_incident(client):
    incident_store.clear()
    now = _now()
    # Build context with linked evidence
    r = client.post(
        "/api/v1/incidents/context",
        json={
            "title": "Payment cascade",
            "severity": "WARN",
            "affected_services": ["order-service", "payment-service"],
            "logs": [
                {
                    "timestamp": now,
                    "severity": "ERROR",
                    "body": "Order PAYMENT_FAILED order_id=ord-1",
                    "attributes": {},
                    "resource": _resource("order-service"),
                    "trace_id": "trace-abc-001",
                    "span_id": "span-order-1",
                },
                {
                    "timestamp": now,
                    "severity": "ERROR",
                    "body": "Payment FAILED gateway timeout",
                    "attributes": {},
                    "resource": _resource("payment-service"),
                    "trace_id": "trace-abc-001",
                    "span_id": "span-pay-1",
                },
            ],
            "metrics": [
                {
                    "name": "payments_total",
                    "type": "COUNTER",
                    "value": 8,
                    "timestamp": now,
                    "attributes": {"status": "failed"},
                    "resource": _resource("payment-service"),
                },
                {
                    "name": "payment_duration_seconds",
                    "type": "HISTOGRAM",
                    "value": 2.5,
                    "timestamp": now,
                    "attributes": {"status": "failed"},
                    "resource": _resource("payment-service"),
                },
            ],
            "traces": [
                {
                    "trace_id": "trace-abc-001",
                    "span_id": "span-order-1",
                    "name": "order.create",
                    "start_time": now,
                    "end_time": now,
                    "status": "ERROR",
                    "attributes": {},
                    "resource": _resource("order-service"),
                },
                {
                    "trace_id": "trace-abc-001",
                    "span_id": "span-pay-1",
                    "parent_span_id": "span-order-1",
                    "name": "payment.process",
                    "start_time": now,
                    "end_time": now,
                    "status": "ERROR",
                    "attributes": {},
                    "resource": _resource("payment-service"),
                },
            ],
        },
    )
    assert r.status_code == 201
    iid = r.json()["incident_id"]

    corr = client.post(f"/api/v1/incidents/{iid}/correlate")
    assert corr.status_code == 200
    data = corr.json()

    assert data["incident_id"] == iid
    assert len(data["links"]) >= 1
    assert any(l["link_type"] == "trace_id" for l in data["links"])
    assert len(data["anomalies"]) >= 1
    assert data["severity"]["score"] > 0
    assert data["severity"]["level"] in ("ERROR", "FATAL", "WARN")
    assert "trace-abc-001" in data["correlated_trace_ids"]
    assert data["summary"]
    # Severity should have been bumped from WARN due to errors
    assert data["incident"]["severity"] in ("ERROR", "FATAL")


def test_correlate_is_idempotent(client):
    incident_store.clear()
    r = client.post(
        "/api/v1/incidents/context",
        json={
            "title": "Repeat correlation",
            "logs": [
                {
                    "timestamp": _now(),
                    "severity": "ERROR",
                    "body": "Payment FAILED",
                    "attributes": {},
                    "resource": _resource("payment-service"),
                }
            ],
        },
    )
    iid = r.json()["incident_id"]

    first = client.post(f"/api/v1/incidents/{iid}/correlate").json()
    second = client.post(f"/api/v1/incidents/{iid}/correlate").json()

    summary = second["incident"]["evidence_summary"]
    assert summary.count("correlated:") == 1
    assert summary == first["incident"]["evidence_summary"]


def test_correlate_not_found(client):
    incident_store.clear()
    r = client.post("/api/v1/incidents/does-not-exist/correlate")
    assert r.status_code == 404
