"""AI-generated runbook & remediation engine (Week 11)."""
from __future__ import annotations

import json
from typing import List, Optional

from app.core.logging import get_logger
from app.prompts.runbook import RUNBOOK_PROMPT
from app.providers.base import AIProvider
from app.schemas.incident import IncidentContext
from app.schemas.runbook import RunbookResponse, RunbookResult
from app.services import incident_store
from app.services.rca_engine import _fmt_logs, _fmt_metrics, _fmt_traces

logger = get_logger(__name__)


def _rca_block(incident: IncidentContext) -> str:
    meta = incident.metadata or {}
    primary = meta.get("rca_primary_cause")
    conf = meta.get("rca_confidence")
    if not primary:
        return (
            "(No prior RCA. Run POST /incidents/{id}/rca first for better runbooks, "
            "or proceed with evidence-only guidance.)"
        )
    lines = [f"Primary cause: {primary}"]
    if conf is not None:
        lines.append(f"RCA confidence: {conf}")
    if incident.notes:
        # Include RCA note lines
        rca_notes = [n for n in incident.notes.split("\n") if n.startswith("[RCA]")]
        if rca_notes:
            lines.append("RCA summary: " + " | ".join(rca_notes))
    return "\n".join(lines)


class RunbookEngine:
    def __init__(self, provider: AIProvider):
        self._provider = provider

    async def generate(self, incident_id: str) -> RunbookResponse:
        incident = incident_store.get(incident_id)
        if not incident:
            raise KeyError(f"Incident {incident_id} not found")

        prompt = RUNBOOK_PROMPT.render(
            incident_id=incident.incident_id,
            title=incident.title,
            severity=incident.severity.value,
            affected_services=", ".join(incident.affected_services) or "unknown",
            scenario=incident.scenario.value if incident.scenario else "none",
            evidence_summary=incident.evidence_summary or "n/a",
            notes=incident.notes or "n/a",
            rca_block=_rca_block(incident),
            logs_block=_fmt_logs(incident),
            metrics_block=_fmt_metrics(incident),
            traces_block=_fmt_traces(incident),
        )

        logger.info(
            "Generating runbook for incident %s via provider=%s",
            incident_id,
            self._provider.name,
        )

        result, info = await self._provider.generate_structured_with_info(
            prompt=prompt,
            response_model=RunbookResult,
            system_instruction=RUNBOOK_PROMPT.system_instruction,
        )

        # Persist key fields for agent / dashboard later
        incident.metadata = {
            **incident.metadata,
            "runbook_title": result.title,
            "runbook_confidence": result.confidence,
            "runbook_provider": info.provider,
            "runbook_degraded": info.degraded,
            "remediation_count": len(result.remediation_actions),
        }
        incident_store.save(incident)

        return RunbookResponse(
            incident_id=incident_id,
            provider=info.provider,
            degraded=info.degraded,
            runbook=result,
            rca_primary_cause=(incident.metadata or {}).get("rca_primary_cause"),
        )
