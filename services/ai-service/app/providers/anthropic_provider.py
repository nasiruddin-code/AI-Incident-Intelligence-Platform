"""
Claude implementation of the AIProvider interface, using the official
`anthropic` SDK. Structured output uses `messages.parse(output_format=...)`,
which constrains the response to the Pydantic model's JSON schema and
returns a validated instance.
"""
from typing import List, Optional, Type

import anthropic

from app.providers.base import AIProvider, AIProviderError, SchemaT

SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider(AIProvider):
    name = "anthropic"

    def __init__(
        self,
        api_key: Optional[str],
        model: str,
        *,
        effort: str = "high",
        max_tokens: int = 16000,
        server_fallbacks: bool = True,
        timeout: float = 120.0,
    ):
        # api_key=None lets the SDK resolve credentials from its environment chain.
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout)
        self._model = model
        self._effort = effort
        self._max_tokens = max_tokens
        self._betas: List[str] = [SERVER_FALLBACK_BETA] if server_fallbacks else []

    def _request_kwargs(self, prompt: str, system_instruction: Optional[str]) -> dict:
        kwargs: dict = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {"effort": self._effort},
        }
        if system_instruction:
            kwargs["system"] = system_instruction
        if self._betas:
            kwargs["betas"] = self._betas
            kwargs["fallbacks"] = "default"
        return kwargs

    @staticmethod
    def _check_stop(response) -> None:
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise AIProviderError(f"Claude declined the request (refusal, category={category})")
        if response.stop_reason == "max_tokens":
            raise AIProviderError("Claude response was truncated (max_tokens reached)")

    async def generate_text(self, prompt: str, system_instruction: Optional[str] = None) -> str:
        try:
            response = await self._client.beta.messages.create(
                **self._request_kwargs(prompt, system_instruction)
            )
        except anthropic.APIError as exc:
            raise AIProviderError(f"Claude text generation failed: {exc}") from exc
        self._check_stop(response)
        return "".join(b.text for b in response.content if b.type == "text")

    async def generate_structured(
        self,
        prompt: str,
        response_model: Type[SchemaT],
        system_instruction: Optional[str] = None,
    ) -> SchemaT:
        try:
            response = await self._client.beta.messages.parse(
                **self._request_kwargs(prompt, system_instruction),
                output_format=response_model,
            )
        except anthropic.APIError as exc:
            raise AIProviderError(f"Claude structured generation failed: {exc}") from exc
        except Exception as exc:  # schema/validation errors raised while parsing
            raise AIProviderError(
                f"Claude response did not match {response_model.__name__}: {exc}"
            ) from exc

        self._check_stop(response)
        parsed = response.parsed_output
        if parsed is None:
            raise AIProviderError(f"Claude returned no {response_model.__name__} output")
        return parsed
