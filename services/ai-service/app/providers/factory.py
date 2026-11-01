"""Provider factory with resilient wrapper (Week 15).

AI_PROVIDER picks the primary model; AI_FALLBACK_PROVIDERS lists further real
providers tried in order; the mock provider is the last resort (if enabled).
Provider SDKs are imported lazily so only the configured ones must be installed.
"""
from functools import lru_cache
from typing import List, Optional

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.providers.base import AIProvider
from app.providers.mock_provider import MockAIProvider
from app.providers.resilient_provider import ResilientAIProvider
from app.providers.response_cache import ResponseCache

logger = get_logger(__name__)


def build_provider(name: str, settings: Settings) -> Optional[AIProvider]:
    """Construct one provider, or None if it is not configured."""
    if name == "mock":
        return MockAIProvider()

    if name == "gemini":
        if not settings.GEMINI_API_KEY:
            logger.warning("Provider gemini skipped: GEMINI_API_KEY missing")
            return None
        from app.providers.gemini_provider import GeminiProvider

        return GeminiProvider(api_key=settings.GEMINI_API_KEY, model=settings.GEMINI_MODEL)

    if name == "anthropic":
        from app.providers.anthropic_provider import AnthropicProvider

        # No explicit key is fine: the SDK falls back to its own credential chain.
        return AnthropicProvider(
            api_key=settings.ANTHROPIC_API_KEY,
            model=settings.ANTHROPIC_MODEL,
            effort=settings.ANTHROPIC_EFFORT,
            max_tokens=settings.ANTHROPIC_MAX_TOKENS,
            server_fallbacks=settings.ANTHROPIC_SERVER_FALLBACKS,
            timeout=settings.AI_REQUEST_TIMEOUT_SECONDS,
        )

    if name == "openai":
        if not settings.OPENAI_API_KEY:
            logger.warning("Provider openai skipped: OPENAI_API_KEY missing")
            return None
        from app.providers.openai_provider import OpenAICompatibleProvider

        return OpenAICompatibleProvider(
            name="openai",
            model=settings.OPENAI_MODEL,
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL,
            timeout=settings.AI_REQUEST_TIMEOUT_SECONDS,
        )

    if name == "ollama":
        from app.providers.openai_provider import OpenAICompatibleProvider

        return OpenAICompatibleProvider(
            name="ollama",
            model=settings.OLLAMA_MODEL,
            base_url=settings.OLLAMA_BASE_URL,
            timeout=settings.AI_REQUEST_TIMEOUT_SECONDS,
        )

    raise ValueError(f"Unknown AI provider: {name}")


def build_chain(settings: Settings) -> List[AIProvider]:
    names: List[str] = []
    for name in [settings.AI_PROVIDER, *settings.AI_FALLBACK_PROVIDERS]:
        if name not in names and name != "mock":
            names.append(name)
    chain = [p for p in (build_provider(n, settings) for n in names) if p is not None]
    if not chain:
        if settings.AI_PROVIDER != "mock":
            logger.warning("No configured AI provider available; using mock")
        chain = [MockAIProvider()]
    return chain


@lru_cache
def get_ai_provider() -> AIProvider:
    settings = get_settings()
    cache = (
        ResponseCache(settings.AI_CACHE_TTL_SECONDS, settings.AI_CACHE_MAX_ENTRIES)
        if settings.AI_CACHE_TTL_SECONDS > 0
        else None
    )
    return ResilientAIProvider(
        build_chain(settings),
        fallback=MockAIProvider(),
        max_retries=settings.AI_MAX_RETRIES,
        retry_backoff_seconds=settings.AI_RETRY_BACKOFF_SECONDS,
        enable_fallback=settings.AI_ENABLE_FALLBACK,
        cache=cache,
        prices=settings.AI_MODEL_PRICES,
    )
