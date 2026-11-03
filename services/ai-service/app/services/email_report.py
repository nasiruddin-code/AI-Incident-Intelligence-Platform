"""Incident emails for the production support team (SMTP).

One thread per incident: "New incident" -> "Incident report" (after the
runbook, with RCA + remediation) -> "Resolved". Events are chosen by
NOTIFY_EMAIL_EVENTS; recipients by NOTIFY_EMAIL_TO. Locally the SMTP server
is Mailpit (free test inbox, nothing leaves the machine).

Everything taken from logs or AI output is HTML-escaped and passed through
redact() (card numbers, JWTs, bearer tokens, key=secret pairs, email
addresses) before it is put in an email.
"""
from __future__ import annotations

import html
import re
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any, Dict, List, Optional

from app.core.config import Settings
from app.schemas.incident import IncidentContext
from app.schemas.telemetry import SeverityLevel

SUBJECTS = {
    "incident_created": "New incident",
    "rca_completed": "Root cause identified",
    "runbook_completed": "Incident report",
    "incident_resolved": "Resolved",
}
SEVERITY_COLORS = {"FATAL": "#b42318", "ERROR": "#c4320a", "WARN": "#b54708", "INFO": "#175cd3"}

_REDACTIONS = [
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[CARD]"),
    (re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+"), "[JWT]"),
    (re.compile(r"(?i)\bbearer\s+[\w.~+/-]+=*"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|token|authorization)(\s*[=:]\s*)[^\s,;&\"']+"),
     r"\1\2[REDACTED]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[EMAIL]"),
]


def redact(text: Any) -> str:
    out = "" if text is None else str(text)
    for pattern, replacement in _REDACTIONS:
        out = pattern.sub(replacement, out)
    return out


def _e(text: Any) -> str:
    return html.escape(redact(text))


def _thread_id(incident_id: str) -> str:
    return f"<incident-{incident_id}@incident-intelligence.local>"


def _pct(value: Any) -> str:
    try:
        return f"{round(float(value) * 100)}%"
    except (TypeError, ValueError):
        return "—"


def _ui_link(settings: Settings, incident_id: str) -> Optional[str]:
    if settings.UI_BASE_URL:
        return f"{settings.UI_BASE_URL.rstrip('/')}/#/incident/{incident_id}"
    if settings.PUBLIC_BASE_URL:
        return f"{settings.PUBLIC_BASE_URL.rstrip('/')}{settings.API_V1_PREFIX}/incidents/{incident_id}"
    return None


def _evidence_lines(incident: IncidentContext, limit: int = 5) -> List[str]:
    rank = {SeverityLevel.FATAL: 0, SeverityLevel.ERROR: 1, SeverityLevel.WARN: 2}
    logs = [log for log in incident.logs if log.severity in rank]
    logs.sort(key=lambda log: (rank[log.severity], log.timestamp))
    seen, out = set(), []
    for log in logs:
        key = log.body[:120]
        if key in seen:
            continue
        seen.add(key)
        out.append(f"{log.timestamp.strftime('%H:%M:%S')} {log.severity.value} {log.resource.service_name}: {log.body[:300]}")
        if len(out) >= limit:
            break
    return out


def _impact(incident: IncidentContext) -> str:
    m = incident.metadata or {}
    lm = m.get("log_monitor") or {}
    parts = []
    if lm.get("error_count"):
        parts.append(f"{lm['error_count']} failing log line(s)")
    parts.append(f"service(s): {', '.join(incident.affected_services) or 'unknown'}")
    window = incident.time_window
    minutes = max(0, int((window.end - window.start).total_seconds() // 60))
    parts.append(f"window {window.start.strftime('%Y-%m-%d %H:%M:%S')} → {window.end.strftime('%H:%M:%S')} UTC (~{minutes} min)")
    if m.get("severity_score") is not None:
        parts.append(f"severity score {m['severity_score']}/100")
    if m.get("anomaly_count"):
        parts.append(f"{m['anomaly_count']} anomal(ies) detected")
    return "; ".join(parts)


# --------------- content ------------------------------------------------------

def _sections(event: str, incident: IncidentContext, details: Dict[str, Any], settings: Settings) -> List[tuple]:
    """(heading, [paragraphs or ('list', items)]) — rendered as both HTML and text."""
    m = incident.metadata or {}
    lm = m.get("log_monitor") or {}
    sections: List[tuple] = []

    facts = [
        f"Severity: {incident.severity.value}",
        f"Status: {incident.status.value}",
        f"Services: {', '.join(incident.affected_services) or 'unknown'}",
        f"Incident ID: {incident.incident_id}",
    ]
    if lm.get("error_class"):
        facts.append(f"Error type: {lm['error_class']} ({', '.join(lm.get('detection') or [])})")
    if lm.get("recurring_count"):
        facts.append(f"Recurring: seen {lm['recurring_count'] + 1}x in the last {settings.LOG_MONITOR_RECURRING_DAYS} days")
    if m.get("alertnames"):
        facts.append(f"Alerts: {', '.join(m['alertnames'])}")
    sections.append(("Summary", [("list", facts), incident.evidence_summary or ""]))

    if event == "incident_resolved":
        lines = [f"Resolved at {m.get('resolved_at') or datetime.now().isoformat()}."]
        if details.get("error_duration_seconds") is not None:
            lines.append(f"Errors lasted about {round(details['error_duration_seconds'] / 60, 1)} minute(s).")
        if details.get("quiet_seconds"):
            lines.append(f"No matching errors for {details['quiet_seconds']} seconds before resolving.")
        if m.get("rca_primary_cause"):
            lines.append(f"Root cause identified during the incident: {m['rca_primary_cause']}")
        sections.append(("Resolution", lines))
        return sections

    sections.append(("Potential impact", [_impact(incident)]))

    if event == "incident_created":
        if lm.get("example") or details.get("example"):
            sections.append(("First error", [("code", lm.get("example") or details.get("example"))]))
        sections.append(("Next", ["AI root-cause analysis and remediation steps are being prepared; "
                                  "the full incident report will follow in this thread."]))
        return sections

    if m.get("rca_primary_cause"):
        rca = [f"{m['rca_primary_cause']} (confidence {_pct(m.get('rca_confidence'))})"]
        if m.get("rca_summary"):
            rca.append(m["rca_summary"])
        hyps = [f"{h.get('cause')} — {_pct(h.get('confidence'))}" for h in (m.get("rca_hypotheses") or [])[1:4]]
        if hyps:
            rca.append("Other possible causes:")
            rca.append(("list", hyps))
        if m.get("rca_contributing_factors"):
            rca.append("Contributing factors:")
            rca.append(("list", m["rca_contributing_factors"]))
        sections.append(("Probable root cause", rca))

    if event == "runbook_completed":
        steps = []
        for s in m.get("runbook_steps") or []:
            line = s.get("action", "")
            if s.get("command_or_check"):
                line += f" — run/check: {s['command_or_check']}"
            if s.get("expected_result"):
                line += f" (expect: {s['expected_result']})"
            steps.append(line)
        resolution: List[Any] = []
        if m.get("runbook_title"):
            resolution.append(f"{m['runbook_title']} (confidence {_pct(m.get('runbook_confidence'))}"
                              + (f", ~{m['runbook_estimated_minutes']} min" if m.get("runbook_estimated_minutes") else "") + ")")
        if steps:
            resolution.append(("olist", steps))
        remediation = [
            f"P{a.get('priority')}: {a.get('action')} — risk {a.get('risk_level')}; {a.get('rationale', '')}"
            for a in sorted(m.get("runbook_remediation") or [], key=lambda a: a.get("priority") or 99)
        ]
        if remediation:
            resolution.append("Remediation actions:")
            resolution.append(("list", remediation))
        if m.get("runbook_verification"):
            resolution.append("Verify the fix:")
            resolution.append(("list", m["runbook_verification"]))
        if m.get("runbook_escalation"):
            resolution.append(f"Escalation: {m['runbook_escalation']}")
        if resolution:
            sections.append(("Suggested resolution", resolution))
    elif m.get("rca_next_steps"):
        sections.append(("Recommended next steps", [("list", m["rca_next_steps"])]))

    evidence = _evidence_lines(incident)
    if evidence:
        sections.append(("Key evidence", [("code", "\n".join(evidence))]))

    provider = m.get("runbook_provider") or m.get("rca_provider")
    if m.get("rca_degraded") or m.get("runbook_degraded") or details.get("degraded"):
        sections.append(("Note", ["No AI model was available: the analysis above is placeholder text from the "
                                  "fallback provider. Configure AI_PROVIDER for real root-cause analysis."]))
    elif provider == "mock":
        sections.append(("Note", ["AI_PROVIDER=mock: the root cause and resolution above are keyword-based "
                                  "placeholder text, not an analysis of this incident. Configure a real model "
                                  "(e.g. AI_PROVIDER=ollama) for real root-cause analysis."]))
    else:
        if provider:
            sections.append(("About this report", [f"Generated by AI ({provider}). Verify before acting."]))
    return sections


def _render_html(title: str, color: str, sections: List[tuple], link: Optional[str]) -> str:
    def block(item: Any) -> str:
        if isinstance(item, tuple):
            kind, content = item
            if kind == "code":
                return f'<pre style="background:#f6f8fa;padding:10px;border-radius:6px;white-space:pre-wrap;font-size:12px">{_e(content)}</pre>'
            tag = "ol" if kind == "olist" else "ul"
            return f"<{tag}>" + "".join(f"<li>{_e(x)}</li>" for x in content) + f"</{tag}>"
        return f"<p>{_e(item)}</p>" if item else ""

    body = "".join(
        f'<h3 style="margin:18px 0 6px;font-size:15px">{html.escape(heading)}</h3>' + "".join(block(i) for i in items)
        for heading, items in sections
    )
    button = (f'<p><a href="{html.escape(link)}" style="background:#d9480f;color:#fff;padding:8px 14px;'
              f'border-radius:6px;text-decoration:none">Open incident</a></p>') if link else ""
    return (
        '<!doctype html><html><body style="font-family:Segoe UI,Arial,sans-serif;color:#1d2433;max-width:720px">'
        f'<div style="border-left:6px solid {color};padding:6px 14px"><h2 style="margin:0">{_e(title)}</h2></div>'
        f"{body}{button}"
        '<p style="color:#667085;font-size:12px">Sent by Incident Intelligence (POC).</p></body></html>'
    )


def _render_text(title: str, sections: List[tuple], link: Optional[str]) -> str:
    out = [redact(title), "=" * min(len(title), 70)]
    for heading, items in sections:
        out += ["", heading.upper()]
        for item in items:
            if isinstance(item, tuple):
                kind, content = item
                if kind == "code":
                    out.append(redact(content))
                else:
                    out += [f"{i + 1}. {redact(x)}" if kind == "olist" else f"- {redact(x)}" for i, x in enumerate(content)]
            elif item:
                out.append(redact(item))
    if link:
        out += ["", f"Open incident: {link}"]
    return "\n".join(out) + "\n"


def build_email(event: str, incident: IncidentContext, details: Dict[str, Any], settings: Settings) -> EmailMessage:
    subject_kind = SUBJECTS.get(event, event)
    title = f"{subject_kind}: {incident.title}"
    sections = _sections(event, incident, details or {}, settings)
    link = _ui_link(settings, incident.incident_id)
    color = "#027a48" if event == "incident_resolved" else SEVERITY_COLORS.get(incident.severity.value, "#475467")

    msg = EmailMessage()
    msg["Subject"] = f"[{incident.severity.value}] {redact(title)}"[:200]
    msg["From"] = settings.NOTIFY_EMAIL_FROM
    msg["To"] = ", ".join(settings.NOTIFY_EMAIL_TO)
    msg["Date"] = formatdate(localtime=False)
    thread = _thread_id(incident.incident_id)
    if event == "incident_created":
        msg["Message-ID"] = thread
    else:
        msg["Message-ID"] = make_msgid(domain="incident-intelligence.local")
        msg["In-Reply-To"] = thread
        msg["References"] = thread
    msg["X-Incident-ID"] = incident.incident_id
    msg["X-Incident-Event"] = event
    msg.set_content(_render_text(title, sections, link))
    msg.add_alternative(_render_html(title, color, sections, link), subtype="html")
    return msg


def send_email(msg: EmailMessage, settings: Settings) -> None:
    """Blocking SMTP send — call from a thread (notifier does)."""
    timeout = settings.NOTIFY_TIMEOUT_SECONDS
    if settings.SMTP_SSL:
        server: smtplib.SMTP = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=timeout,
                                                 context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=timeout)
    with server:
        if settings.SMTP_STARTTLS and not settings.SMTP_SSL:
            server.starttls(context=ssl.create_default_context())
        if settings.SMTP_USERNAME:
            server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD or "")
        server.send_message(msg)
