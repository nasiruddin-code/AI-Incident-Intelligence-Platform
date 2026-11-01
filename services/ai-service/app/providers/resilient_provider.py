"""Resilient AI provider: retries + fallback to mock on outage (Week 15)."""
from __future__ import annotations

import asyncio
from typing import Optional, Type

from app.core.logging import get_logger
from app.providers.base import AIProvider, AIProviderError, SchemaT
from app.providers.mock_provider import MockAIProvider

logger = get_logger(__name__)


class ResilientAIProvider(AIProvider):
    """Wraps a primary provider with retries and optional mock fallback."""

    name = "resilient"

    def __init__(
        self,
        primary: AIProvider,
        *,
        fallback: AIProvider | None = None,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.5,
        enable_fallback: bool = True,
    ):
        self._primary = primary
        self._fallback = fallback or MockAIProvider()
        self._max_retries = max_retries
        self._backoff = retry_backoff_seconds
        self._enable_fallback = enable_fallback
        self.name = f"resilient[{primary.name}]"

    async def _call_with_resilience(self, method_name: str, *args, **kwargs):
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                method = getattr(self._primary, method_name)
                result = await method(*args, **kwargs)
                if attempt > 0:
                    logger.info("Primary AI provider recovered on attempt %d", attempt + 1)
                return result
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "AI provider %s failed (attempt %d/%d): %s",
                    self._primary.name,
                    attempt + 1,
                    self._max_retries + 1,
                    exc,
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(self._backoff * (2**attempt))

        if self._enable_fallback:
            logger.error(
                "Primary AI provider %s exhausted retries; falling back to %s",
                self._primary.name,
                self._fallback.name,
            )
            method = getattr(self._fallback, method_name)
            result = await method(*args, **kwargs)
            # Tag fallback usage for observability
            if hasattr(result, "model_dump"):
                # Can't mutate pydantic easily; callers see provider name via outer response
                pass
            self.name = f"resilient[{self._primary.name}->fallback:{self._fallback.name}]"
            return result

        raise AIProviderError(
            f"AI provider {self._primary.name} failed after retries: {last_exc}"
        ) from last_exc

    async def generate_text(self, prompt: str, system_instruction: Optional[str] = None) -> str:
        return await self._call_with_resilience(
            "generate_text", prompt, system_instruction=system_instruction
        )

    async def generate_structured(
        self,
        prompt: str,
        response_model: Type[SchemaT],
        system_instruction: Optional[str] = None,
    ) -> SchemaT:
        return await self._call_with_resilience(
            "generate_structured",
            prompt,
            response_model,
            system_instruction=system_instruction,
        )
