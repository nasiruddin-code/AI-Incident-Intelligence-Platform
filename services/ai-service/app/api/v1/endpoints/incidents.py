"""Incident management + scenario endpoints (Week 8)."""
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status

from starlette.concurrency import run_in_threadpool
from app.core.logging import get_logger
from app.core.dependencies import RCAEngineDep, RunbookEngineDep, SettingsDep
from app.services.telemetry_enrichment import TelemetryEnrichmentService
from app.schemas.rca import RCAResponse
from app.schemas.runbook import RunbookResponse
from app.schemas.incident import (
    BuildContextRequest,
    CorrelationResult,
    EnrichmentResult,
    EnrichRequest,
    IncidentContext,
    IncidentCreate,
    IncidentListResponse,
    IncidentStatus,
    IncidentSummaryListResponse,
    ScenarioRunRequest,
    ScenarioRunResult,
    TimeWindow,
)
from app.schemas.telemetry import SeverityLevel
from app.services import incident_store
from app.services.context_builder import build_context
from app.services.scenario_runner import run_scenario
from app.services.correlation import correlate_incident
from app.services.notifier import notify, rca_details, runbook_details

from datetime import datetime, timezone
import uuid

router = APIRouter()
logger = get_logger(__name__)


@router.post(
    "",
    response_model=IncidentContext,
    status_code=status.HTTP_201_CREATED,
    summary="Create an empty incident shell",
)
def create_incident(payload: IncidentCreate, background: BackgroundTasks) -> IncidentContext:
    now = datetime.now(timezone.utc)
    incident = IncidentContext(
        incident_id=str(uuid.uuid4()),
        title=payload.title,
        status=IncidentStatus.OPEN,
        severity=payload.severity,
        affected_services=payload.affected_services,
        time_window=TimeWindow(start=now, end=now),
        scenario=payload.scenario,
        notes=payload.notes,
        evidence_summary="No evidence attached yet",
    )
    incident_store.save(incident)
    logger.info("Created incident %s: %s", incident.incident_id, incident.title)
    background.add_task(notify, "incident_created", incident, {"source": "api"})
    return incident


