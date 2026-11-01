from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.logging import RequestIDMiddleware, configure_logging, get_logger
from app.core.telemetry import instrument_app, setup_telemetry, shutdown_telemetry

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
        logger.info(
            "Starting %s v%s in %s mode (payment_service=%s)",
            settings.APP_NAME,
            settings.APP_VERSION,
            settings.ENVIRONMENT,
            settings.PAYMENT_SERVICE_URL,
        )
        yield
        shutdown_telemetry()
        logger.info("Shutting down %s", settings.APP_NAME)

    return lifespan


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL, settings.LOG_FORMAT, settings.SERVICE_NAME)

    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        description="Sample Order microservice (Week 6–7) — creates orders, calls payment-service, with OTel instrumentation.",
        lifespan=_make_lifespan(settings),
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Outermost: every response (including 401/429) carries X-Request-ID
    app.add_middleware(RequestIDMiddleware)

    app.include_router(api_router, prefix=settings.API_V1_PREFIX)
    return app


app = create_app()
