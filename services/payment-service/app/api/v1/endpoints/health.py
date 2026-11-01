from fastapi import APIRouter

from app.core.dependencies import SettingsDep
from app.schemas.common import HealthResponse, ReadinessResponse

router = APIRouter()


@router.get("", response_model=HealthResponse, summary="Liveness check")
async def health(settings: SettingsDep) -> HealthResponse:
    return HealthResponse(
        status="ok",
        app_name=settings.APP_NAME,
        version=settings.APP_VERSION,
        environment=settings.ENVIRONMENT,
    )


@router.get("/ready", response_model=ReadinessResponse, summary="Readiness check")
async def readiness(settings: SettingsDep) -> ReadinessResponse:
    return ReadinessResponse(status="ready", service=settings.SERVICE_NAME)
