"""Investigation agent schemas (Week 12)."""
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

AgentToolName = Literal[
    "list_incidents",
    "get_incident",
    "search_evidence",
    "collect_live_telemetry",
    "analyze_baseline",
    "correlate",
    "run_rca",
    "generate_runbook",
    "query_prometheus",
    "query_loki",
    "search_tempo",
    "get_trace",
    "finish",
]


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
    mode: Optional[Literal["auto", "planner", "fixed"]] = Field(
        None,
        description="planner = model chooses tools step by step; fixed = deterministic "
        "pipeline; auto/omitted = AGENT_MODE setting",
    )


class AgentAction(BaseModel):
    """One planner step, produced by the model via structured output.

    Arguments are flat optional fields (not a free-form dict) so the schema
    works with every provider's structured-output mode.
    """
    thought: str = Field(..., description="One or two sentences: why this step")
    tool: AgentToolName = Field(..., description="Tool to call next, or 'finish'")
    incident_id: Optional[str] = Field(None, description="Incident to act on (defaults to the current one)")
    query: Optional[str] = Field(
        None,
        description="Keyword for search_evidence, PromQL for query_prometheus, LogQL for query_loki",
    )
    service: Optional[str] = Field(None, description="Service name for search_tempo")
    trace_id: Optional[str] = Field(None, description="Trace ID for get_trace")
    final_answer: Optional[str] = Field(
        None,
        description="Required when tool is 'finish': the answer, citing the evidence found",
    )


class ToolCallRecord(BaseModel):
    tool: str
    input_summary: str
    success: bool
    output_summary: str
    thought: Optional[str] = None


class AgentInvestigateResponse(BaseModel):
    incident_id: Optional[str]
    question: Optional[str]
    steps: List[ToolCallRecord] = Field(default_factory=list)
    findings: Dict[str, Any] = Field(default_factory=dict)
    summary: str
    provider: str
    mode: Literal["planner", "fixed"] = "fixed"
