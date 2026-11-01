"""Centralised application configuration."""
from functools import lru_cache
from typing import List, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    APP_NAME: str = "ai-service"
    APP_VERSION: str = "0.1.0"
    ENVIRONMENT: Literal["development", "staging", "production", "test"] = "development"
    DEBUG: bool = True

    API_V1_PREFIX: str = "/api/v1"
    CORS_ORIGINS: List[str] = Field(default_factory=lambda: ["*"])

    LOG_LEVEL: str = "INFO"

    AI_PROVIDER: Literal["mock", "gemini"] = "mock"
    GEMINI_API_KEY: str | None = None
    GEMINI_MODEL: str = "gemini-2.5-flash"

    OTEL_EXPORTER_OTLP_ENDPOINT: str | None = None
    SERVICE_NAME: str = "ai-service"

    PROMETHEUS_URL: str = "http://prometheus:9090"
    LOKI_URL: str = "http://loki:3100"
    TEMPO_URL: str = "http://tempo:3200"
    OBSERVABILITY_TIMEOUT_SECONDS: float = 10.0

    # --- Week 15: resilience + security -----------------------------------
    AI_MAX_RETRIES: int = 2
    AI_RETRY_BACKOFF_SECONDS: float = 0.5
    AI_ENABLE_FALLBACK: bool = True  # fall back to mock if primary fails
    API_KEY: str | None = None  # if set, require X-API-Key header
    ENABLE_AUTH: bool = False  # master switch for API key auth

    # --- Persistence --------------------------------------------------------
    # If set (e.g. postgresql+psycopg2://user:pass@postgres:5432/ai_incidents),
    # incidents are stored in Postgres. If empty, in-memory store is used.
    DATABASE_URL: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
