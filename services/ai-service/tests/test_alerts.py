from unittest.mock import AsyncMock, patch

import pytest

from app.services import incident_store

URL = "/api/v1/alerts/alertmanager"


def _alert(name="HighPaymentFailureRate", service="payment-service", severity="warning",
           fingerprint="fp1", status="firing", **labels):
    return {
        "status": status,
        "labels": {"alertname": name, "service": service, "severity": severity, **labels},
        "annotations": {"summary": "High payment failure rate",
                        "description": "More than 30% of payments are failing."},
        "startsAt": "2026-10-20T10:00:00Z",
        "endsAt": "0001-01-01T00:00:00Z",
        "generatorURL": "http://prometheus:9090/graph",
        "fingerprint": fingerprint,
    }


def _webhook(*alerts, status="firing", group="{}:{alertname=\"HighPaymentFailureRate\"}"):
    return {
        "version": "4",
        "groupKey": group,
        "status": status,
        "receiver": "ai-service",
        "groupLabels": {"alertname": "HighPaymentFailureRate"},
        "commonLabels": {},
        "commonAnnotations": {"summary": "High payment failure rate"},
        "externalURL": "http://alertmanager:9093",
        "alerts": list(alerts),
    }


@pytest.fixture(autouse=True)
def no_live_telemetry():
    incident_store.clear()
    with patch("app.services.alert_ingest.TelemetryEnrichmentService.enrich", new_callable=AsyncMock) as enrich:
        yield enrich


def test_firing_alert_creates_and_investigates_incident(client, no_live_telemetry):
    r = client.post(URL, json=_webhook(_alert(severity="critical")))
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "created"
    assert body["investigation_scheduled"] is True

    incident = incident_store.get(body["incident_id"])
    assert incident.title == "[Alert] High payment failure rate"
    assert incident.severity.value == "FATAL"
    assert incident.affected_services == ["payment-service"]
    assert incident.logs[0].body.startswith("ALERT HighPaymentFailureRate firing")
    assert incident.metadata["source"] == "alertmanager"
    assert incident.metadata["alert_fingerprints"] == ["fp1"]
    # Background investigation ran (TestClient runs background tasks before returning)
    no_live_telemetry.assert_awaited_once()
    assert incident.metadata["auto_investigation"]["steps"] == {
        "enrich": "ok", "correlate": "ok", "rca": "ok", "runbook": "ok"
    }
    assert incident.metadata["rca_primary_cause"]
    assert incident.metadata["runbook_title"]
    assert incident.status.value == "INVESTIGATING"


def test_repeat_notification_does_not_reinvestigate(client, no_live_telemetry):
    first = client.post(URL, json=_webhook(_alert())).json()
    again = client.post(URL, json=_webhook(_alert())).json()
    assert again["action"] == "updated"
    assert again["incident_id"] == first["incident_id"]
    assert again["new_alerts"] == 0
    assert again["investigation_scheduled"] is False
    assert no_live_telemetry.await_count == 1
    assert len(incident_store.get(first["incident_id"]).logs) == 1


def test_new_alert_in_group_is_added_and_reinvestigated(client, no_live_telemetry):
    first = client.post(URL, json=_webhook(_alert())).json()
    more = client.post(URL, json=_webhook(_alert(), _alert(service="order-service", fingerprint="fp2"))).json()
    assert more["new_alerts"] == 1 and more["investigation_scheduled"] is True
    incident = incident_store.get(first["incident_id"])
    assert len(incident.logs) == 2
    assert "order-service" in incident.affected_services
    assert no_live_telemetry.await_count == 2


def test_resolved_notification_resolves_incident(client):
    created = client.post(URL, json=_webhook(_alert())).json()
    r = client.post(URL, json=_webhook(_alert(status="resolved"), status="resolved")).json()
    assert r["action"] == "resolved"
    assert incident_store.get(created["incident_id"]).status.value == "RESOLVED"
    # A later firing notification for the same group opens a fresh incident
    reopened = client.post(URL, json=_webhook(_alert())).json()
    assert reopened["action"] == "created"
    assert reopened["incident_id"] != created["incident_id"]


def test_resolved_without_incident_is_ignored(client):
    r = client.post(URL, json=_webhook(_alert(status="resolved"), status="resolved"))
    assert r.json()["action"] == "ignored"


def test_opt_out_label_skips_investigation(client, no_live_telemetry):
    body = client.post(URL, json=_webhook(
        _alert(name="AIProviderFallbackActive", service="ai-service", ai_investigate="false"),
        group="ai",
    )).json()
    assert body["action"] == "created"
    assert body["investigation_scheduled"] is False
    no_live_telemetry.assert_not_awaited()
    assert "rca_primary_cause" not in incident_store.get(body["incident_id"]).metadata


def test_webhook_accepts_bearer_token_when_auth_enabled(client, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "ENABLE_AUTH", True)
    monkeypatch.setattr(get_settings(), "API_KEY", "secret-key")
    assert client.post(URL, json=_webhook(_alert())).status_code == 401
    r = client.post(URL, json=_webhook(_alert()), headers={"Authorization": "Bearer secret-key"})
    assert r.status_code == 200
