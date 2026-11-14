"""Centralised application configuration."""
from functools import lru_cache
from typing import Dict, List, Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ProviderName = Literal["mock", "gemini", "anthropic", "openai", "ollama"]

# read: GET endpoints | write: other non-AI changes | ai: endpoints that call
# the AI model | alerts: Alertmanager webhook | *: everything
Scope = Literal["read", "write", "ai", "alerts", "*"]
NotifyEvent = Literal["incident_created", "rca_completed", "runbook_completed", "incident_resolved"]
SeverityName = Literal["TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"]


class APIKeySpec(BaseModel):
    key: str = Field(..., min_length=16)
    scopes: List[Scope] = Field(default_factory=lambda: ["read"])


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
    # text (local dev) | json (one JSON object per line, for log pipelines)
    LOG_FORMAT: Literal["text", "json"] = "text"

    AI_PROVIDER: ProviderName = "mock"
    # Real providers tried in order after AI_PROVIDER fails (mock fallback comes last).
    AI_FALLBACK_PROVIDERS: List[ProviderName] = Field(default_factory=list)
    AI_REQUEST_TIMEOUT_SECONDS: float = 120.0

    # Structured-response cache for real model answers (0 disables)
    AI_CACHE_TTL_SECONDS: float = 3600.0
    AI_CACHE_MAX_ENTRIES: int = 500
    # model -> [USD per 1M input tokens, USD per 1M output tokens], for the
    # ai_llm_cost_usd_total estimate. Add entries for other models you use.
    AI_MODEL_PRICES: Dict[str, List[float]] = Field(
        default_factory=lambda: {
            "claude-opus-5-5": [4.0, 20.0],
            "claude-sonnet-5-5": [2.0, 10.0],
            "claude-haiku-5-5": [0.10, 0.50],
            "claude-fable-5-1": [10.0, 50.0],
        }
    )

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

    # Log monitor: polls Loki (read-only) for error patterns and opens incidents
    LOG_MONITOR_ENABLED: bool = False
    LOG_MONITOR_INTERVAL_SECONDS: int = 30
    LOG_MONITOR_QUERY: str = '{source=~"docker|file"}'  # stream selector for watched logs
    LOG_MONITOR_SERVICE_LABEL: str = "service_name"
    LOG_MONITOR_LAG_SECONDS: int = 10  # don't read the newest seconds (ingest delay)
    LOG_MONITOR_START_LOOKBACK_SECONDS: int = 60
    LOG_MONITOR_NEW_PATTERN_MIN_COUNT: int = 2  # a never-seen error pattern this often per poll
    LOG_MONITOR_MIN_COUNT: int = 3  # a known pattern must reach this AND spike
    LOG_MONITOR_Z_THRESHOLD: float = 3.0
    LOG_MONITOR_RESOLVE_AFTER_SECONDS: int = 300  # quiet this long -> auto-resolve
    LOG_MONITOR_RECURRING_DAYS: int = 7
    LOG_MONITOR_EXCLUDE_SERVICES: List[str] = Field(default_factory=list)
    LOG_MONITOR_INVESTIGATE: bool = True  # run enrich/correlate/RCA/runbook on new incidents
    LOG_MONITOR_MAX_LINES_PER_RUN: int = 5000

    # Alertmanager webhook (POST /alerts/alertmanager): what to run for new alerts
    ALERT_AUTO_INVESTIGATE: bool = True
    ALERT_AUTO_ENRICH: bool = True
    ALERT_AUTO_RCA: bool = True
    ALERT_AUTO_RUNBOOK: bool = True

    # Notifications (see app/services/notifier.py)
    NOTIFY_SLACK_WEBHOOK_URL: str | None = None
    NOTIFY_WEBHOOK_URLS: List[str] = Field(default_factory=list)
    NOTIFY_WEBHOOK_SECRET: str | None = None  # HMAC-SHA256 signs generic webhook bodies
    NOTIFY_EVENTS: List[NotifyEvent] = Field(
        default_factory=lambda: ["incident_created", "rca_completed", "runbook_completed", "incident_resolved"]
    )
    NOTIFY_MIN_SEVERITY: SeverityName = "WARN"
    NOTIFY_TIMEOUT_SECONDS: float = 5.0
    # Email channel (SMTP). Locally: Mailpit (SMTP_HOST=mailpit, SMTP_PORT=1025).
    SMTP_HOST: str | None = None  # unset = email disabled
    SMTP_PORT: int = 1025
    SMTP_USERNAME: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_STARTTLS: bool = False
    SMTP_SSL: bool = False
    NOTIFY_EMAIL_FROM: str = "incident-bot@localhost"
    NOTIFY_EMAIL_TO: List[str] = Field(default_factory=list)  # production support team
    # The full incident report goes out with runbook_completed
    NOTIFY_EMAIL_EVENTS: List[NotifyEvent] = Field(
        default_factory=lambda: ["incident_created", "runbook_completed", "incident_resolved"]
    )
    # Web UI base URL for "open incident" links in emails, e.g. http://localhost:8080
    UI_BASE_URL: str | None = None
    # Base URL used for incident links in notifications, e.g. https://ai.example.com
    PUBLIC_BASE_URL: str | None = None

    # Sent as X-Fault-Token when scenarios reconfigure payment-service
    PAYMENT_FAULT_INJECTION_TOKEN: str | None = None

    # Live-telemetry enrichment (POST /incidents/{id}/enrich)
    ENRICH_LOOKBACK_SECONDS: int = 900
    ENRICH_MAX_LOGS_PER_SERVICE: int = 100
    ENRICH_MAX_METRICS_PER_SERVICE: int = 50
    ENRICH_MAX_TRACES_PER_SERVICE: int = 5
    # Baseline anomaly detection (POST /incidents/{id}/baseline)
    BASELINE_ENABLED: bool = True  # also run during Alertmanager auto-investigation
    BASELINE_LOOKBACK_SECONDS: int = 3600  # how much history defines "normal"
    BASELINE_GAP_SECONDS: int = 60  # excluded between baseline and incident
    BASELINE_MIN_WINDOW_SECONDS: int = 300  # incident window is at least this long
    BASELINE_STEP_SECONDS: int = 30
    BASELINE_MIN_POINTS: int = 10
    BASELINE_Z_THRESHOLD: float = 3.0

    # Similar-incident lookup (GET /incidents/{id}/similar); top N go into the RCA prompt
    SIMILAR_IN_RCA: int = 3  # 0 disables
    SIMILAR_MIN_SCORE: float = 0.35

    # Namespace the OTel collector's prometheus exporter adds to metric names
    ENRICH_METRIC_PREFIX: str = "otel_"

    # --- Week 15: resilience + security -----------------------------------
    AI_MAX_RETRIES: int = 2
    AI_RETRY_BACKOFF_SECONDS: float = 0.5
    AI_ENABLE_FALLBACK: bool = True  # fall back to mock if primary fails
    ENABLE_AUTH: bool = False  # master switch for API key auth
    API_KEY: str | None = None  # full-access key (all scopes)
    # Named, scoped keys, e.g. {"grafana": {"key": "...", "scopes": ["read"]},
    #                           "alertmanager": {"key": "...", "scopes": ["alerts"]}}
    API_KEYS: Dict[str, APIKeySpec] = Field(default_factory=dict)
    # Accept ?api_key= (leaks into logs/history; header or Bearer preferred)
    ALLOW_API_KEY_QUERY_PARAM: bool = False

    # Per-client rate limits (client = API key name, or IP when auth is off).
    # In-process: with several replicas each enforces its own limit.
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_PER_MINUTE: int = 300
    # Stricter limit for endpoints that call the AI model (cost control)
    AI_RATE_LIMIT_PER_MINUTE: int = 30

    # --- Persistence --------------------------------------------------------
    # If set (e.g. postgresql+psycopg2://user:pass@postgres:5432/ai_incidents),
    # incidents are stored in Postgres. If empty, in-memory store is used.
    DATABASE_URL: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
