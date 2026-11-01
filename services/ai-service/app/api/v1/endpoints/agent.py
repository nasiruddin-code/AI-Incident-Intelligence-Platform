"""Investigation agent endpoints (Week 12)."""
from fastapi import APIRouter

from app.core.dependencies import AIProviderDep
from app.core.logging import get_logger
from app.agent.graph import run_investigation
from app.schemas.agent import AgentInvestigateRequest, AgentInvestigateResponse

router = APIRouter()
logger = get_logger(__name__)


@router.post(
    "/investigate",
    response_model=AgentInvestigateResponse,
    summary="Run AI investigation agent on an incident",
)
async def investigate(
    payload: AgentInvestigateRequest,
    provider: AIProviderDep,
) -> AgentInvestigateResponse:
    """Week 12: LangGraph-style agent that uses tools to investigate an incident.

    Tools: list/get incident, search evidence, correlate, RCA, runbook.
    If incident_id is omitted, the agent picks the most recent incident.
    """
    logger.info(
        "Agent investigate incident_id=%s question=%s",
        payload.incident_id,
        payload.question,
    )
    return await run_investigation(payload, provider)
