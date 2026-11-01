"""
The AI provider abstraction.

Every place in the codebase that needs "ask an LLM something" talks to this
interface, never to a vendor SDK directly. That is what lets Week 3 add
Gemini, and a later stretch goal ("multi-provider expansion") add OpenAI,
Anthropic, or a local model, without touching any endpoint or service code —
only app/providers/factory.py needs to learn about the new provider.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Tuple, Type, TypeVar

from pydantic import BaseModel

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class AIProviderError(Exception):
    """Raised when the underlying model/provider call fails or returns
    something that cannot be parsed into the expected shape."""


@dataclass(frozen=True)
class GenerationInfo:
    """Per-call attribution: which backend actually answered, and whether
    that answer came from a fallback rather than the configured provider."""

    provider: str
    degraded: bool = False


class AIProvider(ABC):
    """Common contract every concrete provider must implement."""

    #: Short machine-readable name, surfaced in API responses / health checks
    #: so callers can tell which backend actually produced an answer.
    name: str = "base"

    @abstractmethod
    async def generate_text(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
    ) -> str:
        """Free-form text generation."""
        raise NotImplementedError

    @abstractmethod
    async def generate_structured(
        self,
        prompt: str,
        response_model: Type[SchemaT],
        system_instruction: Optional[str] = None,
    ) -> SchemaT:
        """Generate a response and validate/parse it into `response_model`.

        This is the method almost everything in the platform should use:
        log analysis, RCA, runbook generation, etc. all want a typed,
        validated object back — not a raw string to regex apart.
        """
        raise NotImplementedError

    async def generate_structured_with_info(
        self,
        prompt: str,
        response_model: Type[SchemaT],
        system_instruction: Optional[str] = None,
    ) -> Tuple[SchemaT, GenerationInfo]:
        """Like `generate_structured`, but also reports who produced the answer.

        Wrappers (e.g. the resilient provider) override this so callers can
        surface fallback usage per request instead of via shared state.
        """
        result = await self.generate_structured(
            prompt, response_model, system_instruction=system_instruction
        )
        return result, GenerationInfo(provider=self.name)
