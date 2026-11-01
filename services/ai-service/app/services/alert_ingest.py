"""Turn Alertmanager notifications into incidents and investigate them.

One alert group (Alertmanager groupKey) maps to one open incident:
- firing, no open incident for the group  -> create it (alerts become log evidence)
- firing, open incident exists            -> add any new alerts; re-investigate
                                             only if new alerts arrived (not on
                                             Alertmanager's periodic repeats)
- resolved                                -> mark the incident RESOLVED

Investigation (enrich from live telemetry -> correlate -> RCA -> runbook) runs
in the background so the webhook answers Alertmanager immediately. Alerts
labelled ai_investigate="false" create/update incidents but are not
investigated (used for the AI service's own health alerts, to avoid an
AI-outage alert triggering more AI calls).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from starlette.concurrency import run_in_threadpool

from app.core.config import Settings
from app.core.logging import get_logger
from app.providers.base import AIProvider
from app.schemas.alerts import AlertIngestResult, AlertmanagerAlert, AlertmanagerWebhook
from app.schemas.incident import BuildContextRequest, IncidentStatus, TimeWindow
from app.schemas.telemetry import LogRecord, Resource, SeverityLevel
from app.services import incident_store
from app.services.context_builder import build_context
from app.services.correlation import _SEV_RANK, correlate_incident
from app.services.notifier import notify, rca_details, runbook_details
from app.services.rca_engine import RCAEngine
from app.services.runbook_engine import RunbookEngine
from app.services.telemetry_enrichment import TelemetryEnrichmentService

logger = get_logger(__name__)

GROUP_KEY = "alert_group_key"

SEVERITY_MAP = {
    "critical": SeverityLevel.FATAL,
    "page": SeverityLevel.FATAL,
    "error": SeverityLevel.ERROR,
    "high": SeverityLevel.ERROR,
    "warning": SeverityLevel.WARN,
    "warn": SeverityLevel.WARN,
    "info": SeverityLevel.INFO,
}
SERVICE_LABELS = ("service", "service_name", "job")


def _severity(alert: AlertmanagerAlert) -> SeverityLevel:
    return SEVERITY_MAP.get(alert.labels.get("severity", "").lower(), SeverityLevel.WARN)


def _service(alert: AlertmanagerAlert) -> str:
    for label in SERVICE_LABELS:
        if alert.labels.get(label):
            return alert.labels[label]
    return "unknown"


def _alert_id(alert: AlertmanagerAlert) -> str:
    return alert.fingerprint or f"{alert.labels.get('alertname')}:{sorted(alert.labels.items())}"


def _as_log(alert: AlertmanagerAlert) -> LogRecord:
    name = alert.labels.get("alertname", "alert")
    summary = alert.annotations.get("summary", "")
    description = alert.annotations.get("description", "")
    body = f"ALERT {name} firing: {summary}" + (f" — {description}" if description else "")
    return LogRecord(
        timestamp=alert.startsAt or datetime.now(timezone.utc),
        severity=_severity(alert),
        body=body,
        attributes={
            "source": "alertmanager",
            "fingerprint": alert.fingerprint,
            "generator_url": alert.generatorURL,
            **{f"label.{k}": v for k, v in alert.labels.items()},
        },
        resource=Resource(service_name=_service(alert)),
    )


def _title(payload: AlertmanagerWebhook, alertnames: List[str]) -> str:
    summary = payload.commonAnnotations.get("summary")
    if summary:
        return f"[Alert] {summary}"[:200]
    return f"[Alert] {', '.join(alertnames)}"[:200]


def _wants_investigation(alerts: List[AlertmanagerAlert]) -> bool:
    return any(a.labels.get("ai_investigate", "true").lower() != "false" for a in alerts)


def ingest(payload: AlertmanagerWebhook) -> AlertIngestResult:
    """Create/update/resolve the incident for this alert group (synchronous store work)."""
    existing = incident_store.find_open_by_metadata(GROUP_KEY, payload.groupKey)
    firing = [a for a in payload.alerts if a.status == "firing"]
    alertnames = sorted({a.labels.get("alertname", "alert") for a in payload.alerts})

    if payload.status == "resolved" or not firing:
        if not existing:
            return AlertIngestResult(action="ignored", alertnames=alertnames,
                                     detail="resolved notification for a group with no open incident")
        line = f"[alert] Resolved by Alertmanager at {datetime.now(timezone.utc).isoformat()}"

        def resolve(incident) -> None:
            incident.status = IncidentStatus.RESOLVED
            incident.notes = f"{incident.notes}\n{line}" if incident.notes else line

        incident_store.update(existing.incident_id, resolve)
        logger.info("Alert group resolved -> incident %s RESOLVED", existing.incident_id)
        return AlertIngestResult(incident_id=existing.incident_id, action="resolved", alertnames=alertnames)

    seen = set((existing.metadata.get("alert_fingerprints") or []) if existing else [])
    new_alerts = [a for a in firing if _alert_id(a) not in seen]
    investigate = bool(new_alerts) and _wants_investigation(new_alerts)
    severity = max((_severity(a) for a in firing), key=lambda s: _SEV_RANK[s])

    if existing:
        incident = existing
        if new_alerts:
            incident = build_context(BuildContextRequest(
                incident_id=existing.incident_id, logs=[_as_log(a) for a in new_alerts]
            ))
        action = "updated"
    else:
        starts = [a.startsAt for a in firing if a.startsAt]
        now = datetime.now(timezone.utc)
        incident = build_context(BuildContextRequest(
            title=_title(payload, alertnames),
            severity=severity,
            affected_services=sorted({_service(a) for a in firing} - {"unknown"}),
            time_window=TimeWindow(start=min(starts) if starts else now, end=now),
            logs=[_as_log(a) for a in firing],
            notes=f"Opened from Alertmanager receiver '{payload.receiver}'.",
        ))
        action = "created"

    def tag(fresh) -> None:
        meta = fresh.metadata
        fresh.metadata = {
            **meta,
            "source": "alertmanager",
            GROUP_KEY: payload.groupKey,
            "alertnames": sorted(set(meta.get("alertnames", [])) | set(alertnames)),
            "alert_fingerprints": sorted(
                set(meta.get("alert_fingerprints", [])) | {_alert_id(a) for a in firing}
            ),
            "alertmanager_url": payload.externalURL,
        }

    incident, _ = incident_store.update(incident.incident_id, tag)
    logger.info(
        "Alert group %s -> incident %s %s (%d new alert(s))",
        payload.groupKey, incident.incident_id, action, len(new_alerts),
    )
    return AlertIngestResult(
        incident_id=incident.incident_id,
        action=action,
        alertnames=alertnames,
        new_alerts=len(new_alerts),
        investigation_scheduled=investigate,
    )


async def investigate(incident_id: str, provider: AIProvider, settings: Settings) -> Dict[str, Any]:
    """Enrich -> correlate -> RCA -> runbook. Each step's failure is recorded, not raised."""
    steps: Dict[str, Any] = {}

    async def step(name: str, coro_factory) -> Optional[Any]:
        try:
            result = await coro_factory()
            steps[name] = "ok"
            return result
        except Exception as exc:  # background task: never propagate
            logger.warning("Auto-investigation %s failed for %s: %s", name, incident_id, exc)
            steps[name] = f"error: {exc}"
            return None

    if settings.ALERT_AUTO_ENRICH:
        await step("enrich", lambda: TelemetryEnrichmentService(settings).enrich(incident_id))
    correlation = await step("correlate", lambda: run_in_threadpool(correlate_incident, incident_id))
    if settings.ALERT_AUTO_RCA:
        rca = await step("rca", lambda: RCAEngine(provider).analyze(
            incident_id, run_correlation=False, correlation=correlation
        ))
        if rca is not None:
            await _notify_current("rca_completed", incident_id, rca_details(rca), settings)
        if settings.ALERT_AUTO_RUNBOOK:
            runbook = await step("runbook", lambda: RunbookEngine(provider).generate(incident_id))
            if runbook is not None:
                await _notify_current("runbook_completed", incident_id, runbook_details(runbook), settings)

    def record(incident) -> None:
        if incident.status == IncidentStatus.OPEN:
            incident.status = IncidentStatus.INVESTIGATING
        incident.metadata = {
            **incident.metadata,
            "auto_investigation": {"finished_at": datetime.now(timezone.utc).isoformat(), "steps": steps},
        }

    try:
        await run_in_threadpool(incident_store.update, incident_id, record)
    except KeyError:
        logger.warning("Incident %s was deleted during auto-investigation", incident_id)
    logger.info("Auto-investigation for %s finished: %s", incident_id, steps)
    return steps


async def _notify_current(
    event: str, incident_id: str, details: Dict[str, Any], settings: Settings
) -> None:
    incident = await run_in_threadpool(incident_store.get, incident_id)
    if incident:
        await notify(event, incident, {"source": "alertmanager", **details}, settings)


async def notify_ingest_result(result: AlertIngestResult, settings: Settings) -> None:
    """Background task: announce incidents opened or resolved by Alertmanager."""
    event = {"created": "incident_created", "resolved": "incident_resolved"}.get(result.action)
    if event and result.incident_id:
        await _notify_current(event, result.incident_id, {"alertnames": result.alertnames}, settings)


def ingest_and_plan(payload: AlertmanagerWebhook, settings: Settings) -> Tuple[AlertIngestResult, Optional[str]]:
    result = ingest(payload)
    if not settings.ALERT_AUTO_INVESTIGATE:
        result.investigation_scheduled = False
    return result, (result.incident_id if result.investigation_scheduled else None)
