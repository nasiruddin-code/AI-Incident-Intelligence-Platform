from fastapi import APIRouter
import httpx

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
    payment_status = "unknown"
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{settings.PAYMENT_SERVICE_URL.rstrip('/')}/api/v1/health")
            payment_status = "up" if r.status_code == 200 else "down"
    except Exception:
        payment_status = "down"

    overall = "ready" if payment_status == "up" else "degraded"
    return ReadinessResponse(
        status=overall,
        service=settings.SERVICE_NAME,
        payment_service=payment_status,
    )
