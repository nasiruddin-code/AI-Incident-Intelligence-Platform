"""
Gemini implementation of the AIProvider interface, using Google's unified
`google-genai` SDK (the `google-generativeai` package is legacy/deprecated).
"""
from typing import Optional, Type

from google import genai
from google.genai import types

from app.providers.base import AIProvider, AIProviderError, SchemaT


class GeminiProvider(AIProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str):
        self._client = genai.Client(api_key=api_key)
        self._model = model

    async def generate_text(self, prompt: str, system_instruction: Optional[str] = None) -> str:
        try:
            config = types.GenerateContentConfig(system_instruction=system_instruction) if system_instruction else None
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=prompt,
                config=config,
            )
            return response.text or ""
        except Exception as exc:  # SDK-level errors -> our own error type
            raise AIProviderError(f"Gemini text generation failed: {exc}") from exc

    async def generate_structured(
        self,
        prompt: str,
        response_model: Type[SchemaT],
        system_instruction: Optional[str] = None,
    ) -> SchemaT:
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                    response_schema=response_model,
                ),
            )
        except Exception as exc:
            raise AIProviderError(f"Gemini structured generation failed: {exc}") from exc

        if not response.text:
            raise AIProviderError("Gemini returned an empty response")

        try:
            return response_model.model_validate_json(response.text)
        except Exception as exc:
            raise AIProviderError(
                f"Gemini response did not match {response_model.__name__}: {exc}"
            ) from exc
