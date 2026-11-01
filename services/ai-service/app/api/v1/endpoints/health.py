from fastapi import APIRouter

from app.core.dependencies import AIProviderDep, SettingsDep
from app.schemas.common import HealthResponse, ReadinessResponse

router = APIRouter()


@router.get("", response_model=HealthResponse, summary="Liveness check")
async def health(settings: SettingsDep) -> HealthResponse:
    """Is the process up? No dependencies checked — used for k8s liveness."""
    return HealthResponse(
        status="ok",
        app_name=settings.APP_NAME,
        version=settings.APP_VERSION,
        environment=settings.ENVIRONMENT,
    )


@router.get("/ready", response_model=ReadinessResponse, summary="Readiness check")
async def readiness(provider: AIProviderDep) -> ReadinessResponse:
    """AI provider + optional database readiness."""
    db_status = "memory"
    try:
        from app.db.session import is_db_enabled, get_session
        if is_db_enabled():
            session = get_session()
            try:
                session.execute(__import__("sqlalchemy").text("SELECT 1"))
                db_status = "postgres"
            finally:
                session.close()
    except Exception:
        db_status = "error"
    status = "ready" if db_status != "error" else "degraded"
    return ReadinessResponse(status=status, ai_provider=provider.name, database=db_status)
