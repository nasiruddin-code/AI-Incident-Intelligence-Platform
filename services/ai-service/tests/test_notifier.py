import hashlib
import hmac
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import Settings, get_settings
from app.schemas.incident import IncidentContext, TimeWindow
from app.services import incident_store, notifier

HOOK = "https://hooks.example/incidents"
SLACK = "https://hooks.slack.example/T000/B000/XXX"


def _incident(severity="ERROR", title="Payments <failing> & slow"):
    now = datetime.now(timezone.utc)
    return IncidentContext(
        incident_id="inc-1", title=title, status="OPEN", severity=severity,
        affected_services=["payment-service"], time_window=TimeWindow(start=now, end=now),
    )


class Capture:
    """Stands in for the shared httpx client; records every POST."""

    def __init__(self, status=200, error=None):
        self.calls = []
        self.status = status
        self.error = error

    async def post(self, url, content, headers, timeout):
        self.calls.append({"url": url, "body": json.loads(content), "headers": headers})
        if self.error:
            raise self.error
        return SimpleNamespace(status_code=self.status)

    def events(self, url=HOOK):
        return [c["body"]["event"] for c in self.calls if c["url"] == url]


@pytest.fixture()
def capture():
    cap = Capture()
    notifier.delivery_counts.clear()
    with patch("app.services.notifier.get_http_client", return_value=cap):
        yield cap


def _settings(**kw):
    return Settings(_env_file=None, **kw)


@pytest.mark.asyncio
async def test_no_targets_means_no_calls(capture):
    await notifier.notify("incident_created", _incident(), settings=_settings())
    assert capture.calls == []


@pytest.mark.asyncio
async def test_event_and_severity_filters(capture):
    s = _settings(NOTIFY_WEBHOOK_URLS=[HOOK], NOTIFY_EVENTS=["rca_completed"], NOTIFY_MIN_SEVERITY="ERROR")
    await notifier.notify("incident_created", _incident(), settings=s)        # event filtered
    await notifier.notify("rca_completed", _incident("WARN"), settings=s)     # below min severity
    await notifier.notify("rca_completed", _incident("FATAL"), settings=s)
    assert capture.events() == ["rca_completed"]


@pytest.mark.asyncio
async def test_generic_webhook_payload_and_signature(capture):
    s = _settings(NOTIFY_WEBHOOK_URLS=[HOOK], NOTIFY_WEBHOOK_SECRET="topsecret",
                  PUBLIC_BASE_URL="https://ai.example.com/")
    await notifier.notify("rca_completed", _incident(), {"primary_cause": "gateway timeout"}, settings=s)

    call = capture.calls[0]
    body = call["body"]
    assert body["event"] == "rca_completed"
    assert body["incident"]["url"] == "https://ai.example.com/api/v1/incidents/inc-1"
    assert body["details"]["primary_cause"] == "gateway timeout"
    assert call["headers"]["X-Event"] == "rca_completed"
    raw = json.dumps(body).encode()
    expected = "sha256=" + hmac.new(b"topsecret", raw, hashlib.sha256).hexdigest()
    assert call["headers"]["X-Signature-SHA256"] == expected


@pytest.mark.asyncio
async def test_slack_message_is_escaped_and_flags_degraded(capture):
    s = _settings(NOTIFY_SLACK_WEBHOOK_URL=SLACK)
    await notifier.notify(
        "rca_completed", _incident(),
        {"primary_cause": "a <b> c", "confidence": 0.8, "degraded": True}, settings=s,
    )
    msg = capture.calls[0]["body"]
    assert "Payments &lt;failing&gt; &amp; slow" in msg["blocks"][0]["text"]["text"]
    detail = msg["blocks"][2]["text"]["text"]
    assert "a &lt;b&gt; c" in detail and "fallback provider" in detail


@pytest.mark.asyncio
async def test_delivery_failure_is_counted_not_raised():
    cap = Capture(error=ConnectionError("refused"))
    notifier.delivery_counts.clear()
    with patch("app.services.notifier.get_http_client", return_value=cap):
        await notifier.notify("incident_created", _incident(), settings=_settings(NOTIFY_WEBHOOK_URLS=[HOOK]))
    assert notifier.delivery_counts[("webhook", "error")] == 1
    assert 'ai_notifications_total{target="webhook",outcome="error"} 1' in "\n".join(notifier.render_metrics())


# --- Wiring: endpoints and alert flow ----------------------------------------

@pytest.fixture()
def hooks_on(monkeypatch, capture):
    incident_store.clear()
    monkeypatch.setattr(get_settings(), "NOTIFY_WEBHOOK_URLS", [HOOK])
    return capture


def test_api_flow_notifies(client, hooks_on):
    iid = client.post("/api/v1/incidents", json={"title": "Checkout down", "severity": "ERROR"}).json()["incident_id"]
    client.post(f"/api/v1/incidents/{iid}/rca")
    client.post(f"/api/v1/incidents/{iid}/runbook")
    client.patch(f"/api/v1/incidents/{iid}/status?new_status=RESOLVED")
    client.patch(f"/api/v1/incidents/{iid}/status?new_status=RESOLVED")  # no duplicate
    assert hooks_on.events() == ["incident_created", "rca_completed", "runbook_completed", "incident_resolved"]
    rca = hooks_on.calls[1]["body"]["details"]
    assert rca["primary_cause"] and rca["degraded"] is False


def test_enriching_existing_incident_is_not_a_new_incident(client, hooks_on):
    iid = client.post("/api/v1/incidents", json={"title": "x", "severity": "ERROR"}).json()["incident_id"]
    client.post("/api/v1/incidents/context", json={"incident_id": iid, "notes": "more"})
    assert hooks_on.events() == ["incident_created"]


def test_alert_flow_notifies_in_order(client, hooks_on):
    alert = {
        "status": "firing",
        "labels": {"alertname": "HighPaymentFailureRate", "service": "payment-service", "severity": "critical"},
        "annotations": {"summary": "High payment failure rate"},
        "startsAt": "2026-10-20T10:00:00Z",
        "fingerprint": "fp-n1",
    }
    with patch("app.services.alert_ingest.TelemetryEnrichmentService.enrich", new_callable=AsyncMock):
        client.post("/api/v1/alerts/alertmanager", json={"groupKey": "g1", "status": "firing", "alerts": [alert]})
        client.post("/api/v1/alerts/alertmanager",
                    json={"groupKey": "g1", "status": "resolved", "alerts": [{**alert, "status": "resolved"}]})
    assert hooks_on.events() == ["incident_created", "rca_completed", "runbook_completed", "incident_resolved"]
    assert hooks_on.calls[0]["body"]["details"]["source"] == "alertmanager"
    assert hooks_on.calls[0]["body"]["details"]["alertnames"] == ["HighPaymentFailureRate"]
