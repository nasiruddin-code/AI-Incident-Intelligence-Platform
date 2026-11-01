"""Resilient AI provider: retries, provider chain, and mock fallback (Week 15).

Also the single choke point for AI cost control: every call is timed and
its token usage / estimated cost recorded (app.core.ai_metrics), and
structured responses from real models are served from an optional cache.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import Any, Dict, List, Optional, Sequence, Tuple, Type, Union

from app.core.ai_metrics import AIMetrics, ai_metrics, estimate_cost_usd
from app.core.logging import get_logger
from app.providers.base import AIProvider, AIProviderError, GenerationInfo, SchemaT
from app.providers.mock_provider import MockAIProvider
from app.providers.response_cache import ResponseCache

logger = get_logger(__name__)

_STRUCTURED = "generate_structured_with_info"


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
        cache: Optional[ResponseCache] = None,
        prices: Optional[Dict[str, List[float]]] = None,
        metrics: AIMetrics = ai_metrics,
    ):
        chain = [providers] if isinstance(providers, AIProvider) else list(providers)
        if not chain:
            raise ValueError("ResilientAIProvider needs at least one provider")
        self._chain: List[AIProvider] = chain
        self._fallback = fallback or MockAIProvider()
        self._max_retries = max_retries
        self._backoff = retry_backoff_seconds
        self._enable_fallback = enable_fallback
        self._cache = cache
        self._prices = prices or {}
        self._metrics = metrics
        self.name = chain[0].name

    @property
    def chain_names(self) -> List[str]:
        return [p.name for p in self._chain]

    async def _invoke(
        self, provider: AIProvider, method_name: str, operation: str, *args, **kwargs
    ) -> Tuple[Any, GenerationInfo]:
        """One call to one provider, timed and recorded in the AI metrics."""
        started = time.perf_counter()
        try:
            out = await getattr(provider, method_name)(*args, **kwargs)
        except Exception:
            self._metrics.record_call(
                provider=provider.name,
                operation=operation,
                outcome="error",
                latency_s=time.perf_counter() - started,
            )
            raise
        elapsed = time.perf_counter() - started
        result, info = out if method_name == _STRUCTURED else (out, GenerationInfo(provider=provider.name))
        info = replace(info, provider=provider.name, latency_ms=round(elapsed * 1000, 1))
        self._metrics.record_call(
            provider=provider.name,
            operation=operation,
            outcome="success",
            latency_s=elapsed,
            model=info.model,
            input_tokens=info.input_tokens,
            output_tokens=info.output_tokens,
            cost_usd=estimate_cost_usd(self._prices, info.model, info.input_tokens, info.output_tokens),
        )
        return result, info

    async def _try_provider(
        self, provider: AIProvider, method_name: str, operation: str, *args, **kwargs
    ) -> Tuple[bool, Any, Optional[GenerationInfo], Exception | None]:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                result, info = await self._invoke(provider, method_name, operation, *args, **kwargs)
                if attempt > 0:
                    logger.info("AI provider %s recovered on attempt %d", provider.name, attempt + 1)
                return True, result, info, None
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
        return False, None, None, last_exc

    async def _call_with_resilience(
        self, method_name: str, operation: str, *args, **kwargs
    ) -> Tuple[Any, GenerationInfo]:
        last_exc: Exception | None = None
        for provider in self._chain:
            ok, result, info, last_exc = await self._try_provider(
                provider, method_name, operation, *args, **kwargs
            )
            if ok:
                return result, info
            logger.error("AI provider %s exhausted retries", provider.name)

        if self._enable_fallback:
            logger.error(
                "All AI providers %s failed; falling back to %s",
                self.chain_names,
                self._fallback.name,
            )
            self._metrics.record_fallback(operation)
            result, info = await self._invoke(self._fallback, method_name, operation, *args, **kwargs)
            return result, replace(info, degraded=True)

        raise AIProviderError(
            f"AI providers {self.chain_names} failed after retries: {last_exc}"
        ) from last_exc

    async def generate_text(self, prompt: str, system_instruction: Optional[str] = None) -> str:
        result, _ = await self._call_with_resilience(
            "generate_text", "text", prompt, system_instruction=system_instruction
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
        operation = response_model.__name__
        key = None
        if self._cache is not None:
            key = ResponseCache.make_key(self.chain_names, operation, system_instruction, prompt)
            hit = self._cache.get(key, response_model)
            if hit is not None:
                self._metrics.record_cache_hit(operation)
                result, info = hit
                return result, replace(info, cached=True, latency_ms=0.0)

        result, info = await self._call_with_resilience(
            _STRUCTURED, operation, prompt, response_model, system_instruction=system_instruction
        )
        # Only real model answers are worth caching; never cache a fallback.
        if key is not None and not info.degraded and info.provider != "mock":
            self._cache.put(key, result, info)
        return result, info
