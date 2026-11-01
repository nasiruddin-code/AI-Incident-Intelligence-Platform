from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.auth import APIKeyMiddleware
from app.core.rate_limit import RateLimitMiddleware

from app.api.v1.router import api_router
from app.api.v1.endpoints import metrics as metrics_endpoint
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.core.telemetry import instrument_app, setup_telemetry, shutdown_telemetry
from app.db.session import init_db
from app.integrations.http import close_http_client

logger = get_logger(__name__)


def _make_lifespan(settings: Settings):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_telemetry(
            service_name=settings.SERVICE_NAME,
            otlp_endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT,
            app_version=settings.APP_VERSION,
            environment=settings.ENVIRONMENT,
        )
        instrument_app(app)
        try:
            init_db()
        except Exception as exc:
            logger.error("Database init failed: %s", exc)
            raise
        logger.info(
            "Starting %s v%s in %s mode (AI provider: %s)",
            settings.APP_NAME,
            settings.APP_VERSION,
            settings.ENVIRONMENT,
            settings.AI_PROVIDER,
        )
        if settings.ENVIRONMENT == "production" and not settings.ENABLE_AUTH:
            logger.warning("ENVIRONMENT=production with ENABLE_AUTH=false: the API is unauthenticated")
        yield
        await close_http_client()
        shutdown_telemetry()
        logger.info("Shutting down %s", settings.APP_NAME)

    return lifespan


def create_app() -> FastAPI:
    """Application factory. Using a factory (instead of a bare module-level
    `app = FastAPI()`) makes it straightforward to spin up separately
    configured app instances in tests, and mirrors the pattern every other
    microservice in this repo will follow."""
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)

    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        description="AI Observability Platform - AI Service (log analysis, "
        "AI provider abstraction, and future incident-intelligence features).",
        lifespan=_make_lifespan(settings),
    )

    # Middleware added last runs first: CORS -> auth -> rate limit -> routes.
    # Auth must precede rate limiting so limits are keyed by API key name.
    app.add_middleware(RateLimitMiddleware)
    # Week 15: optional API-key auth with scopes (ENABLE_AUTH=true + API_KEY / API_KEYS)
    app.add_middleware(APIKeyMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        # Credentials with a wildcard origin are rejected by browsers.
        allow_credentials="*" not in settings.CORS_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router, prefix=settings.API_V1_PREFIX)
    # Week 14: Prometheus scrape path (unversioned)
    app.include_router(metrics_endpoint.router, prefix="/metrics", tags=["Metrics"])

    return app


app = create_app()
