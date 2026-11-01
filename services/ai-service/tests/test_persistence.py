"""Optimistic locking, evidence rows, and Alembic migrations.

Runs against the in-memory store, SQLite, and — when TEST_DATABASE_URL points
at a disposable Postgres database (CI sets one) — real Postgres.
"""
import os
import threading
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.core.config import get_settings
from app.db import session as db_session
from app.schemas.incident import BuildContextRequest, IncidentContext, TimeWindow
from app.schemas.telemetry import LogRecord, MetricPoint, Resource, SeverityLevel, Span, SpanStatus
from app.services import incident_store
from app.services.context_builder import build_context
from app.services.incident_store import ConcurrentModificationError

PG_URL = os.environ.get("TEST_DATABASE_URL")
T0 = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)


def _log(i, body=None):
    return LogRecord(timestamp=T0 + timedelta(seconds=i), severity=SeverityLevel.ERROR,
                     body=body or f"Payment FAILED #{i}", resource=Resource(service_name="payment-service"))


def _metric(v):
    return MetricPoint(name="payments_total", type="COUNTER", value=v, timestamp=T0,
                       attributes={"status": "failed"}, resource=Resource(service_name="payment-service"))


def _span(i):
    return Span(trace_id=f"t{i}", span_id=f"s{i}", name="payment.process", start_time=T0, end_time=T0,
                status=SpanStatus.ERROR, resource=Resource(service_name="payment-service"))


def _reset_db_schema(url):
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        for table in ("incident_evidence", "incidents", "alembic_version"):
            conn.execute(sa.text(f"DROP TABLE IF EXISTS {table}" + (" CASCADE" if conn.dialect.name == "postgresql" else "")))
    engine.dispose()


def _use_database(monkeypatch, url):
    monkeypatch.setattr(get_settings(), "DATABASE_URL", url)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_SessionLocal", None)


def _backend_params():
    params = ["memory", "sqlite"]
    params.append(pytest.param("postgres", marks=pytest.mark.skipif(not PG_URL, reason="TEST_DATABASE_URL not set")))
    return params


@pytest.fixture(params=_backend_params())
def backend(request, tmp_path, monkeypatch):
    if request.param == "memory":
        incident_store.clear()
        yield "memory"
        return
    url = f"sqlite:///{tmp_path / 'store.db'}" if request.param == "sqlite" else PG_URL
    _reset_db_schema(url)
    _use_database(monkeypatch, url)
    db_session.init_db()
    yield request.param
    db_session._engine.dispose()


def _new_incident(iid="inc-1", logs=None):
    return IncidentContext(
        incident_id=iid, title="Payments failing", status="OPEN", severity="ERROR",
        affected_services=["payment-service"], time_window=TimeWindow(start=T0, end=T0),
        logs=logs or [], metrics=[_metric(5)], traces=[_span(1)],
    )


# --- Optimistic locking -----------------------------------------------------------

def test_versions_increment_and_stale_save_is_rejected(backend):
    incident_store.save(_new_incident())
    a = incident_store.get("inc-1")
    b = incident_store.get("inc-1")
    assert a.version == b.version == 1

    a.title = "first writer"
    incident_store.save(a)
    assert a.version == 2

    b.title = "second writer"
    with pytest.raises(ConcurrentModificationError):
        incident_store.save(b)
    assert incident_store.get("inc-1").title == "first writer"


def test_creating_an_existing_incident_is_rejected(backend):
    incident_store.save(_new_incident())
    with pytest.raises(ConcurrentModificationError):
        incident_store.save(_new_incident())


def test_concurrent_updates_are_all_kept(backend):
    incident_store.save(_new_incident(logs=[_log(i) for i in range(50)]))
    writers = 10
    start = threading.Barrier(writers)
    errors = []

    def writer(n):
        start.wait(timeout=10)
        try:
            incident_store.update("inc-1", lambda inc: inc.metadata.__setitem__(f"writer_{n}", True))
        except Exception as exc:  # surfaced below
            errors.append(repr(exc))

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    final = incident_store.get("inc-1")
    assert set(final.metadata) == {f"writer_{n}" for n in range(writers)}  # no write lost
    assert final.version == 1 + writers
    assert len(final.logs) == 50


def test_conflicting_raw_save_returns_409(client):
    from unittest.mock import patch

    incident_store.clear()
    iid = client.post("/api/v1/incidents", json={"title": "x", "severity": "ERROR"}).json()["incident_id"]
    with patch.object(incident_store, "update", side_effect=ConcurrentModificationError("changed")):
        r = client.patch(f"/api/v1/incidents/{iid}/status?new_status=RESOLVED")
    assert r.status_code == 409
    assert "retry" in r.json()["detail"].lower()


def test_update_missing_incident_raises_keyerror(backend):
    with pytest.raises(KeyError):
        incident_store.update("nope", lambda i: None)


def test_returned_incidents_are_copies(backend):
    incident_store.save(_new_incident())
    got = incident_store.get("inc-1")
    got.title = "changed but not saved"
    got.logs.append(_log(99))
    stored = incident_store.get("inc-1")
    assert stored.title == "Payments failing"
    assert stored.logs == []


# --- Evidence -----------------------------------------------------------------------

