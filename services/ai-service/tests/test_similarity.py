import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.providers.mock_provider import MockAIProvider
from app.schemas.incident import BuildContextRequest, IncidentStatus
from app.schemas.telemetry import LogRecord, MetricPoint, Resource, SeverityLevel, Span, SpanStatus
from app.services import incident_store
from app.services.context_builder import build_context
from app.services.rca_engine import RCAEngine
from app.services.similarity import log_template, signature, similarity

T0 = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)


def _res(svc):
    return Resource(service_name=svc)


def gateway_timeout_incident(day: int, title="Checkout failing"):
    """Same failure as other calls, different IDs / timestamps / amounts."""
    t = T0 + timedelta(days=day)
    logs, spans = [], []
    for i in range(3):
        tid = uuid.uuid4().hex
        logs.append(LogRecord(timestamp=t, severity=SeverityLevel.ERROR, resource=_res("payment-service"),
                              body=f"Gateway request to https://gw.example/charge timed out after {5000 + i}ms "
                                   f"payment_id={uuid.uuid4()} amount={10 + i}.50", trace_id=tid))
        logs.append(LogRecord(timestamp=t, severity=SeverityLevel.ERROR, resource=_res("order-service"),
                              body=f"Order PAYMENT_FAILED order_id={uuid.uuid4()} reason=payment timeout"))
        spans.append(Span(trace_id=tid, span_id=uuid.uuid4().hex[:16], name="payment.process", start_time=t,
                          end_time=t, status=SpanStatus.ERROR, resource=_res("payment-service")))
    metrics = [MetricPoint(name="payments_total", type="COUNTER", value=40, timestamp=t,
                           attributes={"status": "failed"}, resource=_res("payment-service"))]
    return build_context(BuildContextRequest(title=title, affected_services=["order-service", "payment-service"],
                                             logs=logs, metrics=metrics, traces=spans))


def db_pool_incident():
    logs = [LogRecord(timestamp=T0, severity=SeverityLevel.ERROR, resource=_res("inventory-service"),
                      body=f"HikariPool-1 - Connection is not available, request timed out after 30000ms id={i}")
            for i in range(4)]
    return build_context(BuildContextRequest(title="Inventory API 500s", affected_services=["inventory-service"], logs=logs))


@pytest.fixture(autouse=True)
def clean():
    incident_store.clear()


def test_log_template_strips_ids_numbers_and_values():
    a = log_template("Order PAYMENT_FAILED order_id=3f1c9a0e-1111-2222-3333-444455556666 amount=19.99 after 5000ms")
    b = log_template("Order PAYMENT_FAILED order_id=9b8a7c6d-aaaa-bbbb-cccc-ddddeeeeffff amount=5.00 after 30ms")
    assert a == b == "order payment_failed order_id=* amount=* after #ms"


def test_signature_features_and_normalisation():
    sig = signature(gateway_timeout_incident(0))
    assert "svc:payment-service" in sig
    assert "span:payment-service/payment.process" in sig
    assert any(k.startswith("log:payment-service:gateway request") for k in sig)
    assert any(k.startswith("anomaly:error_log_burst") for k in sig)
    assert abs(sum(v * v for v in sig.values()) - 1.0) < 1e-3


def test_same_failure_matches_and_different_failure_does_not():
    a, b, c = gateway_timeout_incident(0), gateway_timeout_incident(9, "Payments down again"), db_pool_incident()
    same, shared = similarity(signature(a), signature(b))
    different, _ = similarity(signature(a), signature(c))
    assert same > 0.8
    assert "span:payment-service/payment.process" in shared
    assert different < 0.1


def test_endpoint_ranks_and_explains(client):
    past = gateway_timeout_incident(0)
    other = db_pool_incident()
    current = gateway_timeout_incident(30)

    body = client.get(f"/api/v1/incidents/{current.incident_id}/similar").json()
    ids = [s["incident_id"] for s in body["similar"]]
    assert ids == [past.incident_id]  # self excluded, unrelated incident below min_score
    assert other.incident_id not in ids
    assert body["similar"][0]["shared_features"]
    assert client.get("/api/v1/incidents/nope/similar").status_code == 404


@pytest.mark.asyncio
async def test_rca_gets_past_incidents_as_context():
    past = gateway_timeout_incident(0)
    await RCAEngine(MockAIProvider()).analyze(past.incident_id)  # past now has an RCA cause
    incident_store.update(past.incident_id, lambda i: setattr(i, "status", IncidentStatus.RESOLVED))
    current = gateway_timeout_incident(30)

    prompts = []

    class Recording(MockAIProvider):
        async def generate_structured(self, prompt, response_model, system_instruction=None):
            prompts.append(prompt)
            return await super().generate_structured(prompt, response_model, system_instruction)

    resp = await RCAEngine(Recording()).analyze(current.incident_id)
    assert [s.incident_id for s in resp.similar_incidents] == [past.incident_id]
    past_cause = incident_store.get(past.incident_id).metadata["rca_primary_cause"]
    block = prompts[0].split("=== SIMILAR PAST INCIDENTS")[1]
    assert past.incident_id in block and "[RESOLVED]" in block and past_cause in block
    assert "context only, not evidence" in prompts[0]


@pytest.mark.asyncio
async def test_rca_without_history_says_none_found():
    current = gateway_timeout_incident(0)
    prompts = []

    class Recording(MockAIProvider):
        async def generate_structured(self, prompt, response_model, system_instruction=None):
            prompts.append(prompt)
            return await super().generate_structured(prompt, response_model, system_instruction)

    resp = await RCAEngine(Recording()).analyze(current.incident_id)
    assert resp.similar_incidents == []
    assert "(none found)" in prompts[0]


def test_signatures_are_stored_and_backfilled_in_the_database(tmp_path, monkeypatch):
    from app.core.config import get_settings
    from app.db import session as db_session

    monkeypatch.setattr(get_settings(), "DATABASE_URL", f"sqlite:///{tmp_path / 'sim.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_SessionLocal", None)
    db_session.init_db()
    try:
        past = gateway_timeout_incident(0)
        current = gateway_timeout_incident(30)
        engine = db_session.get_engine()
        with engine.begin() as conn:
            assert conn.execute(sa.text("SELECT count(*) FROM incidents WHERE signature IS NOT NULL")).scalar_one() == 2
            # simulate a row stored before migration 0003
            conn.execute(sa.text("UPDATE incidents SET signature = NULL WHERE incident_id = :i"), {"i": past.incident_id})

        assert [s.incident_id for s in incident_store.find_similar(current.incident_id)] == [past.incident_id]
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT count(*) FROM incidents WHERE signature IS NULL")).scalar_one() == 0
            version = conn.execute(sa.text("SELECT version FROM incidents WHERE incident_id = :i"),
                                   {"i": past.incident_id}).scalar_one()
        assert version == 1  # backfill is derived data: no version bump
    finally:
        db_session._engine.dispose()