@router.get("", response_model=IncidentListResponse, summary="List incidents (paginated)")
def list_incidents(
    status_filter: Optional[IncidentStatus] = Query(None, alias="status"),
    severity: Optional[SeverityLevel] = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> IncidentListResponse:
    """Full incidents (including evidence), newest first.

    Prefer GET /incidents/summaries for dashboards and lists.
    """
    items, total = incident_store.list_page(
        status=status_filter, severity=severity, limit=limit, offset=offset
    )
    return IncidentListResponse(
        count=len(items), total=total, limit=limit, offset=offset, incidents=items
    )


@router.get(
    "/summaries",
    response_model=IncidentSummaryListResponse,
    summary="List incident summaries (no evidence, paginated)",
)
def list_incident_summaries(
    status_filter: Optional[IncidentStatus] = Query(None, alias="status"),
    severity: Optional[SeverityLevel] = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> IncidentSummaryListResponse:
    items, total = incident_store.list_summaries(
        status=status_filter, severity=severity, limit=limit, offset=offset
    )
    return IncidentSummaryListResponse(
        count=len(items), total=total, limit=limit, offset=offset, incidents=items
    )


@router.get("/{incident_id}", response_model=IncidentContext, summary="Get incident by ID")
def get_incident(incident_id: str) -> IncidentContext:
    incident = incident_store.get(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail=f"Incident {incident_id} not found")
    return incident


@router.post(
    "/context",
    response_model=IncidentContext,
    status_code=status.HTTP_201_CREATED,
    summary="Build or enrich Incident Context from telemetry evidence",
)
def build_incident_context(
    payload: BuildContextRequest, background: BackgroundTasks
) -> IncidentContext:
    """Accept logs/metrics/traces and assemble a normalized IncidentContext.

    If `incident_id` is provided and exists, evidence is merged into it.
    Otherwise a new incident is created.
    """
    is_new = not (payload.incident_id and incident_store.get(payload.incident_id))
    incident = build_context(payload)
    if is_new:
        background.add_task(notify, "incident_created", incident, {"source": "api"})
    return incident


@router.post(
    "/scenarios/run",
    response_model=ScenarioRunResult,
    status_code=status.HTTP_201_CREATED,
    summary="Run a reproducible failure scenario",
)
async def run_failure_scenario(
    payload: ScenarioRunRequest, background: BackgroundTasks
) -> ScenarioRunResult:
    """Fire orders against order-service under a known failure pattern and
    return a fully populated IncidentContext with synthetic telemetry evidence.

    The scenario's failure settings are applied to payment-service through its
    /fault-injection API before the orders are sent and restored afterwards
    (apply_failure_config / restore_failure_config):
    - payment_timeout:        FAILURE_RATE=1.0
    - payment_high_latency:   LATENCY_MS=2000
    - payment_cascade_failure: FAILURE_RATE=0.6 LATENCY_MS=800
    """
    try:
        result = await run_scenario(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Scenario run failed")
        raise HTTPException(status_code=502, detail=f"Scenario execution failed: {exc}") from exc
    background.add_task(
        notify, "incident_created", result.incident, {"source": f"scenario:{payload.scenario.value}"}
    )
    return result


@router.patch(
    "/{incident_id}/status",
    response_model=IncidentContext,
    summary="Update incident status",
)
def update_status(
    incident_id: str, new_status: IncidentStatus, background: BackgroundTasks
) -> IncidentContext:
    def set_status(incident: IncidentContext) -> bool:
        was_resolved = incident.status in (IncidentStatus.RESOLVED, IncidentStatus.CLOSED)
        incident.status = new_status
        return was_resolved

    try:
        incident, was_resolved = incident_store.update(incident_id, set_status)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Incident {incident_id} not found")
    if new_status == IncidentStatus.RESOLVED and not was_resolved:
        background.add_task(notify, "incident_resolved", incident, {"source": "api"})
    return incident


@router.post(
    "/{incident_id}/correlate",
    response_model=CorrelationResult,
    summary="Correlate evidence + detect anomalies + score severity",
)
def correlate(incident_id: str) -> CorrelationResult:
    """Run Week 9 correlation pipeline on a stored incident.

    - Links logs ↔ spans by trace_id
    - Links multi-signal evidence by service
    - Builds a cross-service graph from parent/child spans
    - Detects error bursts, latency spikes, error-rate spikes
    - Computes a 0–100 severity score with factor breakdown
    """
    try:
        return correlate_incident(incident_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Incident {incident_id} not found")


@router.post(
    "/{incident_id}/enrich",
    response_model=EnrichmentResult,
    summary="Pull live logs/metrics/traces from Loki, Prometheus and Tempo",
)
async def enrich_incident(
    incident_id: str,
    settings: SettingsDep,
    payload: Optional[EnrichRequest] = None,
) -> EnrichmentResult:
    """Fetch evidence for the incident's services and time window from the
    observability stack and merge it into the incident. Safe to re-run:
    evidence already on the incident is skipped. A backend being down is
    reported in `sources` rather than failing the request.
    """
    payload = payload or EnrichRequest()
    try:
        return await TelemetryEnrichmentService(settings).enrich(
            incident_id,
            lookback_seconds=payload.lookback_seconds,
            services=payload.services,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Incident {incident_id} not found")


@router.post(
    "/{incident_id}/rca",
    response_model=RCAResponse,
    summary="Run AI-assisted Root Cause Analysis",
)
async def run_rca(
    incident_id: str,
    rca_engine: RCAEngineDep,
    background: BackgroundTasks,
    run_correlation: bool = True,
) -> RCAResponse:
    """Week 10: AI-assisted RCA using Incident Context + evidence.

    By default runs correlation first so the model sees anomaly findings and
    severity scoring. Set run_correlation=false to skip.
    """
    try:
        result = await rca_engine.analyze(incident_id, run_correlation=run_correlation)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Incident {incident_id} not found")
    incident = await run_in_threadpool(incident_store.get, incident_id)
    if incident:
        background.add_task(notify, "rca_completed", incident, rca_details(result))
    return result


@router.post(
    "/{incident_id}/runbook",
    response_model=RunbookResponse,
    summary="Generate AI runbook + remediation recommendations",
)
async def generate_runbook(
    incident_id: str,
    runbook_engine: RunbookEngineDep,
    background: BackgroundTasks,
) -> RunbookResponse:
    """Week 11: AI-generated runbook and remediation plan.

    Uses prior RCA (if available on the incident) plus logs/metrics/traces
    to produce ordered steps, remediation actions with risk/confidence,
    verification checks, and escalation guidance.
    """
    try:
        result = await runbook_engine.generate(incident_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Incident {incident_id} not found")
    incident = await run_in_threadpool(incident_store.get, incident_id)
    if incident:
        background.add_task(notify, "runbook_completed", incident, runbook_details(result))
    return result

