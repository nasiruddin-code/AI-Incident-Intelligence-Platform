"""
Gemini implementation of the AIProvider interface, using Google's unified
`google-genai` SDK (the `google-generativeai` package is legacy/deprecated).
"""
from typing import Optional, Tuple, Type

from google import genai
from google.genai import types

from app.providers.base import AIProvider, AIProviderError, GenerationInfo, SchemaT


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
            result = response_model.model_validate_json(response.text)
        except Exception as exc:
            raise AIProviderError(
                f"Gemini response did not match {response_model.__name__}: {exc}"
            ) from exc

        usage = getattr(response, "usage_metadata", None)
        return result, GenerationInfo(
            provider=self.name,
            model=self._model,
            input_tokens=getattr(usage, "prompt_token_count", None) or 0,
            output_tokens=getattr(usage, "candidates_token_count", None) or 0,
        )
