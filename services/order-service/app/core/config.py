"""Centralised configuration for order-service."""
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

    APP_NAME: str = "order-service"
    APP_VERSION: str = "0.1.0"
    ENVIRONMENT: Literal["development", "staging", "production", "test"] = "development"
    DEBUG: bool = True

    API_V1_PREFIX: str = "/api/v1"
    CORS_ORIGINS: List[str] = Field(default_factory=lambda: ["*"])

    LOG_LEVEL: str = "INFO"
    SERVICE_NAME: str = "order-service"

    # Downstream dependency
    PAYMENT_SERVICE_URL: str = "http://localhost:8002"

    OTEL_EXPORTER_OTLP_ENDPOINT: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
