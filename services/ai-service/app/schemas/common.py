from typing import Any, Optional

from pydantic import BaseModel, Field


class AIUsage(BaseModel):
    """Token usage and latency for one AI call (zeros when not reported)."""
    model: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    cached: bool = Field(False, description="Served from the AI response cache")

    @classmethod
    def from_info(cls, info: Any) -> "AIUsage":
        return cls(
            model=info.model,
            input_tokens=info.input_tokens,
            output_tokens=info.output_tokens,
            latency_ms=info.latency_ms,
            cached=info.cached,
        )


class HealthResponse(BaseModel):
    status: str
    app_name: str
    version: str
    environment: str


class ReadinessResponse(BaseModel):
    status: str
    ai_provider: str
    database: str = "memory"  # memory | postgres | error
