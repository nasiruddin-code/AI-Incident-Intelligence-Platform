"""Build IncidentContext from telemetry evidence (Week 8)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from app.core.logging import get_logger
from app.schemas.incident import (
    BuildContextRequest,
    IncidentContext,
    IncidentStatus,
    ScenarioType,
    TimeWindow,
)
from app.schemas.telemetry import LogRecord, MetricPoint, SeverityLevel, Span, SpanStatus
from app.services import incident_store
from app.services.evidence import merge_new

logger = get_logger(__name__)


def _derive_severity(
    logs: List[LogRecord],
    traces: List[Span],
    explicit: SeverityLevel,
) -> SeverityLevel:
    """Bump severity if evidence contains ERROR/FATAL or error spans."""
    rank = {
        SeverityLevel.TRACE: 0,
        SeverityLevel.DEBUG: 1,
        SeverityLevel.INFO: 2,
        SeverityLevel.WARN: 3,
        SeverityLevel.ERROR: 4,
        SeverityLevel.FATAL: 5,
    }
    best = explicit
    for log in logs:
        if rank[log.severity] > rank[best]:
            best = log.severity
    for span in traces:
        if span.status == SpanStatus.ERROR and rank[SeverityLevel.ERROR] > rank[best]:
            best = SeverityLevel.ERROR
    return best


def _derive_services(
    explicit: List[str],
    logs: List[LogRecord],
    metrics: List[MetricPoint],
    traces: List[Span],
) -> List[str]:
    services = set(explicit)
    for log in logs:
        services.add(log.resource.service_name)
    for m in metrics:
        services.add(m.resource.service_name)
    for s in traces:
        services.add(s.resource.service_name)
    return sorted(services) if services else ["unknown"]


def _derive_time_window(
    explicit: Optional[TimeWindow],
    logs: List[LogRecord],
    metrics: List[MetricPoint],
    traces: List[Span],
) -> TimeWindow:
    if explicit:
        return explicit
    timestamps: list[datetime] = []
    timestamps.extend(log.timestamp for log in logs)
    timestamps.extend(m.timestamp for m in metrics)
    timestamps.extend(s.start_time for s in traces)
    timestamps.extend(s.end_time for s in traces)
    now = datetime.now(timezone.utc)
    if not timestamps:
        return TimeWindow(start=now, end=now)
    return TimeWindow(start=min(timestamps), end=max(timestamps))


def _evidence_summary(
    logs: List[LogRecord],
    metrics: List[MetricPoint],
    traces: List[Span],
    affected: List[str],
) -> str:
    error_logs = [l for l in logs if l.severity in (SeverityLevel.ERROR, SeverityLevel.FATAL)]
    error_spans = [s for s in traces if s.status == SpanStatus.ERROR]
    parts = [
        f"{len(logs)} log(s)",
        f"{len(metrics)} metric(s)",
        f"{len(traces)} span(s)",
        f"services=[{', '.join(affected)}]",
    ]
    if error_logs:
        parts.append(f"{len(error_logs)} error log(s)")
    if error_spans:
        parts.append(f"{len(error_spans)} error span(s)")
    return "; ".join(parts)


def build_context(req: BuildContextRequest) -> IncidentContext:
    """Assemble (or enrich) an IncidentContext from provided telemetry.

    Evidence already on the incident (same content) is not added twice.
    """
    logs = merge_new([], req.logs)
    metrics = merge_new([], req.metrics)
    traces = merge_new([], req.traces)

    affected = _derive_services(req.affected_services, logs, metrics, traces)
    severity = _derive_severity(logs, traces, req.severity)
    window = _derive_time_window(req.time_window, logs, metrics, traces)
    summary = _evidence_summary(logs, metrics, traces, affected)

    def enrich(existing: IncidentContext) -> None:
        existing.logs = merge_new(existing.logs, logs)
        existing.metrics = merge_new(existing.metrics, metrics)
        existing.traces = merge_new(existing.traces, traces)
        existing.affected_services = _derive_services(
            existing.affected_services, existing.logs, existing.metrics, existing.traces
        )
        existing.severity = _derive_severity(existing.logs, existing.traces, existing.severity)
        existing.time_window = _derive_time_window(
            existing.time_window, existing.logs, existing.metrics, existing.traces
        )
        existing.evidence_summary = _evidence_summary(
            existing.logs, existing.metrics, existing.traces, existing.affected_services
        )
        if req.notes:
            existing.notes = (existing.notes or "") + "\n" + req.notes
        if req.title:
            existing.title = req.title

    if req.incident_id:
        try:
            existing, _ = incident_store.update(req.incident_id, enrich)
        except KeyError:
            pass  # no such incident yet: create it with the requested ID below
        else:
            logger.info("Enriched incident %s — %s", existing.incident_id, existing.evidence_summary)
            return existing

    incident_id = req.incident_id or str(uuid.uuid4())
    title = req.title or f"Incident affecting {', '.join(affected)}"
    incident = IncidentContext(
        incident_id=incident_id,
        title=title,
        status=IncidentStatus.OPEN,
        severity=severity,
        affected_services=affected,
        time_window=window,
        scenario=req.scenario,
        logs=logs,
        metrics=metrics,
        traces=traces,
        evidence_summary=summary,
        notes=req.notes,
    )
    incident_store.save(incident)
    logger.info("Built incident context %s — %s", incident_id, summary)
    return incident


def build_from_scenario_results(
    scenario: ScenarioType,
    title: str,
    affected_services: List[str],
    logs: List[LogRecord],
    metrics: List[MetricPoint],
    traces: List[Span],
    notes: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> IncidentContext:
    """Helper used by the scenario runner."""
    req = BuildContextRequest(
        title=title,
        severity=SeverityLevel.ERROR,
        affected_services=affected_services,
        logs=logs,
        metrics=metrics,
        traces=traces,
        notes=notes,
        scenario=scenario,
    )
    incident = build_context(req)
    if metadata:
        incident, _ = incident_store.update(
            incident.incident_id, lambda inc: inc.metadata.update(metadata)
        )
    return incident
