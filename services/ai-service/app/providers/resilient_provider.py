"""Resilient AI provider: retries, provider chain, and mock fallback (Week 15)."""
from __future__ import annotations

import asyncio
from typing import Any, List, Optional, Sequence, Tuple, Type, Union

from app.core.logging import get_logger
from app.providers.base import AIProvider, AIProviderError, GenerationInfo, SchemaT
from app.providers.mock_provider import MockAIProvider

logger = get_logger(__name__)


class ResilientAIProvider(AIProvider):
    """Tries each provider in the chain (with retries), then the mock fallback.

    The instance is shared (cached by the factory), so it holds no per-call
    state: `name` always reports the first provider in the chain, and who
    actually answered is returned per call via `generate_structured_with_info`.
    `degraded=True` means the answer came from the mock fallback, not a model.
    """

    def __init__(
        self,
        providers: Union[AIProvider, Sequence[AIProvider]],
        *,
        fallback: AIProvider | None = None,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.5,
        enable_fallback: bool = True,
    ):
        chain = [providers] if isinstance(providers, AIProvider) else list(providers)
        if not chain:
            raise ValueError("ResilientAIProvider needs at least one provider")
        self._chain: List[AIProvider] = chain
        self._fallback = fallback or MockAIProvider()
        self._max_retries = max_retries
        self._backoff = retry_backoff_seconds
        self._enable_fallback = enable_fallback
        self.name = chain[0].name

    @property
    def chain_names(self) -> List[str]:
        return [p.name for p in self._chain]

    async def _try_provider(
        self, provider: AIProvider, method_name: str, *args, **kwargs
    ) -> Tuple[bool, Any, Exception | None]:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                result = await getattr(provider, method_name)(*args, **kwargs)
                if attempt > 0:
                    logger.info("AI provider %s recovered on attempt %d", provider.name, attempt + 1)
                return True, result, None
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "AI provider %s failed (attempt %d/%d): %s",
                    provider.name,
                    attempt + 1,
                    self._max_retries + 1,
                    exc,
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(self._backoff * (2**attempt))
        return False, None, last_exc

    async def _call_with_resilience(
        self, method_name: str, *args, **kwargs
    ) -> Tuple[Any, GenerationInfo]:
        last_exc: Exception | None = None
        for provider in self._chain:
            ok, result, last_exc = await self._try_provider(provider, method_name, *args, **kwargs)
            if ok:
                return result, GenerationInfo(provider=provider.name)
            logger.error("AI provider %s exhausted retries", provider.name)

        if self._enable_fallback:
            logger.error(
                "All AI providers %s failed; falling back to %s",
                self.chain_names,
                self._fallback.name,
            )
            result = await getattr(self._fallback, method_name)(*args, **kwargs)
            return result, GenerationInfo(provider=self._fallback.name, degraded=True)

        raise AIProviderError(
            f"AI providers {self.chain_names} failed after retries: {last_exc}"
        ) from last_exc

    async def generate_text(self, prompt: str, system_instruction: Optional[str] = None) -> str:
        result, _ = await self._call_with_resilience(
            "generate_text", prompt, system_instruction=system_instruction
        )
        return result

    async def generate_structured(
        self,
        prompt: str,
        response_model: Type[SchemaT],
        system_instruction: Optional[str] = None,
    ) -> SchemaT:
        result, _ = await self.generate_structured_with_info(
            prompt, response_model, system_instruction=system_instruction
        )
        return result

    async def generate_structured_with_info(
        self,
        prompt: str,
        response_model: Type[SchemaT],
        system_instruction: Optional[str] = None,
    ) -> Tuple[SchemaT, GenerationInfo]:
        return await self._call_with_resilience(
            "generate_structured",
            prompt,
            response_model,
            system_instruction=system_instruction,
        )
