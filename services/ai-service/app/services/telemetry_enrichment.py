"""Pull live evidence from Prometheus / Loki / Tempo into an incident.

For each affected service, within the incident's time window:
- Loki:       logs labelled job=<service> (OTel collector loki exporter, JSON lines)
- Prometheus: series labelled service_name=<service> at the window end
              (collector namespace prefix stripped; *_sum/*_count -> average)
- Tempo:      traces for the service, expanded to spans

Results are mapped onto the platform's telemetry models, de-duplicated
against evidence already on the incident (so enrichment is safe to re-run),
and merged via the context builder. Each backend failing is reported per
source rather than failing the whole enrichment.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from starlette.concurrency import run_in_threadpool

from app.core.config import Settings
from app.core.logging import get_logger
from app.integrations.loki_client import LokiClient
from app.integrations.prometheus_client import PrometheusClient
from app.integrations.tempo_client import TempoClient
from app.schemas.incident import (
    BuildContextRequest,
    EnrichmentResult,
    EnrichmentSourceStatus,
    IncidentContext,
    TimeWindow,
)
from app.schemas.telemetry import (
    LogRecord,
    MetricPoint,
    MetricType,
    Resource,
    SeverityLevel,
    Span,
    SpanStatus,
)
from app.services import incident_store
from app.services.context_builder import build_context
from app.services.telemetry_query import _guess_severity

logger = get_logger(__name__)

_IGNORED_METRICS = ("target_info", "up", "scrape_")

_SEVERITY_ALIASES = {
    "TRACE": SeverityLevel.TRACE,
    "DEBUG": SeverityLevel.DEBUG,
    "INFO": SeverityLevel.INFO,
    "INFORMATION": SeverityLevel.INFO,
    "WARN": SeverityLevel.WARN,
    "WARNING": SeverityLevel.WARN,
    "ERROR": SeverityLevel.ERROR,
    "FATAL": SeverityLevel.FATAL,
    "CRITICAL": SeverityLevel.FATAL,
}


# --------------- Mapping helpers ------------------------------------------

def _ns_to_dt(ns: Any) -> datetime:
    return datetime.fromtimestamp(int(ns) / 1e9, tz=timezone.utc)


def _otlp_id_to_hex(value: Optional[str]) -> Optional[str]:
    """Tempo's OTLP JSON uses base64 IDs; logs carry hex. Normalise to hex."""
    if not value:
        return None
    if all(c in "0123456789abcdefABCDEF" for c in value) and len(value) in (16, 32):
        return value.lower()
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return value
    return raw.hex() if len(raw) in (8, 16) else value


def _otlp_attr_value(value: Dict[str, Any]) -> Any:
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in value:
            return value[key]
    return json.dumps(value)


