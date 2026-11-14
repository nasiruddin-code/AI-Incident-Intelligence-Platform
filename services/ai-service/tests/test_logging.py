import json
import logging
from unittest.mock import AsyncMock, patch

import pytest

from app.core.logging import JSONFormatter, _RequestContextFilter
from app.services import incident_store


class Collect(logging.Handler):
    def __init__(self):
        super().__init__()
        self.addFilter(_RequestContextFilter())
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture()
def collected():
    handler = Collect()
    root = logging.getLogger()
    old_level = root.level
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    yield handler.records
    root.removeHandler(handler)
    root.setLevel(old_level)


def test_request_id_generated_and_returned(client):
    r = client.get("/api/v1/health")
    assert len(r.headers["X-Request-ID"]) == 32


def test_valid_incoming_request_id_is_reused(client):
    r = client.get("/api/v1/health", headers={"X-Request-ID": "abc-123.req"})
    assert r.headers["X-Request-ID"] == "abc-123.req"


def test_malformed_request_id_is_replaced(client):
    r = client.get("/api/v1/health", headers={"X-Request-ID": "bad id\" injected"})
    assert r.headers["X-Request-ID"] != "bad id\" injected"
    assert len(r.headers["X-Request-ID"]) == 32


def test_errors_carry_request_id(client, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "ENABLE_AUTH", True)
    monkeypatch.setattr(get_settings(), "API_KEY", "secret-key")
    r = client.get("/api/v1/incidents", headers={"X-Request-ID": "auth-fail-1"})
    assert r.status_code == 401
    assert r.headers["X-Request-ID"] == "auth-fail-1"


def test_request_id_reaches_threadpool_and_background_logs(client, collected):
    incident_store.clear()
    alert = {
        "status": "firing",
        "labels": {"alertname": "HighPaymentFailureRate", "service": "payment-service", "severity": "warning"},
        "annotations": {"summary": "High payment failure rate"},
        "fingerprint": "fp-log-1",
    }
    with patch("app.services.alert_ingest.TelemetryEnrichmentService.enrich", new_callable=AsyncMock):
        client.post(
            "/api/v1/alerts/alertmanager",
            json={"groupKey": "g-log", "status": "firing", "alerts": [alert]},
            headers={"X-Request-ID": "trace-me-42"},
        )

    def ids_for(fragment):
        return {r.request_id for r in collected if fragment in r.getMessage()}

    # ingest runs in the threadpool; correlation/RCA run as background tasks
    assert ids_for("Alert group g-log") == {"trace-me-42"}
    assert ids_for("Auto-investigation for") == {"trace-me-42"}
    assert ids_for("Running RCA for incident") == {"trace-me-42"}


def test_json_formatter_fields():
    record = logging.LogRecord("app.x", logging.WARNING, __file__, 1, "hello %s", ("world",), None)
    record.request_id = "rid-1"
    record.otelTraceID = "4bf92f3577b34da6a3ce929d0e0e4736"
    record.otelSpanID = "00f067aa0ba902b7"
    entry = json.loads(JSONFormatter("ai-service").format(record))
    assert entry["message"] == "hello world"
    assert entry["level"] == "WARNING"
    assert entry["service"] == "ai-service"
    assert entry["request_id"] == "rid-1"
    assert entry["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert entry["timestamp"].endswith("+00:00")


def test_json_formatter_omits_empty_trace_and_includes_exception():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        record = logging.LogRecord("app.x", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    record.otelTraceID = "0"
    entry = json.loads(JSONFormatter().format(record))
    assert "trace_id" not in entry
    assert "ValueError: boom" in entry["exception"]
