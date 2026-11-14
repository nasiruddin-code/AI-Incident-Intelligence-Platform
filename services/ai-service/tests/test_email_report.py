from datetime import datetime, timezone

from app.core.config import get_settings
from app.schemas.incident import IncidentContext, IncidentStatus, TimeWindow
from app.schemas.telemetry import LogRecord, Resource, SeverityLevel
from app.services import email_report
from app.services.email_report import build_email, redact

NOW = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)


def _settings(**kw):
    return get_settings().model_copy(update={
        "SMTP_HOST": "mailpit", "NOTIFY_EMAIL_TO": ["support@example.com", "oncall@example.com"],
        "UI_BASE_URL": "http://localhost:8080", **kw,
    })


def _incident(**metadata) -> IncidentContext:
    return IncidentContext(
        incident_id="inc-1", title="[Logs] catalog-service: Database errors", status=IncidentStatus.INVESTIGATING,
        severity=SeverityLevel.ERROR, affected_services=["catalog-service"],
        time_window=TimeWindow(start=NOW, end=NOW),
        logs=[LogRecord(timestamp=NOW, severity=SeverityLevel.ERROR,
                        body="charge failed card=4111 1111 1111 1111 user=jane@corp.com <script>x</script>",
                        resource=Resource(service_name="catalog-service"))],
        metadata=metadata,
    )


def test_redaction():
    text = redact("token=abc123 Authorization: Bearer eyJa.eyJb.sig card 4111-1111-1111-1111 to a@b.co")
    for secret in ("abc123", "eyJa", "4111", "a@b.co"):
        assert secret not in text


def test_full_report_contains_cause_steps_and_safe_evidence():
    incident = _incident(
        log_monitor={"error_class": "database", "detection": ["new_pattern"], "error_count": 12, "recurring_count": 2},
        severity_score=82.5, rca_primary_cause="Database at 10.0.0.7:5432 refusing connections",
        rca_confidence=0.8, rca_summary="Catalog cannot reach its database.",
        rca_hypotheses=[{"cause": "a"}, {"cause": "Network policy change", "confidence": 0.3}],
        runbook_title="Restore database connectivity", runbook_confidence=0.7,
        runbook_steps=[{"order": 1, "action": "Check DB pod", "command_or_check": "docker compose ps catalog-db",
                        "expected_result": "running"}],
        runbook_remediation=[{"priority": 1, "action": "Restart DB", "rationale": "down", "risk_level": "low"}],
        runbook_verification=["GET /products returns 200"], runbook_provider="anthropic",
    )
    msg = build_email("runbook_completed", incident, {}, _settings())
    text = msg.get_body(("plain",)).get_content()
    page = msg.get_body(("html",)).get_content()

    assert msg["Subject"] == "[ERROR] Incident report: [Logs] catalog-service: Database errors"
    assert msg["To"] == "support@example.com, oncall@example.com"
    assert msg["In-Reply-To"] == "<incident-inc-1@incident-intelligence.local>"
    for expected in ("Database at 10.0.0.7:5432 refusing connections (confidence 80%)", "1. Check DB pod",
                     "P1: Restart DB", "Recurring: seen 3x", "severity score 82.5/100",
                     "http://localhost:8080/#/incident/inc-1", "Network policy change"):
        assert expected in text, expected
    assert "4111" not in text and "jane@corp.com" not in text
    assert "<script>" not in page and "&lt;script&gt;" in page


def test_created_and_resolved_emails():
    created = build_email("incident_created", _incident(log_monitor={"example": "ERROR db down"}), {}, _settings())
    assert created["Message-ID"] == "<incident-inc-1@incident-intelligence.local>"
    assert "full incident report will follow" in created.get_body(("plain",)).get_content()

    resolved = build_email("incident_resolved", _incident(resolved_at="2026-11-01T10:20:00+00:00"),
                           {"error_duration_seconds": 300, "quiet_seconds": 300}, _settings())
    body = resolved.get_body(("plain",)).get_content()
    assert resolved["Subject"].startswith("[ERROR] Resolved:") and "about 5.0 minute(s)" in body


def test_degraded_and_mock_reports_are_labelled():
    msg = build_email("runbook_completed", _incident(rca_primary_cause="x", rca_degraded=True), {}, _settings())
    assert "placeholder text" in msg.get_body(("plain",)).get_content()
    msg = build_email("runbook_completed", _incident(rca_primary_cause="x", rca_provider="mock"), {}, _settings())
    assert "AI_PROVIDER=mock" in msg.get_body(("plain",)).get_content()


def test_send_email_uses_smtp(monkeypatch):
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context):
            calls.append(("starttls",))

        def login(self, user, pw):
            calls.append(("login", user))

        def send_message(self, msg):
            calls.append(("send", msg["Subject"]))

    monkeypatch.setattr(email_report.smtplib, "SMTP", FakeSMTP)
    settings = _settings(SMTP_PORT=1025, SMTP_STARTTLS=True, SMTP_USERNAME="u", SMTP_PASSWORD="p")
    email_report.send_email(build_email("incident_created", _incident(), {}, settings), settings)
    assert [c[0] for c in calls] == ["connect", "starttls", "login", "send"]
    assert calls[0] == ("connect", "mailpit", 1025)


def test_evidence_shows_one_line_per_pattern():
    incident = _incident(runbook_provider="anthropic", rca_primary_cause="x")
    incident.logs = [
        LogRecord(timestamp=NOW, severity=SeverityLevel.ERROR, body=f"redis error connecting to cache:6379 (sku=SKU-{i})",
                  resource=Resource(service_name="inventory"))
        for i in range(5)
    ] + [LogRecord(timestamp=NOW, severity=SeverityLevel.WARN, body="GET /stock status=503 4ms",
                   resource=Resource(service_name="inventory"))]
    text = build_email("runbook_completed", incident, {}, _settings()).get_body(("plain",)).get_content()
    evidence = text.split("KEY EVIDENCE", 1)[1].split("\n\n")[0]
    assert evidence.count("redis error") == 1 and "status=503" in evidence
