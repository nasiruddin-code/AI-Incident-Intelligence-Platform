"""Runbook & remediation schemas (Week 11)."""
from typing import List, Optional

from pydantic import BaseModel, Field

from app.schemas.common import AIUsage


class RunbookStep(BaseModel):
    """A single step in an incident response runbook."""
    order: int = Field(..., ge=1)
    action: str = Field(..., description="What to do")
    command_or_check: Optional[str] = Field(
        None,
        description="Optional command, query, or dashboard check",
    )
    expected_result: Optional[str] = Field(
        None,
        description="What success looks like for this step",
    )
    rollback: Optional[str] = Field(
        None,
        description="How to undo this step if it makes things worse",
    )


class RemediationAction(BaseModel):
    """A concrete remediation recommendation."""
    priority: int = Field(..., ge=1, description="1 = do first")
    action: str
    rationale: str
    risk_level: str = Field(
        ...,
        description="low | medium | high",
    )
    automated: bool = Field(
        default=False,
        description="Whether this could be auto-executed by an agent later",
    )
    confidence: float = Field(..., ge=0.0, le=1.0)


class RunbookResult(BaseModel):
    """Structured runbook + remediation output."""
    title: str = Field(..., description="Runbook title")
    summary: str = Field(..., description="When to use this runbook / situation overview")
    steps: List[RunbookStep] = Field(default_factory=list)
    remediation_actions: List[RemediationAction] = Field(default_factory=list)
    verification_checks: List[str] = Field(
        default_factory=list,
        description="Post-remediation checks to confirm recovery",
    )
    escalation_guidance: Optional[str] = Field(
        None,
        description="When and to whom to escalate",
    )
    confidence: float = Field(..., ge=0.0, le=1.0, description="Overall confidence in this runbook")
    estimated_time_minutes: Optional[int] = Field(
        None,
        description="Rough time to complete the runbook",
    )


class RunbookResponse(BaseModel):
    """API response wrapper."""
    incident_id: str
    provider: str
    degraded: bool = Field(
        False,
        description="True if the configured AI provider failed and a fallback answered",
    )
    usage: Optional[AIUsage] = None
    runbook: RunbookResult
    rca_primary_cause: Optional[str] = Field(
        None,
        description="Primary cause from prior RCA if available",
    )
