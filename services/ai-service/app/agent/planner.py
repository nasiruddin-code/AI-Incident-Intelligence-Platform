"""Model-driven investigation agent (planner mode).

Each step the model returns an AgentAction (structured output) naming one
tool; the tool runs and its result is fed back on the next step, until the
model chooses 'finish' or max_steps is reached. Works with every provider
because it relies only on generate_structured, not vendor tool calling.

Guardrails:
- tools disabled by the request (run_rca / run_runbook / run_correlation)
  are not offered and are refused if requested
- identical repeat calls return the earlier result instead of re-running
- if the answer comes from the mock fallback (model unavailable), the
  planner aborts with PlannerUnavailable and the caller uses the fixed path
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from starlette.concurrency import run_in_threadpool

from app.agent import tools as agent_tools
from app.core.logging import get_logger
from app.prompts.agent import AGENT_PLANNER_PROMPT
from app.providers.base import AIProvider
from app.schemas.agent import AgentAction, AgentInvestigateRequest, AgentInvestigateResponse, ToolCallRecord

logger = get_logger(__name__)

HISTORY_OBSERVATION_CHARS = 3000

# tool -> key used in response findings (kept compatible with the fixed pipeline)
FINDING_KEYS = {
    "list_incidents": "list_incidents",
    "get_incident": "incident",
    "search_evidence": "evidence",
    "collect_live_telemetry": "live_telemetry",
    "correlate": "correlation",
    "run_rca": "rca",
    "generate_runbook": "runbook",
    "query_prometheus": "prometheus",
    "query_loki": "loki",
    "search_tempo": "tempo",
    "get_trace": "trace",
}

NEEDS_INCIDENT = {
    "get_incident",
    "search_evidence",
    "collect_live_telemetry",
    "correlate",
    "run_rca",
    "generate_runbook",
}


class PlannerUnavailable(Exception):
    """The model could not drive the investigation (e.g. mock fallback answered)."""


def _available_tools(req: AgentInvestigateRequest) -> List[str]:
    disabled = set()
    if not req.run_correlation:
        disabled.add("correlate")
    if not req.run_rca:
        disabled.add("run_rca")
    if not req.run_runbook:
        disabled.add("generate_runbook")
    return [t for t in FINDING_KEYS if t not in disabled]


def _truncate(data: Any, limit: int) -> str:
    try:
        text = json.dumps(data, default=str)
    except (TypeError, ValueError):
        text = str(data)
    return text[:limit] + ("…" if len(text) > limit else "")


def _tool_args(action: AgentAction, incident_id: Optional[str]) -> Dict[str, Any]:
    tool = action.tool
    if tool in NEEDS_INCIDENT:
        return {"incident_id": action.incident_id or incident_id}
    if tool == "query_prometheus":
        return {"promql": action.query or "up"}
    if tool == "query_loki":
        return {"logql": action.query or '{job=~".+"}'}
    if tool == "search_tempo":
        return {"service": action.service}
    if tool == "get_trace":
        return {"trace_id": action.trace_id}
    return {}


async def _dispatch(
    tool: str, args: Dict[str, Any], provider: AIProvider, query: Optional[str]
) -> Dict[str, Any]:
    if tool in NEEDS_INCIDENT and not args.get("incident_id"):
        return {"error": "incident_id is required; call list_incidents first"}
    if tool == "get_trace" and not args.get("trace_id"):
        return {"error": "trace_id is required"}

    incident_id = args.get("incident_id")
    sync_tools: Dict[str, Callable[[], Dict[str, Any]]] = {
        "list_incidents": lambda: agent_tools.tool_list_incidents(),
        "get_incident": lambda: agent_tools.tool_get_incident(incident_id),
        "search_evidence": lambda: agent_tools.tool_search_evidence(incident_id, query or ""),
        "correlate": lambda: agent_tools.tool_correlate(incident_id),
    }
    if tool in sync_tools:
        return await run_in_threadpool(sync_tools[tool])

    async_tools: Dict[str, Callable[[], Awaitable[Dict[str, Any]]]] = {
        "collect_live_telemetry": lambda: agent_tools.tool_collect_live_telemetry(incident_id),
        "run_rca": lambda: agent_tools.tool_run_rca(incident_id, provider),
        "generate_runbook": lambda: agent_tools.tool_generate_runbook(incident_id, provider),
        "query_prometheus": lambda: agent_tools.tool_query_prometheus(args["promql"]),
        "query_loki": lambda: agent_tools.tool_query_loki(args["logql"]),
        "search_tempo": lambda: agent_tools.tool_search_tempo(args["service"]),
        "get_trace": lambda: agent_tools.tool_get_trace(args["trace_id"]),
    }
    return await async_tools[tool]()


def _format_history(history: List[Tuple[AgentAction, str, Dict[str, Any]]]) -> str:
    if not history:
        return "(none yet)"
    lines = []
    for i, (action, input_summary, observation) in enumerate(history, 1):
        lines.append(
            f"{i}. tool={action.tool} {input_summary}\n"
            f"   thought: {action.thought}\n"
            f"   result: {_truncate(observation, HISTORY_OBSERVATION_CHARS)}"
        )
    return "\n".join(lines)


async def run_planner(
    req: AgentInvestigateRequest,
    provider: AIProvider,
    build_summary: Callable[[Optional[str], Dict[str, Any], Optional[str]], str],
) -> AgentInvestigateResponse:
    if provider.name == "mock":
        raise PlannerUnavailable("the mock provider cannot plan an investigation")
    available = _available_tools(req)
    tool_lines = "\n".join(
        f"- {name}: {agent_tools.TOOL_DESCRIPTIONS[name]}" for name in available
    ) + "\n- finish: End the investigation. Args: final_answer."

    incident_id = req.incident_id
    steps: List[ToolCallRecord] = []
    findings: Dict[str, Any] = {}
    history: List[Tuple[AgentAction, str, Dict[str, Any]]] = []
    results_cache: Dict[str, Dict[str, Any]] = {}
    answered_by = provider.name
    final_answer: Optional[str] = None

    for step in range(req.max_steps):
        prompt = AGENT_PLANNER_PROMPT.render(
            question=req.question or "Investigate this incident: what happened, why, and what should be done?",
            incident_id=incident_id or "(none selected — use list_incidents)",
            steps_remaining=str(req.max_steps - step),
            tools=tool_lines,
            history=_format_history(history),
        )
        action, info = await provider.generate_structured_with_info(
            prompt, AgentAction, system_instruction=AGENT_PLANNER_PROMPT.system_instruction
        )
        if info.degraded:
            raise PlannerUnavailable(f"planner model unavailable (answered by {info.provider})")
        answered_by = info.provider

        if action.tool == "finish":
            final_answer = action.final_answer or ""
            break

        args = _tool_args(action, incident_id)
        query = action.query if action.tool == "search_evidence" else None
        input_summary = " ".join(f"{k}={v}" for k, v in {**args, "query": query}.items() if v)
        cache_key = f"{action.tool}:{json.dumps({**args, 'query': query}, sort_keys=True)}"

        if action.tool not in available:
            observation = {"error": f"tool '{action.tool}' is not available in this investigation"}
        elif cache_key in results_cache:
            observation = {
                "note": "duplicate call — result unchanged from earlier step",
                "result": results_cache[cache_key],
            }
        else:
            try:
                observation = await _dispatch(action.tool, args, provider, query)
            except Exception as exc:  # a failing tool is reported to the model, not fatal
                logger.warning("Agent tool %s failed: %s", action.tool, exc)
                observation = {"error": str(exc)}
            results_cache[cache_key] = observation

        ok = "error" not in observation
        if action.tool == "get_incident" and ok:
            incident_id = args["incident_id"]
        elif action.tool in NEEDS_INCIDENT and ok and not incident_id:
            incident_id = args["incident_id"]

        steps.append(
            ToolCallRecord(
                tool=action.tool,
                input_summary=input_summary,
                success=ok,
                output_summary=_truncate(observation, 400),
                thought=action.thought,
            )
        )
        if ok:
            findings[FINDING_KEYS.get(action.tool, action.tool)] = observation
        history.append((action, input_summary, observation))

    if final_answer:
        summary = final_answer
    else:
        summary = build_summary(incident_id, findings, req.question) + (
            f" (Planner stopped after {req.max_steps} step(s) without a final answer.)"
        )

    return AgentInvestigateResponse(
        incident_id=incident_id,
        question=req.question,
        steps=steps,
        findings=findings,
        summary=summary,
        provider=answered_by,
        mode="planner",
    )
