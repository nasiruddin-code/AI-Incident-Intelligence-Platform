"""Incident notifications to Slack and generic webhooks.

Events: incident_created, rca_completed, runbook_completed, incident_resolved.
Targets (any combination):
- NOTIFY_SLACK_WEBHOOK_URL  Slack incoming webhook (Block Kit message)
- NOTIFY_WEBHOOK_URLS       generic JSON POST; if NOTIFY_WEBHOOK_SECRET is set,
                            the body is signed: X-Signature-SHA256: sha256=<hex hmac>

Filtered by NOTIFY_EVENTS and NOTIFY_MIN_SEVERITY. Delivery failures are
logged and counted, never raised: a notification must not fail the request
or investigation that triggered it. Callers run notify() in the background.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.integrations.http import get_http_client
from app.schemas.incident import IncidentContext
from app.schemas.telemetry import SeverityLevel
from app.services.correlation import _SEV_RANK

logger = get_logger(__name__)

EVENT_TITLES = {
    "incident_created": "New incident",
    "rca_completed": "Root cause analysis ready",
    "runbook_completed": "Runbook ready",
    "incident_resolved": "Incident resolved",
}
SEVERITY_EMOJI = {"FATAL": "🔴", "ERROR": "🟠", "WARN": "🟡", "INFO": "🔵", "DEBUG": "⚪", "TRACE": "⚪"}

# Delivery counters exposed on /metrics: (target, outcome) -> count
delivery_counts: Dict[tuple, int] = {}


def _count(target: str, outcome: str) -> None:
    delivery_counts[(target, outcome)] = delivery_counts.get((target, outcome), 0) + 1


def _incident_url(settings: Settings, incident_id: str) -> Optional[str]:
    if not settings.PUBLIC_BASE_URL:
        return None
    return f"{settings.PUBLIC_BASE_URL.rstrip('/')}{settings.API_V1_PREFIX}/incidents/{incident_id}"


def should_notify(settings: Settings, event: str, incident: IncidentContext) -> bool:
    if not (settings.NOTIFY_SLACK_WEBHOOK_URL or settings.NOTIFY_WEBHOOK_URLS):
        return False
    if event not in settings.NOTIFY_EVENTS:
        return False
    return _SEV_RANK[incident.severity] >= _SEV_RANK[SeverityLevel(settings.NOTIFY_MIN_SEVERITY)]


def build_payload(event: str, incident: IncidentContext, details: Dict[str, Any], settings: Settings) -> Dict[str, Any]:
    return {
        "event": event,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "incident": {
            "incident_id": incident.incident_id,
            "title": incident.title,
            "status": incident.status.value,
            "severity": incident.severity.value,
            "affected_services": incident.affected_services,
            "url": _incident_url(settings, incident.incident_id),
        },
        "details": details,
    }


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_slack_message(payload: Dict[str, Any]) -> Dict[str, Any]:
    inc = payload["incident"]
    details = payload["details"]
    emoji = "✅" if payload["event"] == "incident_resolved" else SEVERITY_EMOJI.get(inc["severity"], "⚪")
    heading = f"{emoji} {EVENT_TITLES.get(payload['event'], payload['event'])}: {inc['title']}"
    fields = [
        f"*Severity:* {inc['severity']}",
        f"*Status:* {inc['status']}",
        f"*Services:* {', '.join(inc['affected_services']) or 'unknown'}",
        f"*Incident:* `{inc['incident_id']}`",
    ]
    blocks: List[Dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{_slack_escape(heading)}*"}},
        {"type": "section", "fields": [{"type": "mrkdwn", "text": _slack_escape(f)} for f in fields]},
    ]
    lines = []
    if details.get("primary_cause"):
        lines.append(f"*Likely cause:* {details['primary_cause']} (confidence {details.get('confidence', '?')})")
    if details.get("runbook_title"):
        lines.append(f"*Runbook:* {details['runbook_title']} — {details.get('steps', 0)} steps")
    if details.get("degraded"):
        lines.append("⚠️ Produced by the fallback provider (no AI model available) — placeholder text.")
    if details.get("source"):
        lines.append(f"*Source:* {details['source']}")
    if lines:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _slack_escape("\n".join(lines))}})
    if inc.get("url"):
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"<{inc['url']}|Open incident>"}]})
    return {"text": heading, "blocks": blocks}


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def _post(target: str, url: str, body: bytes, headers: Dict[str, str], timeout: float) -> None:
    try:
        resp = await get_http_client().post(url, content=body, headers=headers, timeout=timeout)
        if resp.status_code >= 300:
            logger.warning("Notification to %s failed: HTTP %s", target, resp.status_code)
            _count(target, "error")
        else:
            _count(target, "success")
    except Exception as exc:
        logger.warning("Notification to %s failed: %s", target, exc)
        _count(target, "error")


async def notify(
    event: str,
    incident: IncidentContext,
    details: Optional[Dict[str, Any]] = None,
    settings: Optional[Settings] = None,
) -> None:
    settings = settings or get_settings()
    if not should_notify(settings, event, incident):
        return
    payload = build_payload(event, incident, details or {}, settings)
    timeout = settings.NOTIFY_TIMEOUT_SECONDS
    headers = {"Content-Type": "application/json"}

    if settings.NOTIFY_SLACK_WEBHOOK_URL:
        body = json.dumps(build_slack_message(payload)).encode()
        await _post("slack", settings.NOTIFY_SLACK_WEBHOOK_URL, body, headers, timeout)

    if settings.NOTIFY_WEBHOOK_URLS:
        body = json.dumps(payload).encode()
        hook_headers = dict(headers, **{"X-Event": event})
        if settings.NOTIFY_WEBHOOK_SECRET:
            hook_headers["X-Signature-SHA256"] = sign(body, settings.NOTIFY_WEBHOOK_SECRET)
        for url in settings.NOTIFY_WEBHOOK_URLS:
            await _post("webhook", url, body, hook_headers, timeout)


def rca_details(rca_response) -> Dict[str, Any]:
    return {
        "primary_cause": rca_response.rca.primary_cause,
        "confidence": rca_response.rca.confidence,
        "summary": rca_response.rca.summary,
        "provider": rca_response.provider,
        "degraded": rca_response.degraded,
    }


def runbook_details(runbook_response) -> Dict[str, Any]:
    return {
        "runbook_title": runbook_response.runbook.title,
        "steps": len(runbook_response.runbook.steps),
        "confidence": runbook_response.runbook.confidence,
        "provider": runbook_response.provider,
        "degraded": runbook_response.degraded,
    }


def render_metrics() -> List[str]:
    lines = [
        "# HELP ai_notifications_total Incident notifications sent, by target and outcome",
        "# TYPE ai_notifications_total counter",
    ]
    for (target, outcome), n in sorted(delivery_counts.items()):
        lines.append(f'ai_notifications_total{{target="{target}",outcome="{outcome}"}} {n}')
    return lines
