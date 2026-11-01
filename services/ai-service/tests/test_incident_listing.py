from datetime import datetime, timezone

import pytest

from app.schemas.incident import IncidentContext, IncidentStatus, TimeWindow
from app.schemas.telemetry import SeverityLevel
from app.services import incident_store


def _incident(iid, severity="ERROR", status="OPEN", metadata=None):
    now = datetime.now(timezone.utc)
    return IncidentContext(
        incident_id=iid,
        title=f"Incident {iid}",
        status=status,
        severity=severity,
        affected_services=["payment-service"],
        time_window=TimeWindow(start=now, end=now),
        evidence_summary=f"summary {iid}",
        metadata=metadata or {},
    )


def _seed():
    incident_store.clear()
    incident_store.save(_incident("a", "WARN", "OPEN", {"rca_confidence": 0.4, "severity_score": 50}))
    incident_store.save(_incident("b", "ERROR", "RESOLVED", {"rca_confidence": 0.8, "runbook_confidence": 0.6}))
    incident_store.save(_incident("c", "ERROR", "INVESTIGATING"))


def test_list_is_paginated_newest_first(client):
    _seed()
    r = client.get("/api/v1/incidents", params={"limit": 2})
    body = r.json()
    assert r.status_code == 200
    assert body["count"] == 2
    assert body["total"] == 3
    assert [i["incident_id"] for i in body["incidents"]] == ["c", "b"]

    r = client.get("/api/v1/incidents", params={"limit": 2, "offset": 2})
    assert [i["incident_id"] for i in r.json()["incidents"]] == ["a"]


def test_resaving_moves_incident_to_front(client):
    _seed()
    incident_store.save(incident_store.get("a"))
    ids = [i["incident_id"] for i in client.get("/api/v1/incidents").json()["incidents"]]
    assert ids == ["a", "c", "b"]


def test_list_filters(client):
    _seed()
    body = client.get("/api/v1/incidents", params={"severity": "ERROR"}).json()
    assert body["total"] == 2
    body = client.get("/api/v1/incidents", params={"status": "OPEN"}).json()
    assert [i["incident_id"] for i in body["incidents"]] == ["a"]


def test_list_rejects_bad_limit(client):
    assert client.get("/api/v1/incidents", params={"limit": 0}).status_code == 422
    assert client.get("/api/v1/incidents", params={"limit": 501}).status_code == 422


def test_summaries_have_no_evidence(client):
    _seed()
    r = client.get("/api/v1/incidents/summaries", params={"severity": "ERROR"})
    body = r.json()
    assert r.status_code == 200
    assert body["total"] == 2
    item = body["incidents"][0]
    assert item["incident_id"] == "c"
    assert item["evidence_summary"] == "summary c"
    assert "logs" not in item and "traces" not in item


def test_metrics_values(client):
    _seed()
    body = client.get("/metrics").text
    assert "ai_incidents_total 3\n" in body
    assert "ai_incidents_open 2\n" in body
    assert 'ai_incidents_by_severity{severity="ERROR"} 2\n' in body
    assert 'ai_incidents_by_severity{severity="WARN"} 1\n' in body
    assert "ai_rca_runs_total 2\n" in body
    assert "ai_runbook_runs_total 1\n" in body
    rca_avg = float(body.split("\nai_rca_confidence_avg ")[1].split("\n")[0])
    assert rca_avg == pytest.approx(0.6)


@pytest.fixture()
def sqlite_store(tmp_path, monkeypatch):
    """Run the store's SQL backend against a throwaway SQLite file."""
    from app.core.config import get_settings
    from app.db import session as db_session

    monkeypatch.setattr(get_settings(), "DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_SessionLocal", None)
    db_session.init_db()
    yield
    db_session._engine.dispose()


def test_sql_backend_list_and_stats(sqlite_store):
    _seed()

    items, total = incident_store.list_page(severity=SeverityLevel.ERROR, limit=10)
    assert total == 2
    assert {i.incident_id for i in items} == {"b", "c"}

    summaries, total = incident_store.list_summaries(status=IncidentStatus.OPEN)
    assert total == 1
    assert summaries[0].incident_id == "a"
    assert summaries[0].affected_services == ["payment-service"]
    assert summaries[0].evidence_summary == "summary a"

    _, total = incident_store.list_page(limit=1, offset=0)
    assert total == 3

    stats = incident_store.stats()
    assert stats.total == 3
    assert stats.open == 2
    assert stats.by_severity == {"WARN": 1, "ERROR": 2}
    assert stats.rca_runs == 2
    assert stats.rca_confidence_avg == pytest.approx(0.6)
    assert stats.runbook_runs == 1
    assert stats.runbook_confidence_avg == pytest.approx(0.6)
    assert stats.severity_score_avg == pytest.approx(50)
