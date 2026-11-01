"""Real observability query endpoints (Week 13)."""
from typing import Optional

from fastapi import APIRouter, Query

from app.core.dependencies import SettingsDep
from app.services.telemetry_query import TelemetryQueryService

router = APIRouter()


def _svc(settings: SettingsDep) -> TelemetryQueryService:
    return TelemetryQueryService(settings)


@router.get("/health", summary="Check Prometheus / Loki / Tempo connectivity")
async def observability_health(settings: SettingsDep):
    return await _svc(settings).health()


@router.get("/metrics", summary="Query Prometheus (PromQL)")
async def query_metrics(
    settings: SettingsDep,
    query: str = Query(default="up", description="PromQL expression"),
):
    return await _svc(settings).query_metrics(promql=query)


@router.get("/logs", summary="Query Loki (LogQL)")
async def query_logs(
    settings: SettingsDep,
    query: str = Query(default='{job=~".+"}', description="LogQL expression"),
    lookback_seconds: int = Query(default=3600, ge=60, le=86400),
    limit: int = Query(default=50, ge=1, le=500),
):
    return await _svc(settings).query_logs(logql=query, lookback_seconds=lookback_seconds, limit=limit)


@router.get("/traces", summary="Search Tempo traces")
async def search_traces(
    settings: SettingsDep,
    service: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    lookback_seconds: int = Query(default=3600, ge=60, le=86400),
):
    return await _svc(settings).search_traces(
        service=service, limit=limit, lookback_seconds=lookback_seconds
    )


@router.get("/traces/{trace_id}", summary="Get a Tempo trace by ID")
async def get_trace(trace_id: str, settings: SettingsDep):
    return await _svc(settings).get_trace(trace_id)
