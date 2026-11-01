"""Prometheus metrics exposition for AI incident intelligence (Week 14)."""
from collections import Counter

from fastapi import APIRouter, Response

from app.schemas.incident import IncidentStatus
from app.services import incident_store

router = APIRouter()


@router.get("", include_in_schema=True, summary="Prometheus metrics")
async def prometheus_metrics() -> Response:
    incidents = incident_store.list_all()
    open_count = sum(
        1
        for i in incidents
        if i.status in (IncidentStatus.OPEN, IncidentStatus.INVESTIGATING)
    )
    by_sev = Counter(i.severity.value for i in incidents)

    rca_scores = [
        float(i.metadata["rca_confidence"])
        for i in incidents
        if i.metadata and i.metadata.get("rca_confidence") is not None
    ]
    rb_scores = [
        float(i.metadata["runbook_confidence"])
        for i in incidents
        if i.metadata and i.metadata.get("runbook_confidence") is not None
    ]
    sev_scores = [
        float(i.metadata["severity_score"])
        for i in incidents
        if i.metadata and i.metadata.get("severity_score") is not None
    ]

    lines = [
        "# HELP ai_incidents_total Total incidents in store",
        "# TYPE ai_incidents_total gauge",
        f"ai_incidents_total {len(incidents)}",
        "# HELP ai_incidents_open Open or investigating incidents",
        "# TYPE ai_incidents_open gauge",
        f"ai_incidents_open {open_count}",
        "# HELP ai_incidents_by_severity Incidents grouped by severity",
        "# TYPE ai_incidents_by_severity gauge",
    ]
    for sev, count in sorted(by_sev.items()):
        lines.append(f'ai_incidents_by_severity{{severity="{sev}"}} {count}')

    lines += [
        "# HELP ai_rca_confidence_avg Average RCA confidence across incidents",
        "# TYPE ai_rca_confidence_avg gauge",
        f"ai_rca_confidence_avg {(sum(rca_scores) / len(rca_scores)) if rca_scores else 0}",
        "# HELP ai_runbook_confidence_avg Average runbook confidence",
        "# TYPE ai_runbook_confidence_avg gauge",
        f"ai_runbook_confidence_avg {(sum(rb_scores) / len(rb_scores)) if rb_scores else 0}",
        "# HELP ai_severity_score_avg Average correlation severity score",
        "# TYPE ai_severity_score_avg gauge",
        f"ai_severity_score_avg {(sum(sev_scores) / len(sev_scores)) if sev_scores else 0}",
        "# HELP ai_rca_runs_total Incidents that have RCA metadata",
        "# TYPE ai_rca_runs_total gauge",
        f"ai_rca_runs_total {len(rca_scores)}",
        "# HELP ai_runbook_runs_total Incidents that have runbook metadata",
        "# TYPE ai_runbook_runs_total gauge",
        f"ai_runbook_runs_total {len(rb_scores)}",
    ]

    body = "\n".join(lines) + "\n"
    return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")
