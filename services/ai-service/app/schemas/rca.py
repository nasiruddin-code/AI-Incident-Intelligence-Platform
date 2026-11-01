"""Root Cause Analysis schemas (Week 10)."""
from typing import List, Optional

from pydantic import BaseModel, Field


class RCAHypothesis(BaseModel):
    """A ranked root-cause hypothesis with supporting evidence."""
    rank: int = Field(..., ge=1, description="1 = most likely")
    cause: str = Field(..., description="Concise statement of the hypothesized root cause")
    confidence: float = Field(..., ge=0.0, le=1.0)
    evidence: List[str] = Field(
        default_factory=list,
        description="Specific evidence items (log lines, span names, metric values) supporting this hypothesis",
    )
    affected_component: Optional[str] = Field(
        None, description="Primary service or component implicated"
    )


class RCAResult(BaseModel):
    """Structured RCA output from the AI engine."""
    summary: str = Field(..., description="1–3 sentence executive summary of the root cause analysis")
    primary_cause: str = Field(..., description="Most likely root cause (mirrors hypothesis rank 1)")
    hypotheses: List[RCAHypothesis] = Field(
        default_factory=list,
        description="Ranked list of root-cause hypotheses",
    )
    contributing_factors: List[str] = Field(
        default_factory=list,
        description="Secondary factors that amplified or enabled the failure",
    )
    recommended_next_steps: List[str] = Field(
        default_factory=list,
        description="Immediate investigation or mitigation steps",
    )
    confidence: float = Field(..., ge=0.0, le=1.0, description="Overall confidence in the analysis")
    evidence_gaps: List[str] = Field(
        default_factory=list,
        description="What additional telemetry would strengthen the analysis",
    )


class RCAResponse(BaseModel):
    """API response wrapper for RCA."""
    incident_id: str
    provider: str
    degraded: bool = Field(
        False,
        description="True if the configured AI provider failed and a fallback answered",
    )
    rca: RCAResult
