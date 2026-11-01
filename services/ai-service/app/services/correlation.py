"""Correlate logs, metrics, and traces within an IncidentContext (Week 9).

Also runs basic anomaly detection and produces a numeric severity score.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Set, Tuple

from app.core.logging import get_logger
from app.schemas.incident import (
    AnomalyFinding,
    CorrelationLink,
    CorrelationResult,
    IncidentContext,
    SeverityScore,
)
from app.schemas.telemetry import LogRecord, MetricPoint, SeverityLevel, Span, SpanStatus
from app.services import incident_store

logger = get_logger(__name__)

# Severity rank for comparisons
_SEV_RANK = {
    SeverityLevel.TRACE: 0,
    SeverityLevel.DEBUG: 1,
    SeverityLevel.INFO: 2,
    SeverityLevel.WARN: 3,
    SeverityLevel.ERROR: 4,
    SeverityLevel.FATAL: 5,
}

_CORRELATION_MARKER = " | correlated: "


def _correlate_by_trace_id(
    logs: List[LogRecord],
    traces: List[Span],
) -> Tuple[List[CorrelationLink], List[str]]:
    """Link logs ↔ spans that share a trace_id."""
    links: List[CorrelationLink] = []
    logs_by_trace: Dict[str, List[LogRecord]] = defaultdict(list)
    spans_by_trace: Dict[str, List[Span]] = defaultdict(list)

    for log in logs:
        if log.trace_id:
            logs_by_trace[log.trace_id].append(log)
    for span in traces:
        spans_by_trace[span.trace_id].append(span)

    all_trace_ids = set(logs_by_trace) | set(spans_by_trace)
    for tid in all_trace_ids:
        for log in logs_by_trace.get(tid, []):
            for span in spans_by_trace.get(tid, []):
                links.append(
                    CorrelationLink(
                        link_type="trace_id",
                        source_kind="log",
                        source_ref=log.body[:80],
                        target_kind="span",
                        target_ref=f"{span.name} ({span.span_id[:8]})",
                        confidence=1.0,
                        detail=f"shared trace_id={tid[:16]}…",
                    )
                )
    return links, sorted(all_trace_ids)


def _correlate_by_service(
    logs: List[LogRecord],
    metrics: List[MetricPoint],
    traces: List[Span],
) -> Tuple[List[CorrelationLink], Dict[str, List[str]]]:
    """Link evidence that belongs to the same service; build service graph from parent spans."""
    links: List[CorrelationLink] = []
    services_logs: Dict[str, int] = defaultdict(int)
    services_metrics: Dict[str, int] = defaultdict(int)
    services_spans: Dict[str, int] = defaultdict(int)

    for log in logs:
        services_logs[log.resource.service_name] += 1
    for m in metrics:
        services_metrics[m.resource.service_name] += 1
    for s in traces:
        services_spans[s.resource.service_name] += 1

    all_services = set(services_logs) | set(services_metrics) | set(services_spans)
    for svc in all_services:
        kinds = []
        if services_logs[svc]:
            kinds.append(f"{services_logs[svc]} log(s)")
        if services_metrics[svc]:
            kinds.append(f"{services_metrics[svc]} metric(s)")
        if services_spans[svc]:
            kinds.append(f"{services_spans[svc]} span(s)")
        if len(kinds) >= 2:
            links.append(
                CorrelationLink(
                    link_type="service",
                    source_kind="multi",
                    source_ref=svc,
                    target_kind="multi",
                    target_ref=", ".join(kinds),
                    confidence=0.9,
                    detail=f"Evidence co-located on service {svc}",
                )
            )

    # Service graph from parent/child spans across services
    span_by_id = {s.span_id: s for s in traces}
    graph: Dict[str, Set[str]] = defaultdict(set)
    for span in traces:
        if span.parent_span_id and span.parent_span_id in span_by_id:
            parent = span_by_id[span.parent_span_id]
            if parent.resource.service_name != span.resource.service_name:
                graph[parent.resource.service_name].add(span.resource.service_name)
                links.append(
                    CorrelationLink(
                        link_type="service",
                        source_kind="span",
                        source_ref=f"{parent.resource.service_name}/{parent.name}",
                        target_kind="span",
                        target_ref=f"{span.resource.service_name}/{span.name}",
                        confidence=0.95,
                        detail="cross-service parent→child span",
                    )
                )

    return links, {k: sorted(v) for k, v in graph.items()}


def _detect_anomalies(
    logs: List[LogRecord],
    metrics: List[MetricPoint],
    traces: List[Span],
) -> List[AnomalyFinding]:
    findings: List[AnomalyFinding] = []

    # Error log burst
    error_logs = [l for l in logs if l.severity in (SeverityLevel.ERROR, SeverityLevel.FATAL)]
    if error_logs:
        by_svc: Dict[str, int] = defaultdict(int)
        for l in error_logs:
            by_svc[l.resource.service_name] += 1
        for svc, count in by_svc.items():
            score = min(1.0, count / 5.0)
            findings.append(
                AnomalyFinding(
                    anomaly_type="error_log_burst",
                    service=svc,
                    description=f"{count} error/fatal log(s) on {svc}",
                    score=score,
                    evidence_count=count,
                )
            )

    # Error span burst
    error_spans = [s for s in traces if s.status == SpanStatus.ERROR]
    if error_spans:
        by_svc = defaultdict(int)
        for s in error_spans:
            by_svc[s.resource.service_name] += 1
        for svc, count in by_svc.items():
            score = min(1.0, count / 5.0)
            findings.append(
                AnomalyFinding(
                    anomaly_type="error_span_burst",
                    service=svc,
                    description=f"{count} error span(s) on {svc}",
                    score=score,
                    evidence_count=count,
                )
            )

    # Metric spikes: high failure counters or elevated latency
    for m in metrics:
        if m.name in ("orders_total", "payments_total") and m.attributes.get("status") in (
            "failed",
            "payment_failed",
        ):
            score = min(1.0, m.value / 10.0)
            findings.append(
                AnomalyFinding(
                    anomaly_type="error_rate_spike",
                    service=m.resource.service_name,
                    description=f"{m.name}={{status={m.attributes.get('status')}}} value={m.value}",
                    score=score,
                    related_metric=m.name,
                    evidence_count=int(m.value),
                )
            )
        if m.name in ("payment_duration_seconds", "order_duration_seconds") and m.value >= 1.0:
            score = min(1.0, m.value / 5.0)
            findings.append(
                AnomalyFinding(
                    anomaly_type="latency_spike",
                    service=m.resource.service_name,
                    description=f"{m.name}={m.value:.2f}s (elevated)",
                    score=score,
                    related_metric=m.name,
                    evidence_count=1,
                )
            )

    # Service fanout: many services involved
    services = set()
    for l in logs:
        services.add(l.resource.service_name)
    for m in metrics:
        services.add(m.resource.service_name)
    for s in traces:
        services.add(s.resource.service_name)
    if len(services) >= 3:
        findings.append(
            AnomalyFinding(
                anomaly_type="service_fanout",
                service=None,
                description=f"{len(services)} services involved: {', '.join(sorted(services))}",
                score=min(1.0, len(services) / 5.0),
                evidence_count=len(services),
            )
        )

    return findings


def _score_severity(
    incident: IncidentContext,
    anomalies: List[AnomalyFinding],
) -> SeverityScore:
    """Produce a 0–100 severity score with factor breakdown."""
    factors: Dict[str, float] = {}

    # Base from declared severity
    base = {SeverityLevel.TRACE: 5, SeverityLevel.DEBUG: 10, SeverityLevel.INFO: 20,
            SeverityLevel.WARN: 40, SeverityLevel.ERROR: 65, SeverityLevel.FATAL: 90}
    factors["declared_severity"] = float(base.get(incident.severity, 50))

    # Anomaly contribution (avg of top scores * 30)
    if anomalies:
        top = sorted((a.score for a in anomalies), reverse=True)[:3]
        factors["anomaly_strength"] = round(sum(top) / len(top) * 30, 1)
    else:
        factors["anomaly_strength"] = 0.0

    # Error evidence volume
    error_logs = sum(
        1 for l in incident.logs if l.severity in (SeverityLevel.ERROR, SeverityLevel.FATAL)
    )
    error_spans = sum(1 for s in incident.traces if s.status == SpanStatus.ERROR)
    volume = error_logs + error_spans
    factors["error_volume"] = round(min(20.0, volume * 2.0), 1)

    # Blast radius (affected services)
    factors["blast_radius"] = round(min(15.0, len(incident.affected_services) * 5.0), 1)

    total = sum(factors.values())
    total = max(0.0, min(100.0, total))

    # Map numeric → level
    if total >= 85:
        level = SeverityLevel.FATAL
    elif total >= 60:
        level = SeverityLevel.ERROR
    elif total >= 40:
        level = SeverityLevel.WARN
    elif total >= 20:
        level = SeverityLevel.INFO
    else:
        level = SeverityLevel.DEBUG

    # Prefer higher of computed vs declared
    if _SEV_RANK[incident.severity] > _SEV_RANK[level]:
        level = incident.severity

    rationale_parts = [f"{k}={v}" for k, v in factors.items()]
    rationale = (
        f"Score {total:.0f}/100 → {level.value}. Factors: " + ", ".join(rationale_parts)
    )

    return SeverityScore(level=level, score=round(total, 1), factors=factors, rationale=rationale)


def correlate_incident(incident_id: str) -> CorrelationResult:
    """Run correlation + anomaly detection + severity scoring on a stored incident.

    Runs inside incident_store.update(): if another writer changes the
    incident meanwhile, the analysis is redone on the merged state.
    """

    def analyse(incident: IncidentContext) -> CorrelationResult:
        logs = incident.logs
        metrics = incident.metrics
        traces = incident.traces

        trace_links, trace_ids = _correlate_by_trace_id(logs, traces)
        service_links, service_graph = _correlate_by_service(logs, metrics, traces)
        links = trace_links + service_links

        anomalies = _detect_anomalies(logs, metrics, traces)
        severity = _score_severity(incident, anomalies)

        # Update incident severity if scoring is higher
        if _SEV_RANK[severity.level] > _SEV_RANK[incident.severity]:
            incident.severity = severity.level
        incident.metadata = {
            **incident.metadata,
            "severity_score": severity.score,
            "anomaly_count": len(anomalies),
            "correlation_link_count": len(links),
        }
        # Replace (not append) any previous correlation suffix so re-running
        # correlation (agent, RCA, API) keeps the summary stable.
        base_summary = (incident.evidence_summary or "").split(_CORRELATION_MARKER)[0]
        incident.evidence_summary = (
            f"{base_summary}{_CORRELATION_MARKER}"
            f"{len(links)} links, {len(anomalies)} anomalies, "
            f"severity_score={severity.score}"
        ).strip(" |")

        summary = (
            f"Incident {incident_id}: {len(links)} correlation link(s), "
            f"{len(anomalies)} anomal(ies), severity {severity.level.value} "
            f"({severity.score}/100). Trace IDs: {len(trace_ids)}."
        )
        return CorrelationResult(
            incident_id=incident_id,
            links=links,
            anomalies=anomalies,
            severity=severity,
            correlated_trace_ids=trace_ids,
            service_graph=service_graph,
            summary=summary,
            incident=incident,
        )

    incident, result = incident_store.update(incident_id, analyse)
    result.incident = incident  # the stored copy, with its new version
    logger.info(result.summary)
    return result
