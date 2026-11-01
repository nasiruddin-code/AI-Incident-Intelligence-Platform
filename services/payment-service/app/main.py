from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
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
            "Starting %s v%s in %s mode",
            settings.APP_NAME,
            settings.APP_VERSION,
            settings.ENVIRONMENT,
        )
        yield
        shutdown_telemetry()
        logger.info("Shutting down %s", settings.APP_NAME)

    return lifespan


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)

    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        description="Sample Payment microservice (Week 6–7) — processes payments with OTel instrumentation.",
        lifespan=_make_lifespan(settings),
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router, prefix=settings.API_V1_PREFIX)
    return app


app = create_app()
