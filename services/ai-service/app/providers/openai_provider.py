"""
OpenAI-compatible implementation of the AIProvider interface.

Serves both OpenAI and Ollama (which exposes an OpenAI-compatible
/v1/chat/completions endpoint), selected by base_url and name.
Structured output requests a JSON-schema response and validates it with
Pydantic, so the platform's existing schemas work without strict-mode changes.
"""
from typing import Optional, Type

import openai

from app.providers.base import AIProvider, AIProviderError, SchemaT


class OpenAICompatibleProvider(AIProvider):
    def __init__(
        self,
        *,
        name: str,
        model: str,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 120.0,
    ):
        self.name = name
        self._model = model
        # Ollama ignores the key but the SDK requires a non-empty value.
        self._client = openai.AsyncOpenAI(
            api_key=api_key or "not-needed", base_url=base_url, timeout=timeout
        )

    @staticmethod
    def _messages(prompt: str, system_instruction: Optional[str]) -> list:
        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": prompt})
        return messages

    async def _complete(self, messages: list, **kwargs) -> str:
        try:
            response = await self._client.chat.completions.create(
                model=self._model, messages=messages, **kwargs
            )
        except openai.OpenAIError as exc:
            raise AIProviderError(f"{self.name} request failed: {exc}") from exc
        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise AIProviderError(f"{self.name} response was truncated (length limit)")
        return choice.message.content or ""

    async def generate_text(self, prompt: str, system_instruction: Optional[str] = None) -> str:
        return await self._complete(self._messages(prompt, system_instruction))

    async def generate_structured(
        self,
        prompt: str,
        response_model: Type[SchemaT],
        system_instruction: Optional[str] = None,
    ) -> SchemaT:
        text = await self._complete(
            self._messages(prompt, system_instruction),
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": response_model.__name__,
                    "schema": response_model.model_json_schema(),
                    "strict": False,
                },
            },
        )
        if not text:
            raise AIProviderError(f"{self.name} returned an empty response")
        try:
            return response_model.model_validate_json(text)
        except Exception as exc:
            raise AIProviderError(
                f"{self.name} response did not match {response_model.__name__}: {exc}"
            ) from exc
