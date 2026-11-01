from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.schemas.incident import ScenarioType
from app.schemas.telemetry import (
    LogRecord,
    MetricPoint,
    MetricType,
    Resource,
    SeverityLevel,
    Span,
    SpanStatus,
)
from app.services import incident_store


def _resource(name="order-service"):
    return Resource(service_name=name, service_version="0.1.0", environment="test")


def test_create_and_get_incident(client):
    incident_store.clear()
    r = client.post(
        "/api/v1/incidents",
        json={
            "title": "Test outage",
            "severity": "ERROR",
            "affected_services": ["order-service"],
        },
    )
    assert r.status_code == 201
    data = r.json()
    assert data["title"] == "Test outage"
    assert data["status"] == "OPEN"
    assert "incident_id" in data

    r2 = client.get(f"/api/v1/incidents/{data['incident_id']}")
    assert r2.status_code == 200
    assert r2.json()["incident_id"] == data["incident_id"]


def test_list_incidents(client):
    incident_store.clear()
    client.post("/api/v1/incidents", json={"title": "A", "severity": "WARN"})
    client.post("/api/v1/incidents", json={"title": "B", "severity": "ERROR"})
    r = client.get("/api/v1/incidents")
    assert r.status_code == 200
    assert r.json()["count"] == 2


def test_build_context_from_telemetry(client):
    incident_store.clear()
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "title": "Payment failures detected",
        "severity": "ERROR",
        "affected_services": ["payment-service"],
        "logs": [
            {
                "timestamp": now,
                "severity": "ERROR",
                "body": "Payment FAILED order_id=ord-1",
                "attributes": {},
                "resource": {
                    "service_name": "payment-service",
                    "service_version": "0.1.0",
                    "environment": "test",
                    "attributes": {},
                },
            }
        ],
        "metrics": [
            {
                "name": "payments_total",
                "type": "COUNTER",
                "value": 5,
                "timestamp": now,
                "attributes": {"status": "failed"},
                "resource": {
                    "service_name": "payment-service",
                    "service_version": "0.1.0",
                    "environment": "test",
                    "attributes": {},
                },
            }
        ],
        "traces": [
            {
                "trace_id": "abc123",
                "span_id": "span1",
                "name": "payment.process",
                "start_time": now,
                "end_time": now,
                "status": "ERROR",
                "attributes": {},
                "resource": {
                    "service_name": "payment-service",
                    "service_version": "0.1.0",
                    "environment": "test",
                    "attributes": {},
                },
            }
        ],
    }
    r = client.post("/api/v1/incidents/context", json=payload)
    assert r.status_code == 201
    data = r.json()
    assert data["title"] == "Payment failures detected"
    assert len(data["logs"]) == 1
    assert len(data["metrics"]) == 1
    assert len(data["traces"]) == 1
    assert "payment-service" in data["affected_services"]
    assert data["evidence_summary"] is not None
    assert "error" in data["evidence_summary"].lower()


def test_build_context_enriches_existing(client):
    incident_store.clear()
    create = client.post(
        "/api/v1/incidents",
        json={"title": "Shell", "severity": "WARN", "affected_services": ["order-service"]},
    )
    iid = create.json()["incident_id"]
    now = datetime.now(timezone.utc).isoformat()
    r = client.post(
        "/api/v1/incidents/context",
        json={
            "incident_id": iid,
            "logs": [
                {
                    "timestamp": now,
                    "severity": "ERROR",
                    "body": "boom",
                    "attributes": {},
                    "resource": {
                        "service_name": "payment-service",
                        "attributes": {},
                    },
                }
            ],
        },
    )
    assert r.status_code == 201
    data = r.json()
    assert data["incident_id"] == iid
    assert len(data["logs"]) == 1
    assert "payment-service" in data["affected_services"]
    assert data["severity"] == "ERROR"  # bumped from WARN by error log


def test_scenario_run_mocked(client):
    incident_store.clear()

    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {
        "order_id": "ord-fail-1",
        "status": "payment_failed",
        "failure_reason": "Simulated payment gateway timeout",
    }

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.post.return_value = mock_resp

    with patch("app.services.scenario_runner.httpx.AsyncClient", return_value=mock_client):
        r = client.post(
            "/api/v1/incidents/scenarios/run",
            json={
                "scenario": "payment_timeout",
                "order_count": 3,
                "order_service_url": "http://order-service:8001",
            },
        )
    assert r.status_code == 201
    data = r.json()
    assert data["scenario"] == "payment_timeout"
    assert data["orders_attempted"] == 3
    assert data["orders_failed"] == 3
    assert data["incident"]["scenario"] == "payment_timeout"
    assert len(data["incident"]["logs"]) > 0
    assert len(data["incident"]["traces"]) > 0


def test_update_status(client):
    incident_store.clear()
    create = client.post("/api/v1/incidents", json={"title": "X", "severity": "INFO"})
    iid = create.json()["incident_id"]
    r = client.patch(f"/api/v1/incidents/{iid}/status?new_status=INVESTIGATING")
    assert r.status_code == 200
    assert r.json()["status"] == "INVESTIGATING"


def test_incident_not_found(client):
    incident_store.clear()
    r = client.get("/api/v1/incidents/does-not-exist")
    assert r.status_code == 404
