import time
from typing import List
from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import get_settings
from app.schemas.incident import IncidentStatus
from app.schemas.telemetry import SeverityLevel
from app.services import incident_store
from app.services.log_monitor import LogMonitor, classify, is_failure, parse_line, spike_score

DB_ERR = "2026-11-01T10:00:00Z ERROR [catalog] SequelizeConnectionRefusedError: connect ECONNREFUSED 10.0.0.7:5432 (request {})"


def _settings(**overrides):
    base = {
        "LOG_MONITOR_INVESTIGATE": True,
        "ALERT_AUTO_ENRICH": False,  # no Loki/Prometheus/Tempo in unit tests
        "BASELINE_ENABLED": False,
        "SMTP_HOST": "smtp.test",
        "NOTIFY_EMAIL_TO": ["support@example.com"],
    }
    return get_settings().model_copy(update={**base, **overrides})


def _loki(lines: List[tuple]) -> dict:
    """lines: (service, text, age_seconds)."""
    now = time.time()
    streams = {}
    for service, text, age in lines:
        streams.setdefault(service, []).append([str(int((now - age) * 1e9)), text])
    return {"data": {"result": [
        {"stream": {"service_name": svc, "source": "docker"}, "values": vals} for svc, vals in streams.items()
    ]}}


@pytest.fixture(autouse=True)
def clean():
    incident_store.clear()
    yield
    incident_store.clear()


@pytest.fixture()
def sent():
    """Captured emails (subject, event) instead of SMTP."""
    out = []
    with patch("app.services.email_report.send_email", side_effect=lambda msg, s: out.append(msg)):
        yield out


async def _poll(monitor: LogMonitor, lines: List[tuple]):
    with patch("app.services.log_monitor.LokiClient.query_range", new=AsyncMock(return_value=_loki(lines))):
        summary = await monitor.run_once()
    await monitor.wait_idle()
    return summary


# ---- parsing / classification ------------------------------------------------

def test_parse_json_python_log_and_access_line():
    p = parse_line('{"level": "ERROR", "message": "Payment gateway timeout", "trace_id": "abc"}')
    assert (p.severity, p.message, p.trace_id) == (SeverityLevel.ERROR, "Payment gateway timeout", "abc")
    access = parse_line('{"level": "INFO", "logger": "uvicorn.access", "message": "1.2.3.4:5 - \\"POST /pay HTTP/1.1\\" 503"}')
    assert access.severity == SeverityLevel.INFO and is_failure(access)


def test_parse_plain_text_levels_and_failure_rules():
    parsed = parse_line(DB_ERR)
    assert parsed.severity == SeverityLevel.ERROR
    assert parsed.message.startswith("[catalog] SequelizeConnectionRefusedError")  # timestamp + level stripped
    assert parse_line("[2026-11-03 10:00:00,123] WARN: disk 91% full").message == "disk 91% full"
    assert is_failure(parse_line("WARN Payment FAILED (injected) order=1"))
    assert not is_failure(parse_line("WARN cache miss ratio high"))
    assert not is_failure(parse_line("INFO GET /products 200 3ms"))
    assert is_failure(parse_line("INFO request done status=500 path=/x"))


@pytest.mark.parametrize("message,expected", [
    (DB_ERR, "database"),
    ("Payment gateway declined card", "payment"),
    ("upstream request timed out after 5000ms", "timeout"),
    ("java.lang.OutOfMemoryError: Java heap space", "resource"),
    ("connect ECONNREFUSED inventory:8080", "dependency"),
    ('"GET /x HTTP/1.1" 500', "http_5xx"),
    ("GET /stock status=503 4ms", "http_5xx"),  # response status beats the bare-503 dependency rule
    ("upstream service returned 503", "dependency"),
    ("Something odd happened: error 42", "application_error"),
])
def test_classify(message, expected):
    assert classify(message) == expected


def test_spike_score_uses_robust_baseline():
    assert spike_score([1, 1, 2, 1, 1], 10) >= 3
    assert spike_score([8, 10, 9, 11, 10], 12) < 3


# ---- detection -> incident -> investigation -> email ------------------------------

@pytest.mark.asyncio
async def test_new_error_pattern_opens_investigated_incident_and_emails(sent):
    monitor = LogMonitor(_settings())
    summary = await _poll(monitor, [("catalog-service", DB_ERR.format(i), 5) for i in range(4)]
                          + [("catalog-service", "INFO GET /products 200", 5)])
    assert summary["failures"] == 4 and summary["triggers"] == 1

    [incident] = incident_store.list_by_metadata("source", "log_monitor")
    lm = incident.metadata["log_monitor"]
    assert incident.title == "[Logs] catalog-service: Database errors"
    assert incident.metadata["log_group_key"] == "catalog-service:database"
    assert lm["error_count"] == 4 and lm["detection"] == ["new_pattern"]
    assert incident.affected_services == ["catalog-service"]
    # investigated: RCA + full runbook stored, status advanced
    assert incident.metadata["rca_primary_cause"] and incident.metadata["runbook_steps"]
    assert incident.status == IncidentStatus.INVESTIGATING
    # emails: new incident, then the full report, threaded
    assert [m["X-Incident-Event"] for m in sent] == ["incident_created", "runbook_completed"]
    assert sent[1]["In-Reply-To"] == sent[0]["Message-ID"]
    report = sent[1].get_body(("plain",)).get_content()
    for section in ("PROBABLE ROOT CAUSE", "SUGGESTED RESOLUTION", "POTENTIAL IMPACT", "KEY EVIDENCE"):
        assert section in report


