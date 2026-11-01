from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class LogAnalysisRequest(BaseModel):
    log_content: str = Field(..., min_length=1, description="Raw log line(s) to analyze")
    service_name: Optional[str] = Field(None, description="Name of the service the log came from")
    context: Optional[str] = Field(None, description="Any extra context useful for triage")


class LogAnalysisResult(BaseModel):
    """This is the schema the AI provider is asked to fill in. Keeping it
    separate from `LogAnalysisResponse` means the same shape can be reused
    directly as a `response_model` for structured generation."""

    severity: Severity
    category: str
    summary: str
    probable_root_cause: str
    recommended_action: str
    confidence: float = Field(ge=0.0, le=1.0)


class LogAnalysisResponse(BaseModel):
    request_id: str
    provider: str
    degraded: bool = Field(
        False,
        description="True if the configured AI provider failed and a fallback answered",
    )
    analysis: LogAnalysisResult
