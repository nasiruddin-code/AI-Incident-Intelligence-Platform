"""Prometheus metrics exposition for AI incident intelligence (Week 14).

Values come from incident_store.stats(), which aggregates in SQL when
Postgres is enabled, so a scrape never loads incident evidence.
"""
from fastapi import APIRouter, Response
from starlette.concurrency import run_in_threadpool

from app.core.ai_metrics import ai_metrics
from app.services import notifier
from app.services import incident_store

router = APIRouter()


@router.get("", include_in_schema=True, summary="Prometheus metrics")
async def prometheus_metrics() -> Response:
    stats = await run_in_threadpool(incident_store.stats)

    lines = [
        "# HELP ai_incidents_total Total incidents in store",
        "# TYPE ai_incidents_total gauge",
        f"ai_incidents_total {stats.total}",
        "# HELP ai_incidents_open Open or investigating incidents",
        "# TYPE ai_incidents_open gauge",
        f"ai_incidents_open {stats.open}",
        "# HELP ai_incidents_by_severity Incidents grouped by severity",
        "# TYPE ai_incidents_by_severity gauge",
    ]
    for sev, count in sorted(stats.by_severity.items()):
        lines.append(f'ai_incidents_by_severity{{severity="{sev}"}} {count}')

    lines += [
        "# HELP ai_rca_confidence_avg Average RCA confidence across incidents",
        "# TYPE ai_rca_confidence_avg gauge",
        f"ai_rca_confidence_avg {stats.rca_confidence_avg}",
        "# HELP ai_runbook_confidence_avg Average runbook confidence",
        "# TYPE ai_runbook_confidence_avg gauge",
        f"ai_runbook_confidence_avg {stats.runbook_confidence_avg}",
        "# HELP ai_severity_score_avg Average correlation severity score",
        "# TYPE ai_severity_score_avg gauge",
        f"ai_severity_score_avg {stats.severity_score_avg}",
        "# HELP ai_rca_runs_total Incidents that have RCA metadata",
        "# TYPE ai_rca_runs_total gauge",
        f"ai_rca_runs_total {stats.rca_runs}",
        "# HELP ai_runbook_runs_total Incidents that have runbook metadata",
        "# TYPE ai_runbook_runs_total gauge",
        f"ai_runbook_runs_total {stats.runbook_runs}",
    ]
    lines += ai_metrics.render()
    lines += notifier.render_metrics()

    body = "\n".join(lines) + "\n"
    return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")
