"""End-to-end incident intelligence workflow (Week 15)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.core.dependencies import AIProviderDep, SettingsDep
from app.core.logging import get_logger
from app.schemas.incident import BuildContextRequest, ScenarioType
from app.schemas.telemetry import (
    LogRecord,
    MetricPoint,
    MetricType,
    Resource,
    SeverityLevel,
    Span,
    SpanStatus,
)
from app.services.context_builder import build_context
from app.services.correlation import correlate_incident
from app.services.rca_engine import RCAEngine
from app.services.runbook_engine import RunbookEngine
from app.services.telemetry_query import TelemetryQueryService

router = APIRouter()
logger = get_logger(__name__)


class E2ERequest(BaseModel):
    title: str = "E2E payment failure drill"
    simulate_payment_failure: bool = True
    run_correlation: bool = True
    run_rca: bool = True
    run_runbook: bool = True
    check_observability: bool = True


class E2EStep(BaseModel):
    name: str
    ok: bool
    detail: str


class E2EResponse(BaseModel):
    ok: bool
    incident_id: Optional[str] = None
    steps: List[E2EStep]
    provider: str
    summary: str


@router.post(
    "/run",
    response_model=E2EResponse,
    summary="Run full incident intelligence E2E workflow",
)
async def run_e2e(
    payload: E2ERequest,
    provider: AIProviderDep,
    settings: SettingsDep,
) -> E2EResponse:
    """Executes: synthetic evidence → context → correlate → RCA → runbook
    (+ optional observability health check). Validates the full pipeline.
    """
    steps: List[E2EStep] = []
    incident_id = None

    # 0. Observability health (non-fatal)
    if payload.check_observability:
        try:
            health = await TelemetryQueryService(settings).health()
            ok = any(health.values())
            steps.append(
                E2EStep(
                    name="observability_health",
                    ok=ok,
                    detail=str(health),
                )
            )
        except Exception as exc:
            steps.append(E2EStep(name="observability_health", ok=False, detail=str(exc)))

    # 1. Build incident context with synthetic failure evidence
    now = datetime.now(timezone.utc)
    pay = Resource(service_name="payment-service", service_version="0.1.0", environment="development")
    order = Resource(service_name="order-service", service_version="0.1.0", environment="development")
    trace_id = "e2e" + "0" * 28

    logs = [
        LogRecord(
            timestamp=now,
            severity=SeverityLevel.ERROR,
            body="Payment FAILED gateway timeout (E2E drill)",
            attributes={"source": "e2e"},
            resource=pay,
            trace_id=trace_id,
            span_id="spanpay01",
        ),
        LogRecord(
            timestamp=now,
            severity=SeverityLevel.ERROR,
            body="Order PAYMENT_FAILED order_id=e2e-ord-1",
            attributes={"source": "e2e"},
            resource=order,
            trace_id=trace_id,
            span_id="spanord01",
        ),
    ]
    metrics = [
        MetricPoint(
            name="payments_total",
            type=MetricType.COUNTER,
            value=5.0,
            timestamp=now,
            attributes={"status": "failed"},
            resource=pay,
        )
    ]
    traces = [
        Span(
            trace_id=trace_id,
            span_id="spanord01",
            parent_span_id=None,
            name="order.create",
            start_time=now,
            end_time=now,
            status=SpanStatus.ERROR,
            attributes={},
            resource=order,
        ),
        Span(
            trace_id=trace_id,
            span_id="spanpay01",
            parent_span_id="spanord01",
            name="payment.process",
            start_time=now,
            end_time=now,
            status=SpanStatus.ERROR,
            attributes={},
            resource=pay,
        ),
    ]

    try:
        incident = build_context(
            BuildContextRequest(
                title=payload.title,
                severity=SeverityLevel.ERROR,
                affected_services=["order-service", "payment-service"],
                logs=logs,
                metrics=metrics,
                traces=traces,
                scenario=ScenarioType.PAYMENT_TIMEOUT if payload.simulate_payment_failure else ScenarioType.MANUAL,
                notes="Created by E2E workflow",
            )
        )
        incident_id = incident.incident_id
        steps.append(
            E2EStep(
                name="build_context",
                ok=True,
                detail=f"incident_id={incident_id}, evidence={incident.evidence_summary}",
            )
        )
    except Exception as exc:
        steps.append(E2EStep(name="build_context", ok=False, detail=str(exc)))
        return E2EResponse(
            ok=False,
            steps=steps,
            provider=provider.name,
            summary="E2E failed at context build",
        )

    # 2. Correlate
    if payload.run_correlation:
        try:
            corr = correlate_incident(incident_id)
            steps.append(
                E2EStep(
                    name="correlate",
                    ok=True,
                    detail=f"score={corr.severity.score}, anomalies={len(corr.anomalies)}, links={len(corr.links)}",
                )
            )
        except Exception as exc:
            steps.append(E2EStep(name="correlate", ok=False, detail=str(exc)))

    # 3. RCA (exercises resilient AI provider)
    if payload.run_rca:
        try:
            rca = await RCAEngine(provider).analyze(incident_id, run_correlation=False)
            steps.append(
                E2EStep(
                    name="rca",
                    ok=True,
                    detail=f"cause={rca.rca.primary_cause[:120]}, confidence={rca.rca.confidence}, provider={rca.provider}",
                )
            )
        except Exception as exp:
            steps.append(E2EStep(name="rca", ok=False, detail=str(exp)))

    # 4. Runbook
    if payload.run_runbook:
        try:
            rb = await RunbookEngine(provider).generate(incident_id)
            steps.append(
                E2EStep(
                    name="runbook",
                    ok=True,
                    detail=f"title={rb.runbook.title}, steps={len(rb.runbook.steps)}, confidence={rb.runbook.confidence}",
                )
            )
        except Exception as exp:
            steps.append(E2EStep(name="runbook", ok=False, detail=str(exp)))

    ok = all(s.ok for s in steps if s.name in ("build_context", "correlate", "rca", "runbook"))
    summary = (
        f"E2E {'PASSED' if ok else 'FAILED'} for incident {incident_id}. "
        f"Provider={provider.name}. "
        + "; ".join(f"{s.name}={'ok' if s.ok else 'FAIL'}" for s in steps)
    )
    logger.info(summary)
    return E2EResponse(
        ok=ok,
        incident_id=incident_id,
        steps=steps,
        provider=provider.name,
        summary=summary,
    )
