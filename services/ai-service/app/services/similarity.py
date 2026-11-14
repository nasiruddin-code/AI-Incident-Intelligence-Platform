"""Incident signatures and similarity (similar-incident lookup).

A signature is a sparse, weighted bag of features that identify *what kind*
of incident this is, independent of when it happened or which request IDs
were involved:

    svc:<service>                       affected services
    alert:<alertname>                   Alertmanager alert names
    scenario:<name>                     reproducible scenario, if any
    anomaly:<type>:<service>            rule-based and baseline anomalies
    log:<service>:<template>            error-log templates (IDs, numbers, hex stripped)
    warn:<service>:<template>           warning templates (lower weight: many apps log
                                        handled failures, e.g. declined payments, as WARN)
    span:<service>/<name>               failing spans
    word:<w> / cause:<w>                title words / RCA primary-cause words

Signatures are L2-normalised, so similarity is a cosine in [0, 1]. It is
deterministic, needs no model or API, and every match can be explained by
the features it shares. Stored per incident (incidents.signature) and
refreshed on every save.
"""
from __future__ import annotations

import math
import re
from typing import Dict, List, Tuple

from app.schemas.incident import IncidentContext
from app.schemas.telemetry import SeverityLevel, SpanStatus

WEIGHTS = {
    "svc": 1.0,
    "alert": 2.0,
    "scenario": 1.0,
    "anomaly": 2.0,
    "log": 1.5,
    "warn": 1.0,
    "span": 1.5,
    "word": 0.5,
    "cause": 1.0,
}
MAX_LOG_TEMPLATES = 20

_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "into", "over", "under", "after",
    "incident", "alert", "affecting", "service", "services", "high", "rate", "mock",
}
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_HEX = re.compile(r"\b[0-9a-f]{12,}\b")
_NUM = re.compile(r"\d+(\.\d+)?")
_KV_VALUE = re.compile(r"(\w+)=\S+")
_WORD = re.compile(r"[a-z][a-z0-9_-]{3,}")


def log_template(body: str) -> str:
    """'Order PAYMENT_FAILED order_id=ab12... amount=5.00' -> 'order payment_failed order_id=* amount=*'."""
    text = body.lower()
    text = _UUID.sub("*", text)
    text = _HEX.sub("*", text)
    text = _KV_VALUE.sub(r"\1=*", text)
    text = _NUM.sub("#", text)
    return " ".join(text.split()[:10])


def _words(text: str) -> List[str]:
    return [w for w in _WORD.findall((text or "").lower()) if w not in _STOPWORDS]


def signature(incident: IncidentContext) -> Dict[str, float]:
    from app.services.baseline import findings_from
    from app.services.correlation import _detect_anomalies

    features: Dict[str, float] = {}

    def add(kind: str, value: str) -> None:
        key = f"{kind}:{value}"
        features[key] = max(features.get(key, 0.0), WEIGHTS[kind])

    for svc in incident.affected_services:
        if svc and svc != "unknown":
            add("svc", svc)
    for name in incident.metadata.get("alertnames") or []:
        add("alert", name)
    if incident.scenario:
        add("scenario", incident.scenario.value)

    anomalies = _detect_anomalies(incident.logs, incident.metrics, incident.traces)
    anomalies += findings_from(incident.metadata.get("baseline"))
    for a in anomalies:
        add("anomaly", f"{a.anomaly_type}:{a.service or '*'}")

    templates: Dict[str, List[str]] = {"log": [], "warn": []}
    for log in incident.logs:
        if log.severity in (SeverityLevel.ERROR, SeverityLevel.FATAL):
            kind = "log"
        elif log.severity == SeverityLevel.WARN:
            kind = "warn"
        else:
            continue
        t = f"{log.resource.service_name}:{log_template(log.body)}"
        if t not in templates[kind]:
            templates[kind].append(t)
    for kind, items in templates.items():
        for t in items[:MAX_LOG_TEMPLATES]:
            add(kind, t)
    for span in incident.traces:
        if span.status == SpanStatus.ERROR:
            add("span", f"{span.resource.service_name}/{span.name}")

    for w in _words(incident.title):
        add("word", w)
    for w in _words(incident.metadata.get("rca_primary_cause") or ""):
        add("cause", w)

    norm = math.sqrt(sum(v * v for v in features.values()))
    return {k: round(v / norm, 6) for k, v in features.items()} if norm else {}


def similarity(a: Dict[str, float], b: Dict[str, float]) -> Tuple[float, List[str]]:
    """Cosine similarity and the shared features, strongest contribution first."""
    if len(a) > len(b):
        a, b = b, a
    shared = [(k, v * b[k]) for k, v in a.items() if k in b]
    shared.sort(key=lambda kv: kv[1], reverse=True)
    return round(min(1.0, sum(c for _, c in shared)), 4), [k for k, _ in shared]
