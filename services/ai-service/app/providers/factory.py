"""Provider factory with resilient wrapper (Week 15)."""
from functools import lru_cache

from app.core.config import get_settings
from app.core.logging import get_logger
from app.providers.base import AIProvider
from app.providers.mock_provider import MockAIProvider
from app.providers.resilient_provider import ResilientAIProvider

logger = get_logger(__name__)


@lru_cache
def get_ai_provider() -> AIProvider:
    settings = get_settings()

    if settings.AI_PROVIDER == "gemini":
        if not settings.GEMINI_API_KEY:
            logger.warning(
                "AI_PROVIDER=gemini but GEMINI_API_KEY missing; using mock with resilience wrapper"
            )
            primary: AIProvider = MockAIProvider()
        else:
            from app.providers.gemini_provider import GeminiProvider

            primary = GeminiProvider(
                api_key=settings.GEMINI_API_KEY, model=settings.GEMINI_MODEL
            )
    else:
        primary = MockAIProvider()

    return ResilientAIProvider(
        primary,
        fallback=MockAIProvider(),
        max_retries=settings.AI_MAX_RETRIES,
        retry_backoff_seconds=settings.AI_RETRY_BACKOFF_SECONDS,
        enable_fallback=settings.AI_ENABLE_FALLBACK,
    )
