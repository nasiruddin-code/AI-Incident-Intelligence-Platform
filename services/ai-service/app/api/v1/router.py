"""
Aggregates all v1 endpoint routers into a single `api_router`.
"""
from fastapi import APIRouter

from app.api.v1.endpoints import agent, e2e, health, incidents, log_analysis, observability, telemetry

api_router = APIRouter()

api_router.include_router(health.router, prefix="/health", tags=["Health"])
api_router.include_router(log_analysis.router, prefix="/logs", tags=["Log Analysis"])
api_router.include_router(telemetry.router, prefix="/telemetry", tags=["Telemetry"])
api_router.include_router(incidents.router, prefix="/incidents", tags=["Incidents"])
api_router.include_router(agent.router, prefix="/agent", tags=["AI Agent"])
api_router.include_router(observability.router, prefix="/observability", tags=["Observability"])
api_router.include_router(e2e.router, prefix="/e2e", tags=["E2E"])
