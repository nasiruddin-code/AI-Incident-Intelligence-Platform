"""Investigation agent schemas (Week 12)."""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class AgentInvestigateRequest(BaseModel):
    """Request to run the investigation agent."""
    incident_id: Optional[str] = Field(
        None,
        description="Existing incident to investigate; if omitted, agent can list recent incidents",
    )
    question: Optional[str] = Field(
        None,
        description="Optional natural-language question, e.g. 'What is the root cause?'",
    )
    run_correlation: bool = True
    run_rca: bool = True
    run_runbook: bool = True
    max_steps: int = Field(default=8, ge=1, le=20)


class ToolCallRecord(BaseModel):
    tool: str
    input_summary: str
    success: bool
    output_summary: str


class AgentInvestigateResponse(BaseModel):
    incident_id: Optional[str]
    question: Optional[str]
    steps: List[ToolCallRecord] = Field(default_factory=list)
    findings: Dict[str, Any] = Field(default_factory=dict)
    summary: str
    provider: str