def _otlp_attrs(attrs: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    return {a.get("key"): _otlp_attr_value(a.get("value") or {}) for a in attrs or [] if a.get("key")}


def map_loki_line(line: str, labels: Dict[str, str], ts_ns: Any, service: str) -> LogRecord:
    """Parse a Loki line written by the collector's loki exporter (JSON), or plain text."""
    body, severity_text, trace_id, span_id = line, None, None, None
    attributes: Dict[str, Any] = dict(labels)
    if line.startswith("{"):
        try:
            data = json.loads(line)
        except ValueError:
            data = None
        if isinstance(data, dict):
            body = str(data.get("body", line))
            severity_text = data.get("severity") or data.get("level")
            trace_id = data.get("traceid") or data.get("trace_id")
            span_id = data.get("spanid") or data.get("span_id")
            attributes.update({k: v for k, v in (data.get("attributes") or {}).items()})
    severity = _SEVERITY_ALIASES.get(str(severity_text).upper()) if severity_text else None
    return LogRecord(
        timestamp=_ns_to_dt(ts_ns),
        severity=severity or _guess_severity(body),
        body=body[:2000],
        attributes=attributes,
        resource=Resource(service_name=service),
        trace_id=trace_id or None,
        span_id=span_id or None,
    )


def map_prometheus_rows(rows: List[Dict[str, Any]], service: str, prefix: str) -> List[MetricPoint]:
    """Turn instant-vector rows into MetricPoints; fold *_sum/*_count into an average."""
    points: List[MetricPoint] = []
    sums: Dict[Tuple[str, Tuple], Tuple[float, float]] = {}
    counts: Dict[Tuple[str, Tuple], float] = {}

    for row in rows:
        labels = dict(row["metric"])
        name = labels.pop("__name__", "unknown")
        if prefix and name.startswith(prefix):
            name = name[len(prefix):]
        if name.startswith(_IGNORED_METRICS) or name.endswith("_bucket"):
            continue
        attrs = {k: str(v) for k, v in labels.items()}
        key_attrs = tuple(sorted(attrs.items()))
        if name.endswith("_sum"):
            sums[(name[:-4], key_attrs)] = (row["value"], row["timestamp"])
            continue
        if name.endswith("_count"):
            counts[(name[:-6], key_attrs)] = row["value"]
            continue
        points.append(
            MetricPoint(
                name=name,
                type=MetricType.COUNTER if name.endswith("_total") else MetricType.GAUGE,
                value=row["value"],
                timestamp=datetime.fromtimestamp(float(row["timestamp"]), tz=timezone.utc),
                attributes=attrs,
                resource=Resource(service_name=service),
            )
        )

    for (base, key_attrs), (total, ts) in sums.items():
        count = counts.get((base, key_attrs))
        if not count:
            continue
        points.append(
            MetricPoint(
                name=base,
                description="average (sum/count) from Prometheus",
                type=MetricType.HISTOGRAM,
                value=total / count,
                timestamp=datetime.fromtimestamp(float(ts), tz=timezone.utc),
                attributes=dict(key_attrs),
                resource=Resource(service_name=service),
            )
        )
    return points


def map_tempo_trace(raw: Dict[str, Any]) -> List[Span]:
    """Map Tempo's OTLP-JSON trace (batches / resourceSpans) to Spans."""
    spans: List[Span] = []
    for batch in raw.get("batches") or raw.get("resourceSpans") or []:
        res_attrs = _otlp_attrs((batch.get("resource") or {}).get("attributes"))
        service = str(res_attrs.get("service.name") or "unknown")
        for scope in batch.get("scopeSpans") or batch.get("instrumentationLibrarySpans") or []:
            for sp in scope.get("spans") or []:
                code = (sp.get("status") or {}).get("code", 0)
                if code in (2, "STATUS_CODE_ERROR"):
                    status = SpanStatus.ERROR
                elif code in (1, "STATUS_CODE_OK"):
                    status = SpanStatus.OK
                else:
                    status = SpanStatus.UNSET
                spans.append(
                    Span(
                        trace_id=_otlp_id_to_hex(sp.get("traceId")) or "unknown",
                        span_id=_otlp_id_to_hex(sp.get("spanId")) or "unknown",
                        parent_span_id=_otlp_id_to_hex(sp.get("parentSpanId")),
                        name=sp.get("name") or "unknown",
                        start_time=_ns_to_dt(sp.get("startTimeUnixNano", 0)),
                        end_time=_ns_to_dt(sp.get("endTimeUnixNano", 0)),
                        status=status,
                        attributes=_otlp_attrs(sp.get("attributes")),
                        resource=Resource(service_name=service),
                    )
                )
    return spans


# --------------- De-duplication -------------------------------------------

def _log_key(log: LogRecord) -> tuple:
    return (log.timestamp.isoformat(), log.resource.service_name, log.body)


def _metric_key(m: MetricPoint) -> tuple:
    return (m.name, m.resource.service_name, tuple(sorted(m.attributes.items())), m.timestamp.isoformat())


def _span_key(s: Span) -> tuple:
    return (s.trace_id, s.span_id)


def _new_only(items: List[Any], existing: List[Any], key) -> List[Any]:
    seen = {key(i) for i in existing}
    out = []
    for item in items:
        k = key(item)
        if k not in seen:
            seen.add(k)
            out.append(item)
    return out


# --------------- Service --------------------------------------------------

class TelemetryEnrichmentService:
    def __init__(self, settings: Settings):
        self.settings = settings
        timeout = settings.OBSERVABILITY_TIMEOUT_SECONDS
        self.prom = PrometheusClient(settings.PROMETHEUS_URL, timeout)
        self.loki = LokiClient(settings.LOKI_URL, timeout)
        self.tempo = TempoClient(settings.TEMPO_URL, timeout)

    def _window(self, incident: IncidentContext, lookback_seconds: Optional[int]) -> TimeWindow:
        now = datetime.now(timezone.utc)
        pad = timedelta(seconds=60)
        end = min(incident.time_window.end + pad, now)
        start = incident.time_window.start - pad
        lookback = lookback_seconds or self.settings.ENRICH_LOOKBACK_SECONDS
        if lookback_seconds or (end - start).total_seconds() < lookback:
            start = min(start, end - timedelta(seconds=lookback))
        return TimeWindow(start=start, end=end)

    async def _logs(self, service: str, window: TimeWindow) -> List[LogRecord]:
        raw = await self.loki.query_range(
            f'{{job="{service}"}}',
            str(int(window.start.timestamp() * 1e9)),
            str(int(window.end.timestamp() * 1e9)),
            limit=self.settings.ENRICH_MAX_LOGS_PER_SERVICE,
        )
        return [
            map_loki_line(item["line"], item.get("labels") or {}, item["timestamp_ns"], service)
            for item in self.loki.parse_streams(raw)
        ]

    async def _metrics(self, service: str, window: TimeWindow) -> List[MetricPoint]:
        raw = await self.prom.query(
            f'{{service_name="{service}"}}', time=str(window.end.timestamp())
        )
        rows = self.prom.parse_vector(raw)
        points = map_prometheus_rows(rows, service, self.settings.ENRICH_METRIC_PREFIX)
        return points[: self.settings.ENRICH_MAX_METRICS_PER_SERVICE]

    async def _traces(self, service: str, window: TimeWindow) -> List[Span]:
        found = await self.tempo.search(
            service=service,
            limit=self.settings.ENRICH_MAX_TRACES_PER_SERVICE,
            start=int(window.start.timestamp()),
            end=int(window.end.timestamp()),
        )
        trace_ids = [t.get("traceID") or t.get("traceId") for t in found.get("traces") or []]
        trace_ids = [t for t in trace_ids if t][: self.settings.ENRICH_MAX_TRACES_PER_SERVICE]
        raws = await asyncio.gather(*(self.tempo.get_trace(t) for t in trace_ids))
        return [span for raw in raws for span in map_tempo_trace(raw)]

    async def enrich(
        self,
        incident_id: str,
        *,
        lookback_seconds: Optional[int] = None,
        services: Optional[List[str]] = None,
    ) -> EnrichmentResult:
        incident = await run_in_threadpool(incident_store.get, incident_id)
        if not incident:
            raise KeyError(f"Incident {incident_id} not found")

        targets = [s for s in (services or incident.affected_services) if s and s != "unknown"]
        window = self._window(incident, lookback_seconds)

        fetchers = {"loki": self._logs, "prometheus": self._metrics, "tempo": self._traces}
        jobs = [(source, svc, fn(svc, window)) for source, fn in fetchers.items() for svc in targets]
        outcomes = await asyncio.gather(*(job for _, _, job in jobs), return_exceptions=True)

        collected: Dict[str, List[Any]] = defaultdict(list)
        errors: Dict[str, List[str]] = defaultdict(list)
        for (source, svc, _), outcome in zip(jobs, outcomes):
            if isinstance(outcome, Exception):
                logger.warning("Enrichment %s query for %s failed: %s", source, svc, outcome)
                errors[source].append(f"{svc}: {outcome}")
            else:
                collected[source].extend(outcome)

        logs = _new_only(collected["loki"], incident.logs, _log_key)
        metrics = _new_only(collected["prometheus"], incident.metrics, _metric_key)
        spans = _new_only(collected["tempo"], incident.traces, _span_key)

        if logs or metrics or spans:
            incident = await run_in_threadpool(
                build_context,
                BuildContextRequest(
                    incident_id=incident_id, logs=logs, metrics=metrics, traces=spans
                ),
            )

        added = {"loki": len(logs), "prometheus": len(metrics), "tempo": len(spans)}
        sources = {
            source: EnrichmentSourceStatus(
                ok=not errors[source] or bool(collected[source]),
                fetched=len(collected[source]),
                added=added[source],
                error="; ".join(errors[source]) or None,
            )
            for source in fetchers
        }
        logger.info("Enriched incident %s from live telemetry: %s", incident_id, added)
        return EnrichmentResult(
            incident_id=incident_id,
            window=window,
            services=targets,
            sources=sources,
            logs_added=len(logs),
            metrics_added=len(metrics),
            spans_added=len(spans),
            evidence_summary=incident.evidence_summary,
        )
