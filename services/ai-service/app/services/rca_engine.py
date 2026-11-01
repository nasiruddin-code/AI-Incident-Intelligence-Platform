"""AI-assisted Root Cause Analysis engine (Week 10).

Consumes a stored IncidentContext (optionally after correlation) and asks the
AI provider for a structured RCAResult.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from starlette.concurrency import run_in_threadpool
from app.core.config import get_settings
from app.core.logging import get_logger
from app.prompts.rca import RCA_PROMPT
from app.providers.base import AIProvider
from app.schemas.common import AIUsage
from app.schemas.incident import CorrelationResult, IncidentContext, SimilarIncident
from app.schemas.rca import RCAResponse, RCAResult
from app.schemas.telemetry import SeverityLevel, SpanStatus
from app.services import incident_store
from app.services.correlation import correlate_incident

logger = get_logger(__name__)


def _fmt_logs(incident: IncidentContext, limit: int = 20) -> str:
    lines: List[str] = []
    for log in incident.logs[:limit]:
        lines.append(
            f"[{log.timestamp.isoformat()}] {log.severity.value} "
            f"{log.resource.service_name}: {log.body[:200]}"
            + (f" trace={log.trace_id[:12]}…" if log.trace_id else "")
        )
    if len(incident.logs) > limit:
        lines.append(f"… and {len(incident.logs) - limit} more log(s)")
    return "\n".join(lines) if lines else "(none)"


def _fmt_metrics(incident: IncidentContext, limit: int = 15) -> str:
    lines: List[str] = []
    for m in incident.metrics[:limit]:
        attrs = ", ".join(f"{k}={v}" for k, v in (m.attributes or {}).items())
        lines.append(
            f"{m.name}={m.value} {m.unit or ''} type={m.type.value} "
            f"service={m.resource.service_name}"
            + (f" {{{attrs}}}" if attrs else "")
        )
    if len(incident.metrics) > limit:
        lines.append(f"… and {len(incident.metrics) - limit} more metric(s)")
    return "\n".join(lines) if lines else "(none)"


def _fmt_traces(incident: IncidentContext, limit: int = 20) -> str:
    lines: List[str] = []
    for s in incident.traces[:limit]:
        lines.append(
            f"trace={s.trace_id[:12]}… span={s.span_id[:8]} "
            f"{s.resource.service_name}/{s.name} status={s.status.value}"
            + (f" parent={s.parent_span_id[:8]}" if s.parent_span_id else "")
        )
    if len(incident.traces) > limit:
        lines.append(f"… and {len(incident.traces) - limit} more span(s)")
    return "\n".join(lines) if lines else "(none)"


def _fmt_correlation(corr: Optional[CorrelationResult]) -> str:
    if not corr:
        return "(correlation not run — call /correlate first for richer analysis)"
    parts = [
        f"Severity score: {corr.severity.score}/100 ({corr.severity.level.value})",
        f"Rationale: {corr.severity.rationale}",
        f"Anomalies ({len(corr.anomalies)}):",
    ]
    for a in corr.anomalies[:10]:
        parts.append(f"  - [{a.anomaly_type}] {a.description} (score={a.score:.2f})")
    parts.append(f"Correlation links ({len(corr.links)}):")
    for link in corr.links[:10]:
        parts.append(
            f"  - {link.link_type}: {link.source_kind}/{link.source_ref[:40]} "
            f"→ {link.target_kind}/{link.target_ref[:40]} (conf={link.confidence})"
        )
    if corr.service_graph:
        parts.append(f"Service graph: {json.dumps(corr.service_graph)}")
    return "\n".join(parts)


# Keys/lines the RCA and runbook engines write back onto the incident. They are
# left out of the RCA prompt so a re-run is judged on the evidence alone (not
# anchored on the previous answer), and identical evidence yields an identical
# prompt, which the AI response cache can reuse.
_DERIVED_METADATA_PREFIXES = ("rca_", "runbook_", "remediation_")


def _evidence_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in (metadata or {}).items() if not k.startswith(_DERIVED_METADATA_PREFIXES)}


def _evidence_notes(notes: Optional[str]) -> str:
    lines = (notes or "").splitlines()
    return "\n".join(line for line in lines if not line.startswith("[RCA]")).strip()


def _fmt_similar(similar: List[SimilarIncident]) -> str:
    if not similar:
        return "(none found)"
    lines = []
    for s in similar:
        past = (
            f"past RCA: {s.rca_primary_cause} (confidence {s.rca_confidence})"
            if s.rca_primary_cause
            else "no RCA recorded"
        )
        fix = f"; runbook: {s.runbook_title}" if s.runbook_title else ""
        lines.append(
            f"- {s.incident_id} [{s.status.value}] '{s.title}' (similarity {s.score:.2f}; "
            f"shared: {', '.join(s.shared_features[:4])}): {past}{fix}"
        )
    return "\n".join(lines)


class RCAEngine:
    def __init__(self, provider: AIProvider):
        self._provider = provider

    async def analyze(
        self,
        incident_id: str,
        *,
        run_correlation: bool = True,
        correlation: Optional[CorrelationResult] = None,
    ) -> RCAResponse:
        """Produce RCA for a stored incident.

        If run_correlation is True and no correlation is supplied, correlation
        is executed first so the model sees anomaly/severity context.
        """
        incident = await run_in_threadpool(incident_store.get, incident_id)
        if not incident:
            raise KeyError(f"Incident {incident_id} not found")

        corr = correlation
        if run_correlation and corr is None:
            try:
                corr = await run_in_threadpool(correlate_incident, incident_id)
                incident = corr.incident  # refreshed
            except Exception as exc:
                logger.warning("Correlation step failed, continuing without it: %s", exc)

        settings = get_settings()
        similar: List[SimilarIncident] = []
        if settings.SIMILAR_IN_RCA > 0:
            try:
                similar = await run_in_threadpool(
                    lambda: incident_store.find_similar(
                        incident_id, limit=settings.SIMILAR_IN_RCA, min_score=settings.SIMILAR_MIN_SCORE
                    )
                )
            except Exception as exc:  # context only: never block an RCA
                logger.warning("Similar-incident lookup failed for %s: %s", incident_id, exc)

        prompt = RCA_PROMPT.render(
            incident_id=incident.incident_id,
            title=incident.title,
            status=incident.status.value,
            severity=incident.severity.value,
            affected_services=", ".join(incident.affected_services) or "unknown",
            time_window=f"{incident.time_window.start.isoformat()} → {incident.time_window.end.isoformat()}",
            scenario=incident.scenario.value if incident.scenario else "none",
            evidence_summary=incident.evidence_summary or "n/a",
            notes=_evidence_notes(incident.notes) or "n/a",
            metadata=json.dumps(_evidence_metadata(incident.metadata)),
            log_count=str(len(incident.logs)),
            logs_block=_fmt_logs(incident),
            metric_count=str(len(incident.metrics)),
            metrics_block=_fmt_metrics(incident),
            span_count=str(len(incident.traces)),
            traces_block=_fmt_traces(incident),
            correlation_block=_fmt_correlation(corr),
            similar_block=_fmt_similar(similar),
        )

        logger.info(
            "Running RCA for incident %s via provider=%s (logs=%d metrics=%d spans=%d)",
            incident_id,
            self._provider.name,
            len(incident.logs),
            len(incident.metrics),
            len(incident.traces),
        )

        result, info = await self._provider.generate_structured_with_info(
            prompt=prompt,
            response_model=RCAResult,
            system_instruction=RCA_PROMPT.system_instruction,
        )

        # Persist RCA onto incident metadata/notes. Applied to a fresh copy so
        # changes made while the model was answering are kept, not overwritten.
        def record(fresh: IncidentContext) -> None:
            fresh.metadata = {
                **fresh.metadata,
                "rca_primary_cause": result.primary_cause,
                "rca_confidence": result.confidence,
                "rca_provider": info.provider,
                "rca_degraded": info.degraded,
                "rca_prompt": RCA_PROMPT.name,
            }
            if result.summary:
                note_line = f"[RCA] {result.summary}"
                fresh.notes = (fresh.notes + "\n" + note_line) if fresh.notes else note_line

        await run_in_threadpool(incident_store.update, incident_id, record)

        return RCAResponse(
            incident_id=incident_id,
            provider=info.provider,
            degraded=info.degraded,
            usage=AIUsage.from_info(info),
            rca=result,
            similar_incidents=similar,
        )