@pytest.mark.asyncio
async def test_continuing_errors_update_the_same_incident(sent):
    monitor = LogMonitor(_settings(LOG_MONITOR_INVESTIGATE=False))
    await _poll(monitor, [("catalog-service", DB_ERR.format(i), 5) for i in range(3)])
    await _poll(monitor, [("catalog-service", DB_ERR.format(i), 2) for i in range(5)])
    [incident] = incident_store.list_by_metadata("source", "log_monitor")
    assert incident.metadata["log_monitor"]["error_count"] == 8
    assert [m["X-Incident-Event"] for m in sent] == ["incident_created"]


@pytest.mark.asyncio
async def test_known_pattern_needs_a_spike():
    monitor = LogMonitor(_settings(LOG_MONITOR_INVESTIGATE=False, NOTIFY_EMAIL_TO=[]))
    for _ in range(5):  # one stray error per poll: below the new-pattern threshold, becomes "normal"
        await _poll(monitor, [("orders", "ERROR cache refresh failed for key 1", 5)])
    assert incident_store.list_by_metadata("source", "log_monitor") == []
    await _poll(monitor, [("orders", "ERROR cache refresh failed for key 1", 5)] * 12)
    [incident] = incident_store.list_by_metadata("source", "log_monitor")
    assert incident.metadata["log_monitor"]["detection"] == ["spike"]


@pytest.mark.asyncio
async def test_services_and_classes_are_separate_incidents_and_exclusions_apply():
    monitor = LogMonitor(_settings(LOG_MONITOR_INVESTIGATE=False, NOTIFY_EMAIL_TO=[],
                                   LOG_MONITOR_EXCLUDE_SERVICES=["noisy"]))
    await _poll(monitor, [("catalog-service", DB_ERR.format(1), 5)] * 2
                + [("payment-service", "WARN Payment FAILED (injected) order=9", 5)] * 3
                + [("noisy", "ERROR always broken", 5)] * 10)
    keys = sorted(i.metadata["log_group_key"] for i in incident_store.list_by_metadata("source", "log_monitor"))
    assert keys == ["catalog-service:database", "payment-service:payment"]


@pytest.mark.asyncio
async def test_quiet_incident_auto_resolves_and_recurrence_is_flagged(sent):
    monitor = LogMonitor(_settings(LOG_MONITOR_INVESTIGATE=False, LOG_MONITOR_RESOLVE_AFTER_SECONDS=60))
    # errors 10 minutes old: opened, then resolved in the same poll (quiet > 60 s)
    await _poll(monitor, [("catalog-service", DB_ERR.format(i), 600) for i in range(3)])
    [first] = incident_store.list_by_metadata("source", "log_monitor")
    assert first.status == IncidentStatus.RESOLVED
    assert [m["X-Incident-Event"] for m in sent] == ["incident_created", "incident_resolved"]

    await _poll(monitor, [("catalog-service", DB_ERR.format(i), 2) for i in range(3)])
    newest = incident_store.list_by_metadata("source", "log_monitor")[0]
    assert newest.incident_id != first.incident_id
    assert newest.metadata["log_monitor"]["recurring_count"] == 1
    assert "recurring: 2x" in newest.title


@pytest.mark.asyncio
async def test_loki_failure_is_reported_not_raised():
    monitor = LogMonitor(_settings())
    with patch("app.services.log_monitor.LokiClient.query_range", new=AsyncMock(side_effect=OSError("loki down"))):
        summary = await monitor.run_once()
    assert summary["error"] == "loki down" and monitor.status()["last_error"] == "loki down"


def test_endpoints(client):
    with patch("app.services.log_monitor.LokiClient.query_range", new=AsyncMock(return_value=_loki([]))):
        r = client.post("/api/v1/log-monitor/run")
    assert r.status_code == 200 and r.json()["summary"]["lines"] == 0
    status = client.get("/api/v1/log-monitor/status").json()
    assert status["runs"] >= 1 and "|~" in status["query"]


@pytest.mark.asyncio
async def test_http_5xx_symptoms_join_the_cause_incident():
    monitor = LogMonitor(_settings(LOG_MONITOR_INVESTIGATE=False, NOTIFY_EMAIL_TO=[]))
    access = "2026-11-01T10:00:00Z WARN  [catalog] GET /products status=500 12ms req={}"
    await _poll(monitor, [("catalog-service", DB_ERR.format(i), 5) for i in range(3)]
                + [("catalog-service", access.format(i), 5) for i in range(3)])
    [incident] = incident_store.list_by_metadata("source", "log_monitor")
    assert incident.metadata["log_group_key"] == "catalog-service:database"
    assert incident.metadata["log_monitor"]["error_count"] == 6
    # a later poll with only the symptom still extends the open cause incident
    await _poll(monitor, [("catalog-service", access.format(9), 2)] * 3)
    [incident] = incident_store.list_by_metadata("source", "log_monitor")
    assert incident.metadata["log_monitor"]["error_count"] == 9


@pytest.mark.asyncio
async def test_incident_time_window_grows_with_new_evidence():
    monitor = LogMonitor(_settings(LOG_MONITOR_INVESTIGATE=False, NOTIFY_EMAIL_TO=[]))
    await _poll(monitor, [("catalog-service", DB_ERR.format(i), 90) for i in range(3)])
    await _poll(monitor, [("catalog-service", DB_ERR.format(i), 5) for i in range(3)])
    [incident] = incident_store.list_by_metadata("source", "log_monitor")
    span = (incident.time_window.end - incident.time_window.start).total_seconds()
    assert 80 <= span <= 90
