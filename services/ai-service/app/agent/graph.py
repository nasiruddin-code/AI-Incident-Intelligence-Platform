"""LangGraph-style investigation agent workflow (Week 12).

Uses a StateGraph when langgraph is available; otherwise falls back to a
deterministic sequential investigation path so the service still runs offline.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict

from app.agent import tools as agent_tools
from app.core.logging import get_logger
from app.providers.base import AIProvider
from app.schemas.agent import AgentInvestigateRequest, AgentInvestigateResponse, ToolCallRecord

logger = get_logger(__name__)


class AgentState(TypedDict, total=False):
    incident_id: Optional[str]
    question: Optional[str]
    run_correlation: bool
    run_rca: bool
    run_runbook: bool
    max_steps: int
    steps: List[Dict[str, Any]]
    findings: Dict[str, Any]
    summary: str


def _record(tool: str, input_summary: str, success: bool, output: Any) -> ToolCallRecord:
    if isinstance(output, dict):
        out_summary = json_summary(output)
    else:
        out_summary = str(output)[:300]
    return ToolCallRecord(
        tool=tool,
        input_summary=input_summary,
        success=success,
        output_summary=out_summary,
    )


def json_summary(data: Dict[str, Any], limit: int = 400) -> str:
    import json
    try:
        s = json.dumps(data, default=str)
    except Exception:
        s = str(data)
    return s[:limit] + ("…" if len(s) > limit else "")


async def run_investigation(
    req: AgentInvestigateRequest,
    provider: AIProvider,
) -> AgentInvestigateResponse:
    """Execute the investigation workflow.

    Preferred path: LangGraph StateGraph (if importable).
    Fallback: deterministic tool sequence that still produces a full report.
    """
    try:
        return await _run_langgraph(req, provider)
    except Exception as exc:
        logger.warning("LangGraph path unavailable or failed (%s); using sequential fallback", exc)
        return await _run_sequential(req, provider)


async def _run_sequential(
    req: AgentInvestigateRequest,
    provider: AIProvider,
) -> AgentInvestigateResponse:
    steps: List[ToolCallRecord] = []
    findings: Dict[str, Any] = {}
    incident_id = req.incident_id

    # 1. Resolve incident
    if not incident_id:
        listed = agent_tools.tool_list_incidents()
        steps.append(_record("list_incidents", "", True, listed))
        findings["list_incidents"] = listed
        if listed.get("count", 0) == 0:
            return AgentInvestigateResponse(
                incident_id=None,
                question=req.question,
                steps=steps,
                findings=findings,
                summary="No incidents found. Create one via /incidents/context or /incidents/scenarios/run.",
                provider=provider.name,
            )
        incident_id = listed["incidents"][0]["incident_id"]
        findings["selected_incident_id"] = incident_id

    info = agent_tools.tool_get_incident(incident_id)
    ok = "error" not in info
    steps.append(_record("get_incident", f"incident_id={incident_id}", ok, info))
    findings["incident"] = info

    try:
        obs = await agent_tools.tool_observability_health()
        steps.append(_record("observability_health", "", True, obs))
        findings["observability_health"] = obs
    except Exception as exc:
        steps.append(_record("observability_health", "", False, {"error": str(exc)}))

    if not ok:
        return AgentInvestigateResponse(
            incident_id=incident_id,
            question=req.question,
            steps=steps,
            findings=findings,
            summary=info.get("error", "Incident not found"),
            provider=provider.name,
        )

    # 2. Search evidence (keyword from question or "error")
    query = "error"
    if req.question:
        for kw in ("timeout", "latency", "fail", "error", "gateway", "payment"):
            if kw in req.question.lower():
                query = kw
                break
    evidence = agent_tools.tool_search_evidence(incident_id, query)
    steps.append(_record("search_evidence", f"query={query}", True, evidence))
    findings["evidence"] = evidence

    # 3. Correlate
    if req.run_correlation:
        corr = agent_tools.tool_correlate(incident_id)
        steps.append(_record("correlate", f"incident_id={incident_id}", "error" not in corr, corr))
        findings["correlation"] = corr

    # 4. RCA
    if req.run_rca:
        rca = await agent_tools.tool_run_rca(incident_id, provider)
        steps.append(_record("run_rca", f"incident_id={incident_id}", "error" not in rca, rca))
        findings["rca"] = rca

    # 5. Runbook
    if req.run_runbook:
        rb = await agent_tools.tool_generate_runbook(incident_id, provider)
        steps.append(_record("generate_runbook", f"incident_id={incident_id}", "error" not in rb, rb))
        findings["runbook"] = rb

    summary = _build_summary(incident_id, findings, req.question)
    return AgentInvestigateResponse(
        incident_id=incident_id,
        question=req.question,
        steps=steps,
        findings=findings,
        summary=summary,
        provider=provider.name,
    )


async def _run_langgraph(
    req: AgentInvestigateRequest,
    provider: AIProvider,
) -> AgentInvestigateResponse:
    """LangGraph StateGraph: plan → tools → summarize."""
    from langgraph.graph import END, StateGraph

    async def node_resolve(state: AgentState) -> AgentState:
        steps = list(state.get("steps") or [])
        findings = dict(state.get("findings") or {})
        incident_id = state.get("incident_id")

        if not incident_id:
            listed = agent_tools.tool_list_incidents()
            steps.append(_record("list_incidents", "", True, listed).model_dump())
            findings["list_incidents"] = listed
            if listed.get("count", 0) > 0:
                incident_id = listed["incidents"][0]["incident_id"]
                findings["selected_incident_id"] = incident_id

        if incident_id:
            info = agent_tools.tool_get_incident(incident_id)
            steps.append(
                _record("get_incident", f"incident_id={incident_id}", "error" not in info, info).model_dump()
            )
            findings["incident"] = info

        return {
            **state,
            "incident_id": incident_id,
            "steps": steps,
            "findings": findings,
        }

    async def node_evidence(state: AgentState) -> AgentState:
        steps = list(state.get("steps") or [])
        findings = dict(state.get("findings") or {})
        incident_id = state.get("incident_id")
        if not incident_id or findings.get("incident", {}).get("error"):
            return state

        query = "error"
        q = (state.get("question") or "").lower()
        for kw in ("timeout", "latency", "fail", "error", "gateway", "payment"):
            if kw in q:
                query = kw
                break
        evidence = agent_tools.tool_search_evidence(incident_id, query)
        steps.append(_record("search_evidence", f"query={query}", True, evidence).model_dump())
        findings["evidence"] = evidence
        return {**state, "steps": steps, "findings": findings}

    async def node_correlate(state: AgentState) -> AgentState:
        if not state.get("run_correlation"):
            return state
        steps = list(state.get("steps") or [])
        findings = dict(state.get("findings") or {})
        incident_id = state.get("incident_id")
        if not incident_id:
            return state
        corr = agent_tools.tool_correlate(incident_id)
        steps.append(
            _record("correlate", f"incident_id={incident_id}", "error" not in corr, corr).model_dump()
        )
        findings["correlation"] = corr
        return {**state, "steps": steps, "findings": findings}

    async def node_rca(state: AgentState) -> AgentState:
        if not state.get("run_rca"):
            return state
        steps = list(state.get("steps") or [])
        findings = dict(state.get("findings") or {})
        incident_id = state.get("incident_id")
        if not incident_id:
            return state
        rca = await agent_tools.tool_run_rca(incident_id, provider)
        steps.append(
            _record("run_rca", f"incident_id={incident_id}", "error" not in rca, rca).model_dump()
        )
        findings["rca"] = rca
        return {**state, "steps": steps, "findings": findings}

    async def node_runbook(state: AgentState) -> AgentState:
        if not state.get("run_runbook"):
            return state
        steps = list(state.get("steps") or [])
        findings = dict(state.get("findings") or {})
        incident_id = state.get("incident_id")
        if not incident_id:
            return state
        rb = await agent_tools.tool_generate_runbook(incident_id, provider)
        steps.append(
            _record(
                "generate_runbook", f"incident_id={incident_id}", "error" not in rb, rb
            ).model_dump()
        )
        findings["runbook"] = rb
        return {**state, "steps": steps, "findings": findings}

    async def node_summarize(state: AgentState) -> AgentState:
        summary = _build_summary(
            state.get("incident_id"),
            state.get("findings") or {},
            state.get("question"),
        )
        return {**state, "summary": summary}

    graph = StateGraph(AgentState)
    graph.add_node("resolve", node_resolve)
    graph.add_node("evidence", node_evidence)
    graph.add_node("correlate", node_correlate)
    graph.add_node("rca", node_rca)
    graph.add_node("runbook", node_runbook)
    graph.add_node("summarize", node_summarize)

    graph.set_entry_point("resolve")
    graph.add_edge("resolve", "evidence")
    graph.add_edge("evidence", "correlate")
    graph.add_edge("correlate", "rca")
    graph.add_edge("rca", "runbook")
    graph.add_edge("runbook", "summarize")
    graph.add_edge("summarize", END)

    app = graph.compile()
    initial: AgentState = {
        "incident_id": req.incident_id,
        "question": req.question,
        "run_correlation": req.run_correlation,
        "run_rca": req.run_rca,
        "run_runbook": req.run_runbook,
        "max_steps": req.max_steps,
        "steps": [],
        "findings": {},
        "summary": "",
    }
    final = await app.ainvoke(initial)

    step_records = [ToolCallRecord.model_validate(s) for s in (final.get("steps") or [])]
    return AgentInvestigateResponse(
        incident_id=final.get("incident_id"),
        question=final.get("question"),
        steps=step_records,
        findings=final.get("findings") or {},
        summary=final.get("summary") or "",
        provider=provider.name,
    )


def _build_summary(
    incident_id: Optional[str],
    findings: Dict[str, Any],
    question: Optional[str],
) -> str:
    parts: List[str] = []
    if question:
        parts.append(f"Question: {question}")
    if not incident_id:
        parts.append("No incident available to investigate.")
        return " ".join(parts)

    info = findings.get("incident") or {}
    parts.append(
        f"Investigated incident {incident_id} "
        f"('{info.get('title', '?')}', severity={info.get('severity', '?')})."
    )

    corr = findings.get("correlation") or {}
    if corr and "error" not in corr:
        parts.append(
            f"Correlation: severity_score={corr.get('severity_score')} "
            f"({corr.get('severity_level')}), "
            f"{corr.get('anomaly_count', 0)} anomalies, "
            f"{corr.get('link_count', 0)} links."
        )

    rca = findings.get("rca") or {}
    if rca and "error" not in rca:
        parts.append(
            f"RCA primary cause: {rca.get('primary_cause')} "
            f"(confidence={rca.get('confidence')})."
        )

    rb = findings.get("runbook") or {}
    if rb and "error" not in rb:
        n_steps = len(rb.get("steps") or [])
        n_rem = len(rb.get("remediation_actions") or [])
        parts.append(
            f"Runbook '{rb.get('title')}' with {n_steps} steps and "
            f"{n_rem} remediation actions (confidence={rb.get('confidence')})."
        )

    return " ".join(parts)
