"""Query real observability backends and map results into our models (Week 13)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.config import Settings
from app.core.logging import get_logger
from app.integrations.loki_client import LokiClient
from app.integrations.prometheus_client import PrometheusClient
from app.integrations.tempo_client import TempoClient
from app.schemas.telemetry import (
    LogRecord,
    MetricPoint,
    MetricType,
    Resource,
    SeverityLevel,
    Span,
    SpanStatus,
)

logger = get_logger(__name__)


def _ns_to_dt(ns: str | int) -> datetime:
    try:
        return datetime.fromtimestamp(int(ns) / 1e9, tz=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def _guess_severity(line: str) -> SeverityLevel:
    lower = line.lower()
    if "fatal" in lower or "panic" in lower:
        return SeverityLevel.FATAL
    if "error" in lower or "exception" in lower or "failed" in lower:
        return SeverityLevel.ERROR
    if "warn" in lower:
        return SeverityLevel.WARN
    if "debug" in lower:
        return SeverityLevel.DEBUG
    return SeverityLevel.INFO


class TelemetryQueryService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.prom = PrometheusClient(settings.PROMETHEUS_URL, settings.OBSERVABILITY_TIMEOUT_SECONDS)
        self.loki = LokiClient(settings.LOKI_URL, settings.OBSERVABILITY_TIMEOUT_SECONDS)
        self.tempo = TempoClient(settings.TEMPO_URL, settings.OBSERVABILITY_TIMEOUT_SECONDS)

    async def health(self) -> Dict[str, bool]:
        return {
            "prometheus": await self.prom.health(),
            "loki": await self.loki.health(),
            "tempo": await self.tempo.health(),
        }

    async def query_metrics(
        self,
        promql: str = 'orders_total OR payments_total OR {__name__=~".*"}',
    ) -> Dict[str, Any]:
        try:
            raw = await self.prom.query(promql)
            rows = self.prom.parse_vector(raw)
            metrics: List[MetricPoint] = []
            for row in rows[:50]:
                metric = row["metric"]
                name = metric.get("__name__", "unknown")
                svc = metric.get("service_name") or metric.get("job") or metric.get("service") or "unknown"
                metrics.append(
                    MetricPoint(
                        name=name,
                        type=MetricType.GAUGE,
                        value=row["value"],
                        timestamp=datetime.fromtimestamp(float(row["timestamp"]), tz=timezone.utc),
                        attributes={k: str(v) for k, v in metric.items() if k != "__name__"},
                        resource=Resource(service_name=str(svc)),
                    )
                )
            return {
                "ok": True,
                "count": len(metrics),
                "metrics": [m.model_dump(mode="json") for m in metrics],
                "raw_status": raw.get("status"),
            }
        except Exception as exc:
            logger.warning("Prometheus query failed: %s", exc)
            return {"ok": False, "error": str(exc), "count": 0, "metrics": []}

    async def query_logs(
        self,
        logql: str = '{service_name=~".+"} |= ``',
        lookback_seconds: int = 3600,
        limit: int = 50,
    ) -> Dict[str, Any]:
        try:
            end = datetime.now(timezone.utc)
            start = datetime.fromtimestamp(end.timestamp() - lookback_seconds, tz=timezone.utc)
            start_ns = str(int(start.timestamp() * 1e9))
            end_ns = str(int(end.timestamp() * 1e9))
            # Simpler default query that often works with OTel→Loki
            if logql == '{service_name=~".+"} |= ``':
                logql = '{job=~".+"}'
            raw = await self.loki.query_range(logql, start_ns, end_ns, limit=limit)
            lines = self.loki.parse_streams(raw)
            logs: List[LogRecord] = []
            for item in lines[:limit]:
                labels = item.get("labels") or {}
                svc = labels.get("service_name") or labels.get("job") or labels.get("service") or "unknown"
                body = item.get("line") or ""
                logs.append(
                    LogRecord(
                        timestamp=_ns_to_dt(item.get("timestamp_ns", 0)),
                        severity=_guess_severity(body),
                        body=body[:1000],
                        attributes={k: str(v) for k, v in labels.items()},
                        resource=Resource(service_name=str(svc)),
                    )
                )
            return {
                "ok": True,
                "count": len(logs),
                "logs": [l.model_dump(mode="json") for l in logs],
            }
        except Exception as exc:
            logger.warning("Loki query failed: %s", exc)
            return {"ok": False, "error": str(exc), "count": 0, "logs": []}

    async def search_traces(
        self,
        service: Optional[str] = None,
        limit: int = 20,
        lookback_seconds: int = 3600,
    ) -> Dict[str, Any]:
        try:
            raw = await self.tempo.search(
                service=service,
                limit=limit,
                lookback_seconds=lookback_seconds,
            )
            traces = raw.get("traces") or []
            # If empty with service filter, retry unfiltered in the same window
            if not traces and service:
                logger.info("No traces for service=%s; retrying without service filter", service)
                raw2 = await self.tempo.search(limit=limit, lookback_seconds=lookback_seconds)
                traces = raw2.get("traces") or []
                return {
                    "ok": True,
                    "count": len(traces),
                    "traces": traces[:limit],
                    "note": f"No traces matched service={service}; showing last {lookback_seconds}s unfiltered",
                }
            return {
                "ok": True,
                "count": len(traces),
                "traces": traces[:limit],
            }
        except Exception as exc:
            logger.warning("Tempo search failed: %s", exc)
            return {"ok": False, "error": str(exc), "count": 0, "traces": []}

    async def get_trace(self, trace_id: str) -> Dict[str, Any]:
        try:
            raw = await self.tempo.get_trace(trace_id)
            # Map a simplified span list if possible
            spans_out: List[Dict[str, Any]] = []
            batches = raw.get("batches") or []
            for batch in batches:
                rs = (batch.get("resource") or {}).get("attributes") or []
                svc = "unknown"
                for attr in rs:
                    if attr.get("key") in ("service.name", "service_name"):
                        val = attr.get("value") or {}
                        svc = val.get("stringValue") or svc
                for scope_span in batch.get("scopeSpans") or batch.get("instrumentationLibrarySpans") or []:
                    for sp in scope_span.get("spans") or []:
                        status = (sp.get("status") or {}).get("code", 0)
                        span_status = SpanStatus.ERROR if status == 2 else SpanStatus.OK
                        spans_out.append(
                            {
                                "trace_id": trace_id,
                                "span_id": sp.get("spanId"),
                                "name": sp.get("name"),
                                "status": span_status.value,
                                "service": svc,
                            }
                        )
            return {
                "ok": True,
                "trace_id": trace_id,
                "span_count": len(spans_out),
                "spans": spans_out[:50],
                "raw_keys": list(raw.keys()),
            }
        except Exception as exc:
            logger.warning("Tempo get_trace failed: %s", exc)
            return {"ok": False, "error": str(exc), "trace_id": trace_id, "spans": []}
