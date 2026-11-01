from datetime import datetime, timezone

from fastapi.testclient import TestClient


def test_ingest_log(client: TestClient):
    now = datetime.now(timezone.utc)
    log_data = {
        "timestamp": now.isoformat(),
        "severity": "INFO",
        "body": "API request successful",
        "resource": {
            "service_name": "test-service"
        }
    }
    response = client.post("/api/v1/telemetry/logs", json=log_data)
    assert response.status_code == 200
    assert response.json()["body"] == "API request successful"

    # Invalid payload
    invalid_data = {**log_data, "severity": "INVALID"}
    response = client.post("/api/v1/telemetry/logs", json=invalid_data)
    assert response.status_code == 422


def test_ingest_metric(client: TestClient):
    now = datetime.now(timezone.utc)
    metric_data = {
        "name": "http_requests",
        "type": "COUNTER",
        "value": 42.0,
        "timestamp": now.isoformat(),
        "resource": {
            "service_name": "test-service"
        }
    }
    response = client.post("/api/v1/telemetry/metrics", json=metric_data)
    assert response.status_code == 200
    assert response.json()["value"] == 42.0


def test_ingest_trace(client: TestClient):
    now = datetime.now(timezone.utc)
    span_data = {
        "trace_id": "t123",
        "span_id": "s456",
        "name": "db_query",
        "start_time": now.isoformat(),
        "end_time": now.isoformat(),
        "status": "OK",
        "resource": {
            "service_name": "test-service"
        }
    }
    response = client.post("/api/v1/telemetry/traces", json=span_data)
    assert response.status_code == 200
    assert response.json()["trace_id"] == "t123"
