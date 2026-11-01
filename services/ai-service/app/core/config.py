"""Centralised application configuration."""
from functools import lru_cache
from typing import List, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ProviderName = Literal["mock", "gemini", "anthropic", "openai", "ollama"]


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

    AI_PROVIDER: ProviderName = "mock"
    # Real providers tried in order after AI_PROVIDER fails (mock fallback comes last).
    AI_FALLBACK_PROVIDERS: List[ProviderName] = Field(default_factory=list)
    AI_REQUEST_TIMEOUT_SECONDS: float = 120.0

    GEMINI_API_KEY: str | None = None
    GEMINI_MODEL: str = "gemini-2.5-flash"

    # Claude (Anthropic). Key may also come from the SDK's own credential chain.
    ANTHROPIC_API_KEY: str | None = None
    ANTHROPIC_MODEL: str = "claude-opus-5-5"
    ANTHROPIC_EFFORT: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    ANTHROPIC_MAX_TOKENS: int = 16000
    # Server-side refusal fallback (`fallbacks: "default"`)
    ANTHROPIC_SERVER_FALLBACKS: bool = True

    OPENAI_API_KEY: str | None = None
    OPENAI_MODEL: str = "gpt-4o-mini"
    OPENAI_BASE_URL: str | None = None

    # Ollama via its OpenAI-compatible endpoint (no API key)
    OLLAMA_BASE_URL: str = "http://ollama:11434/v1"
    OLLAMA_MODEL: str = "llama3.1"

    # Investigation agent: "auto" = LLM planner when a real provider is
    # configured, fixed pipeline with the mock provider.
    AGENT_MODE: Literal["auto", "planner", "fixed"] = "auto"

    OTEL_EXPORTER_OTLP_ENDPOINT: str | None = None
    SERVICE_NAME: str = "ai-service"

    PROMETHEUS_URL: str = "http://prometheus:9090"
    LOKI_URL: str = "http://loki:3100"
    TEMPO_URL: str = "http://tempo:3200"
    OBSERVABILITY_TIMEOUT_SECONDS: float = 10.0

    # Live-telemetry enrichment (POST /incidents/{id}/enrich)
    ENRICH_LOOKBACK_SECONDS: int = 900
    ENRICH_MAX_LOGS_PER_SERVICE: int = 100
    ENRICH_MAX_METRICS_PER_SERVICE: int = 50
    ENRICH_MAX_TRACES_PER_SERVICE: int = 5
    # Namespace the OTel collector's prometheus exporter adds to metric names
    ENRICH_METRIC_PREFIX: str = "otel_"

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
