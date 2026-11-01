"""AI-assisted Root Cause Analysis engine (Week 10).

Consumes a stored IncidentContext (optionally after correlation) and asks the
AI provider for a structured RCAResult.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.core.logging import get_logger
from app.prompts.rca import RCA_PROMPT
from app.providers.base import AIProvider
from app.schemas.incident import CorrelationResult, IncidentContext
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
        incident = incident_store.get(incident_id)
        if not incident:
            raise KeyError(f"Incident {incident_id} not found")

        corr = correlation
        if run_correlation and corr is None:
            try:
                corr = correlate_incident(incident_id)
                incident = corr.incident  # refreshed
            except Exception as exc:
                logger.warning("Correlation step failed, continuing without it: %s", exc)

        prompt = RCA_PROMPT.render(
            incident_id=incident.incident_id,
            title=incident.title,
            status=incident.status.value,
            severity=incident.severity.value,
            affected_services=", ".join(incident.affected_services) or "unknown",
            time_window=f"{incident.time_window.start.isoformat()} → {incident.time_window.end.isoformat()}",
            scenario=incident.scenario.value if incident.scenario else "none",
            evidence_summary=incident.evidence_summary or "n/a",
            notes=incident.notes or "n/a",
            metadata=json.dumps(incident.metadata) if incident.metadata else "{}",
            log_count=str(len(incident.logs)),
            logs_block=_fmt_logs(incident),
            metric_count=str(len(incident.metrics)),
            metrics_block=_fmt_metrics(incident),
            span_count=str(len(incident.traces)),
            traces_block=_fmt_traces(incident),
            correlation_block=_fmt_correlation(corr),
        )

        logger.info(
            "Running RCA for incident %s via provider=%s (logs=%d metrics=%d spans=%d)",
            incident_id,
            self._provider.name,
            len(incident.logs),
            len(incident.metrics),
            len(incident.traces),
        )

        result: RCAResult = await self._provider.generate_structured(
            prompt=prompt,
            response_model=RCAResult,
            system_instruction=RCA_PROMPT.system_instruction,
        )

        # Persist RCA onto incident metadata/notes for later weeks
        incident.metadata = {
            **incident.metadata,
            "rca_primary_cause": result.primary_cause,
            "rca_confidence": result.confidence,
            "rca_provider": self._provider.name,
        }
        if result.summary:
            note_line = f"[RCA] {result.summary}"
            incident.notes = (incident.notes + "\n" + note_line) if incident.notes else note_line
        incident_store.save(incident)

        return RCAResponse(
            incident_id=incident_id,
            provider=self._provider.name,
            rca=result,
        )