def test_evidence_round_trips_in_order_and_is_deduplicated(backend):
    build_context(BuildContextRequest(incident_id="inc-2", title="t", logs=[_log(2), _log(1), _log(2)]))
    build_context(BuildContextRequest(incident_id="inc-2", logs=[_log(1), _log(3)], traces=[_span(1)]))

    incident = incident_store.get("inc-2")
    assert [l.body for l in incident.logs] == ["Payment FAILED #2", "Payment FAILED #1", "Payment FAILED #3"]
    assert len(incident.traces) == 1
    assert incident.version == 2


def test_evidence_lives_in_its_own_table(backend):
    if backend == "memory":
        pytest.skip("database layout only")
    incident_store.save(_new_incident(logs=[_log(1), _log(2)]))
    incident_store.update("inc-1", lambda i: i.metadata.update(touched=True))  # re-save writes no duplicates

    engine = db_session.get_engine()
    with engine.connect() as conn:
        kinds = conn.execute(sa.text(
            "SELECT kind, count(*) FROM incident_evidence WHERE incident_id = 'inc-1' GROUP BY kind ORDER BY kind"
        )).all()
        payload = conn.execute(sa.text("SELECT payload FROM incidents WHERE incident_id = 'inc-1'")).scalar_one()
    assert [tuple(k) for k in kinds] == [("log", 2), ("metric", 1), ("span", 1)]
    payload = payload if isinstance(payload, dict) else __import__("json").loads(payload)
    assert "logs" not in payload and "traces" not in payload and payload["metadata"] == {"touched": True}


def test_list_page_and_summaries_with_evidence(backend):
    incident_store.save(_new_incident("a", logs=[_log(1)]))
    incident_store.save(_new_incident("b", logs=[_log(2), _log(3)]))
    items, total = incident_store.list_page(limit=10)
    assert total == 2
    assert {i.incident_id: len(i.logs) for i in items} == {"a": 1, "b": 2}
    summaries, _ = incident_store.list_summaries()
    assert {s.incident_id for s in summaries} == {"a", "b"}


def test_delete_removes_evidence(backend):
    incident_store.save(_new_incident(logs=[_log(1)]))
    assert incident_store.delete("inc-1") is True
    assert incident_store.get("inc-1") is None
    if backend != "memory":
        with db_session.get_engine().connect() as conn:
            assert conn.execute(sa.text("SELECT count(*) FROM incident_evidence")).scalar_one() == 0


# --- Migrations ---------------------------------------------------------------------

def _legacy_database(url):
    """Schema + data exactly as the pre-migration create_all() code produced them."""
    _reset_db_schema(url)
    metadata = sa.MetaData()
    JSONType = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")
    legacy = sa.Table(
        "incidents", metadata,
        sa.Column("incident_id", sa.String(64), primary_key=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, index=True),
        sa.Column("severity", sa.String(32), nullable=False, index=True),
        sa.Column("payload", JSONType, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
    )
    engine = sa.create_engine(url)
    metadata.create_all(engine)
    old = _new_incident("legacy-1", logs=[_log(1), _log(2), _log(1)])  # contains a duplicate
    with engine.begin() as conn:
        conn.execute(legacy.insert().values(
            incident_id="legacy-1", title=old.title, status="OPEN", severity="ERROR",
            payload=old.model_dump(mode="json", exclude={"version"}),
        ))
    engine.dispose()


@pytest.mark.parametrize("db", ["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(not PG_URL, reason="TEST_DATABASE_URL not set"))])
def test_legacy_database_is_stamped_migrated_and_reversible(db, tmp_path, monkeypatch):
    from app.db.migrate import downgrade

    url = f"sqlite:///{tmp_path / 'legacy.db'}" if db == "sqlite" else PG_URL
    _legacy_database(url)
    _use_database(monkeypatch, url)
    db_session.init_db()  # stamps 0001, upgrades to head
    engine = db_session.get_engine()
    try:
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one() == "0002"

        migrated = incident_store.get("legacy-1")
        assert migrated.version == 1
        assert [l.body for l in migrated.logs] == ["Payment FAILED #1", "Payment FAILED #2"]  # deduplicated
        assert len(migrated.metrics) == 1 and len(migrated.traces) == 1

        # Migrated evidence keys match runtime keys: re-adding the same log is a no-op
        build_context(BuildContextRequest(incident_id="legacy-1", logs=[_log(1)]))
        assert len(incident_store.get("legacy-1").logs) == 2

        downgrade(engine, "0001")
        with engine.connect() as conn:
            payload = conn.execute(sa.text("SELECT payload FROM incidents")).scalar_one()
            payload = payload if isinstance(payload, dict) else __import__("json").loads(payload)
            assert len(payload["logs"]) == 2 and len(payload["traces"]) == 1
            assert not sa.inspect(conn).has_table("incident_evidence")

        db_session.init_db()  # and forward again
        assert len(incident_store.get("legacy-1").logs) == 2
    finally:
        engine.dispose()


def test_fresh_database_migrates_from_empty(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'fresh.db'}"
    _use_database(monkeypatch, url)
    db_session.init_db()
    db_session.init_db()  # idempotent
    with db_session.get_engine().connect() as conn:
        insp = sa.inspect(conn)
        assert insp.has_table("incidents") and insp.has_table("incident_evidence")
        assert "version" in {c["name"] for c in insp.get_columns("incidents")}
    db_session._engine.dispose()
