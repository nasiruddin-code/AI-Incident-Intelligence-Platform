"""Investigation agent endpoints (Week 12)."""
import asyncio
import json

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.core.dependencies import AIProviderDep
from app.core.logging import get_logger
from app.agent.events import emitting
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


KEEPALIVE_SECONDS = 15


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@router.post(
    "/investigate/stream",
    summary="Run the investigation agent, streaming progress as server-sent events",
    response_class=StreamingResponse,
)
async def investigate_stream(
    payload: AgentInvestigateRequest,
    provider: AIProviderDep,
) -> StreamingResponse:
    """Same request as /investigate; the response is `text/event-stream`:

    - `mode`      {mode: planner|fixed, reason?}
    - `planning`  {step, max_steps}  (planner: the model is choosing the next tool)
    - `step`      a ToolCallRecord as each tool finishes (tool, thought, success, output_summary)
    - `result`    the full AgentInvestigateResponse (last event)
    - `error`     {detail} if the investigation failed

    Comment lines (`: keepalive`) are sent while idle so proxies keep the
    connection open. If the client disconnects, the investigation is cancelled.
    """
    queue: asyncio.Queue = asyncio.Queue()

    def push(event: str, data) -> None:
        queue.put_nowait((event, data))

    async def run() -> None:
        with emitting(push):
            try:
                result = await run_investigation(payload, provider)
                push("result", result.model_dump(mode="json"))
            except Exception as exc:
                logger.exception("Streaming investigation failed")
                push("error", {"detail": str(exc)})
            finally:
                push("end", None)

    async def events():
        task = asyncio.create_task(run())
        try:
            while True:
                try:
                    event, data = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if event == "end":
                    break
                yield _sse(event, data)
        finally:
            if not task.done():
                task.cancel()

    logger.info("Agent investigate (stream) incident_id=%s", payload.incident_id)
    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
