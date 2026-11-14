"""Baseline anomaly detection: compare each service with its own recent past.

For every affected service, a few signals are queried from Prometheus as
time series over two windows:

    baseline  [incident start - BASELINE_LOOKBACK, incident start - gap)
    incident  [incident start, incident end]   (at least BASELINE_MIN_WINDOW long)

The incident window's median is compared with the baseline using a robust
z-score, (current - median) / (1.4826 * MAD), which ignores the occasional
spike in the baseline. A signal is anomalous when it moves in the signal's
"bad" direction by at least BASELINE_Z_THRESHOLD and by a minimum absolute
amount (so a 0.001 -> 0.002 change on a quiet service does not page anyone).

Signals (per service, collector `otel_` metric names):
    request_rate      requests/s (drop or spike)
    http_error_ratio  share of 5xx responses
    latency_avg       mean server latency, seconds
    failure_ratio     share of business counters (otel_*_total) whose
                      status contains "fail" — catches failures that still
                      return HTTP 2xx, like declined payments

Results are stored on the incident (metadata.baseline) and turned into
AnomalyFindings by correlation, feeding severity scoring and RCA.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple

from starlette.concurrency import run_in_threadpool

from app.core.config import Settings
from app.core.logging import get_logger
from app.integrations.prometheus_client import PrometheusClient
from app.schemas.incident import AnomalyFinding, BaselineResult, BaselineSignalResult, TimeWindow
from app.services import incident_store

logger = get_logger(__name__)

MAD_SCALE = 1.4826  # makes MAD comparable to a standard deviation for normal data


@dataclass(frozen=True)
class Signal:
    name: str
    query: Callable[[str], str]  # service name -> PromQL
    direction: Literal["up", "down", "both"]
    min_delta: float  # smallest absolute change worth reporting
    unit: str


def _http(part: str, service: str, extra: str = "") -> str:
    return (
        f'rate(otel_http_server_request_duration_seconds_{part}'
        f'{{service_name="{service}",http_route!="/metrics"{extra}}}[1m])'
    )


def _business(service: str, status_matcher: str) -> str:
    return f'rate({{__name__=~"otel_.+_total",service_name="{service}",status{status_matcher}}}[1m])'


def _ratio(numerator: str, denominator: str) -> str:
    # "or vector(0)": no matching failure series means a ratio of 0, not "no data".
    # No clamp on the denominator: with no traffic, 0/0 = NaN and the point is
    # dropped, so idle minutes don't pull the baseline towards zero.
    return f"(sum({numerator}) or vector(0)) / sum({denominator})"


SIGNALS: Tuple[Signal, ...] = (
    Signal("request_rate", lambda s: f"sum({_http('count', s)})", "both", 0.05, "req/s"),
    Signal(
        "http_error_ratio",
        lambda s: _ratio(_http("count", s, ',http_response_status_code=~"5.."'), _http("count", s)),
        "up", 0.02, "ratio",
    ),
    Signal("latency_avg", lambda s: _ratio(_http("sum", s), _http("count", s)), "up", 0.05, "s"),
    Signal(
        "failure_ratio",
        lambda s: _ratio(_business(s, '=~".*fail.*"'), _business(s, '!=""')),
        "up", 0.05, "ratio",
    ),
)


# --------------- Statistics (pure) ------------------------------------------

def robust_stats(values: List[float]) -> Tuple[float, float]:
    """(median, MAD) of the values."""
    median = statistics.median(values)
    mad = statistics.median(abs(v - median) for v in values)
    return median, mad


def evaluate(
    signal: Signal,
    service: str,
    baseline: List[float],
    current: List[float],
    *,
    z_threshold: float,
    min_points: int,
) -> BaselineSignalResult:
    if len(baseline) < min_points or not current:
        return BaselineSignalResult(
            service=service, signal=signal.name, unit=signal.unit, anomalous=False,
            baseline_points=len(baseline), current_points=len(current),
            reason=f"insufficient data (baseline {len(baseline)} pts, need {min_points}; incident {len(current)} pts)",
        )

    base_median, mad = robust_stats(baseline)
    current_median = statistics.median(current)
    delta = current_median - base_median
    # Floor the spread so a perfectly flat baseline doesn't turn noise into infinity.
    spread = max(MAD_SCALE * mad, signal.min_delta / z_threshold, 1e-9)
    z = delta / spread

    moved_badly = (
        (signal.direction == "up" and z >= z_threshold)
        or (signal.direction == "down" and z <= -z_threshold)
        or (signal.direction == "both" and abs(z) >= z_threshold)
    )
    anomalous = moved_badly and abs(delta) >= signal.min_delta
    direction = "above" if delta >= 0 else "below"
    if anomalous and signal.direction == "both" and delta > 0 and base_median < signal.min_delta:
        # A rise from an idle baseline isn't a spike: there was no normal level to exceed.
        return BaselineSignalResult(
            service=service, signal=signal.name, unit=signal.unit, anomalous=False,
            baseline_median=round(base_median, 6), baseline_mad=round(mad, 6),
            current_median=round(current_median, 6), z_score=round(z, 2),
            baseline_points=len(baseline), current_points=len(current),
            reason=f"{signal.name} rose to {current_median:.4g} {signal.unit} from an idle baseline "
                   f"({base_median:.4g}); not treated as a spike",
        )
    return BaselineSignalResult(
        service=service, signal=signal.name, unit=signal.unit, anomalous=anomalous,
        baseline_median=round(base_median, 6), baseline_mad=round(mad, 6),
        current_median=round(current_median, 6), z_score=round(z, 2),
        baseline_points=len(baseline), current_points=len(current),
        reason=(
            f"{signal.name} {current_median:.4g} {signal.unit} vs baseline {base_median:.4g} "
            f"({direction} by {abs(z):.1f} robust SD)"
        ),
    )


def findings_from(baseline: Optional[Dict[str, Any]]) -> List[AnomalyFinding]:
    """AnomalyFindings for correlation, from metadata.baseline."""
    if not baseline:
        return []
    findings = []
    for s in baseline.get("signals") or []:
        if not s.get("anomalous"):
            continue
        z = abs(s.get("z_score") or 0.0)
        findings.append(AnomalyFinding(
            anomaly_type=f"baseline_{s['signal']}",
            service=s["service"],
            description=s["reason"],
            score=round(min(1.0, z / 6.0), 3),
            related_metric=s["signal"],
            evidence_count=s.get("current_points") or 0,
        ))
    return findings


# --------------- Prometheus + persistence -----------------------------------

def _values(raw: Dict[str, Any]) -> List[float]:
    out: List[float] = []
    for series in (raw.get("data") or {}).get("result") or []:
        for _, value in series.get("values") or []:
            try:
                v = float(value)
            except (TypeError, ValueError):
                continue
            if v == v and v not in (float("inf"), float("-inf")):  # drop NaN / Inf
                out.append(v)
    return out


class BaselineAnalyzer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.prom = PrometheusClient(settings.PROMETHEUS_URL, settings.OBSERVABILITY_TIMEOUT_SECONDS)

    def windows(self, window: TimeWindow) -> Tuple[TimeWindow, TimeWindow]:
        s = self.settings
        end = min(window.end, datetime.now(timezone.utc))
        start = min(window.start, end - timedelta(seconds=s.BASELINE_MIN_WINDOW_SECONDS))
        baseline_end = start - timedelta(seconds=s.BASELINE_GAP_SECONDS)
        baseline_start = baseline_end - timedelta(seconds=s.BASELINE_LOOKBACK_SECONDS)
        return TimeWindow(start=baseline_start, end=baseline_end), TimeWindow(start=start, end=end)

    async def _series(self, query: str, window: TimeWindow) -> List[float]:
        raw = await self.prom.query_range(
            query,
            start=str(window.start.timestamp()),
            end=str(window.end.timestamp()),
            step=f"{self.settings.BASELINE_STEP_SECONDS}s",
        )
        return _values(raw)

    async def analyze(self, incident_id: str, services: Optional[List[str]] = None) -> BaselineResult:
        import asyncio

        incident = await run_in_threadpool(incident_store.get, incident_id)
        if not incident:
            raise KeyError(f"Incident {incident_id} not found")
        targets = [s for s in (services or incident.affected_services) if s and s != "unknown"]
        baseline_window, incident_window = self.windows(incident.time_window)

        async def one(service: str, signal: Signal) -> BaselineSignalResult:
            query = signal.query(service)
            try:
                base, cur = await asyncio.gather(
                    self._series(query, baseline_window), self._series(query, incident_window)
                )
            except Exception as exc:
                logger.warning("Baseline query %s/%s failed: %s", service, signal.name, exc)
                return BaselineSignalResult(
                    service=service, signal=signal.name, unit=signal.unit, anomalous=False,
                    reason=f"query failed: {exc}",
                )
            return evaluate(
                signal, service, base, cur,
                z_threshold=self.settings.BASELINE_Z_THRESHOLD,
                min_points=self.settings.BASELINE_MIN_POINTS,
            )

        signals = list(await asyncio.gather(*(one(svc, sig) for svc in targets for sig in SIGNALS)))
        result = BaselineResult(
            incident_id=incident_id,
            analyzed_at=datetime.now(timezone.utc),
            baseline_window=baseline_window,
            incident_window=incident_window,
            signals=signals,
            anomalies=sum(s.anomalous for s in signals),
        )

        def record(fresh) -> None:
            fresh.metadata = {**fresh.metadata, "baseline": result.model_dump(mode="json", exclude={"incident_id"})}

        await run_in_threadpool(incident_store.update, incident_id, record)
        logger.info(
            "Baseline analysis for %s: %d anomalous of %d signal(s)", incident_id, result.anomalies, len(signals)
        )
        return result
