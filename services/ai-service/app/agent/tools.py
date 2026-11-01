"""Tools the investigation agent can call (Week 12).

These wrap existing incident intelligence services so the agent can
investigate without re-implementing logic.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.core.logging import get_logger
from app.providers.base import AIProvider
from app.services import incident_store
from app.services.correlation import correlate_incident
from app.services.rca_engine import RCAEngine
from app.services.runbook_engine import RunbookEngine

logger = get_logger(__name__)


def tool_list_incidents(limit: int = 20) -> Dict[str, Any]:
    """Newest-updated incidents first (summaries only, no evidence)."""
    items, total = incident_store.list_summaries(limit=limit)
    return {
        "count": total,
        "incidents": [
            {
                "incident_id": i.incident_id,
                "title": i.title,
                "status": i.status.value,
                "severity": i.severity.value,
                "affected_services": i.affected_services,
            }
            for i in items
        ],
    }


def tool_get_incident(incident_id: str) -> Dict[str, Any]:
    incident = incident_store.get(incident_id)
    if not incident:
        return {"error": f"Incident {incident_id} not found"}
    return {
        "incident_id": incident.incident_id,
        "title": incident.title,
        "status": incident.status.value,
        "severity": incident.severity.value,
        "affected_services": incident.affected_services,
        "evidence_summary": incident.evidence_summary,
        "log_count": len(incident.logs),
        "metric_count": len(incident.metrics),
        "span_count": len(incident.traces),
        "scenario": incident.scenario.value if incident.scenario else None,
        "metadata": incident.metadata,
        "notes": incident.notes,
    }


def tool_search_evidence(incident_id: str, query: str = "") -> Dict[str, Any]:
    incident = incident_store.get(incident_id)
    if not incident:
        return {"error": f"Incident {incident_id} not found"}
    q = query.lower().strip()
    logs = []
    for log in incident.logs:
        if not q or q in log.body.lower() or q in log.severity.value.lower():
            logs.append(
                {
                    "severity": log.severity.value,
                    "body": log.body[:200],
                    "service": log.resource.service_name,
                    "trace_id": log.trace_id,
                }
            )
    spans = []
    for s in incident.traces:
        if not q or q in s.name.lower() or q in s.status.value.lower():
            spans.append(
                {
                    "name": s.name,
                    "status": s.status.value,
                    "service": s.resource.service_name,
                    "trace_id": s.trace_id,
                }
            )
    metrics = []
    for m in incident.metrics:
        if not q or q in m.name.lower():
            metrics.append(
                {
                    "name": m.name,
                    "value": m.value,
                    "service": m.resource.service_name,
                    "attributes": m.attributes,
                }
            )
    return {
        "query": query,
        "logs": logs[:20],
        "spans": spans[:20],
        "metrics": metrics[:20],
    }


def tool_correlate(incident_id: str) -> Dict[str, Any]:
    try:
        result = correlate_incident(incident_id)
    except KeyError:
        return {"error": f"Incident {incident_id} not found"}
    return {
        "incident_id": result.incident_id,
        "summary": result.summary,
        "severity_score": result.severity.score,
        "severity_level": result.severity.level.value,
        "anomaly_count": len(result.anomalies),
        "anomalies": [
            {"type": a.anomaly_type, "description": a.description, "score": a.score}
            for a in result.anomalies
        ],
        "link_count": len(result.links),
        "trace_ids": result.correlated_trace_ids[:10],
        "service_graph": result.service_graph,
    }


async def tool_run_rca(incident_id: str, provider: AIProvider) -> Dict[str, Any]:
    engine = RCAEngine(provider)
    try:
        resp = await engine.analyze(incident_id, run_correlation=False)
    except KeyError:
        return {"error": f"Incident {incident_id} not found"}
    return {
        "incident_id": resp.incident_id,
        "provider": resp.provider,
        "degraded": resp.degraded,
        "primary_cause": resp.rca.primary_cause,
        "summary": resp.rca.summary,
        "confidence": resp.rca.confidence,
        "hypotheses": [
            {"rank": h.rank, "cause": h.cause, "confidence": h.confidence}
            for h in resp.rca.hypotheses
        ],
        "recommended_next_steps": resp.rca.recommended_next_steps,
    }


async def tool_generate_runbook(incident_id: str, provider: AIProvider) -> Dict[str, Any]:
    engine = RunbookEngine(provider)
    try:
        resp = await engine.generate(incident_id)
    except KeyError:
        return {"error": f"Incident {incident_id} not found"}
    return {
        "incident_id": resp.incident_id,
        "provider": resp.provider,
        "degraded": resp.degraded,
        "title": resp.runbook.title,
        "summary": resp.runbook.summary,
        "confidence": resp.runbook.confidence,
        "steps": [
            {"order": s.order, "action": s.action}
            for s in resp.runbook.steps
        ],
        "remediation_actions": [
            {
                "priority": a.priority,
                "action": a.action,
                "risk_level": a.risk_level,
                "confidence": a.confidence,
            }
            for a in resp.runbook.remediation_actions
        ],
        "verification_checks": resp.runbook.verification_checks,
    }


# Registry for agent planner (args named after AgentAction fields)
TOOL_DESCRIPTIONS = {
    "list_incidents": "List recent incidents (id, title, severity, status), newest first. Args: none.",
    "get_incident": "Get summary of an incident (counts of logs/metrics/spans, notes, metadata). Args: incident_id.",
    "search_evidence": "Search the incident's stored logs/spans/metrics by keyword. Args: incident_id, query (keyword; empty = all).",
    "collect_live_telemetry": "Pull live logs, metrics and traces for the incident's services and time window from Loki/Prometheus/Tempo into the incident. Args: incident_id.",
    "analyze_baseline": "Compare the incident's services with their own recent history in Prometheus (request rate, 5xx ratio, latency, business failure ratio); run before correlate. Args: incident_id.",
    "correlate": "Run correlation, anomaly detection, and severity scoring on an incident. Args: incident_id.",
    "run_rca": "Run AI-assisted root cause analysis on an incident. Args: incident_id.",
    "generate_runbook": "Generate an AI runbook and remediation plan for an incident. Args: incident_id.",
    "query_prometheus": "Run a PromQL instant query (metric names carry the otel_ prefix). Args: query.",
    "query_loki": "Run a LogQL query over the last hour (label job=<service>). Args: query.",
    "search_tempo": "Search recent distributed traces, optionally for one service. Args: service.",
    "get_trace": "Get the spans of one trace from Tempo. Args: trace_id.",
    "observability_health": "Check connectivity to Prometheus, Loki, and Tempo.",
}


async def tool_collect_live_telemetry(incident_id: str) -> Dict[str, Any]:
    from app.core.config import get_settings
    from app.services.telemetry_enrichment import TelemetryEnrichmentService
    try:
        result = await TelemetryEnrichmentService(get_settings()).enrich(incident_id)
    except KeyError:
        return {"error": f"Incident {incident_id} not found"}
    return result.model_dump(mode="json")


async def tool_analyze_baseline(incident_id: str) -> Dict[str, Any]:
    from app.core.config import get_settings
    from app.services.baseline import BaselineAnalyzer
    try:
        result = await BaselineAnalyzer(get_settings()).analyze(incident_id)
    except KeyError:
        return {"error": f"Incident {incident_id} not found"}
    return {
        "anomalies": result.anomalies,
        "signals": [
            {"service": s.service, "signal": s.signal, "anomalous": s.anomalous, "detail": s.reason}
            for s in result.signals
        ],
    }


async def tool_get_trace(trace_id: str) -> Dict[str, Any]:
    from app.core.config import get_settings
    from app.services.telemetry_query import TelemetryQueryService
    return await TelemetryQueryService(get_settings()).get_trace(trace_id)


async def tool_query_prometheus(promql: str = "up") -> Dict[str, Any]:
    from app.core.config import get_settings
    from app.services.telemetry_query import TelemetryQueryService
    svc = TelemetryQueryService(get_settings())
    return await svc.query_metrics(promql=promql)


async def tool_query_loki(logql: str = '{job=~".+"}', lookback_seconds: int = 3600) -> Dict[str, Any]:
    from app.core.config import get_settings
    from app.services.telemetry_query import TelemetryQueryService
    svc = TelemetryQueryService(get_settings())
    return await svc.query_logs(logql=logql, lookback_seconds=lookback_seconds)


async def tool_search_tempo(service: Optional[str] = None) -> Dict[str, Any]:
    from app.core.config import get_settings
    from app.services.telemetry_query import TelemetryQueryService
    svc = TelemetryQueryService(get_settings())
    return await svc.search_traces(service=service)


async def tool_observability_health() -> Dict[str, Any]:
    from app.core.config import get_settings
    from app.services.telemetry_query import TelemetryQueryService
    svc = TelemetryQueryService(get_settings())
    return await svc.health()

