"""Log monitor: detect error patterns in application logs and open incidents.

Read-only and non-intrusive: it only *queries* Loki (LOG_MONITOR_QUERY). How
logs get into Loki is up to the collector (in the demo, Grafana Alloy reads
container stdout through a read-only Docker API proxy, and tails log files),
so watched applications need no code or config changes.

Every LOG_MONITOR_INTERVAL_SECONDS, run_once():
  1. reads new lines since the last cursor (LogQL line filter for error-ish words)
  2. parses each line: JSON logs (level/message/trace_id) or plain text
     (level word or keyword guess); keeps failures only: ERROR/FATAL lines,
     HTTP 5xx access lines, and WARN lines with failure words
  3. turns messages into templates (IDs/numbers stripped) and classifies them
     (database, payment, timeout, http_5xx, auth, resource, dependency, crash)
  4. per (service, template): triggers on a NEW pattern (seen at least
     LOG_MONITOR_NEW_PATTERN_MIN_COUNT times, or once if FATAL) or a SPIKE of a
     known pattern (robust z-score vs its recent per-poll counts)
  5. groups by (service, error class): one open incident per group; new samples
     are added to it; a recurring group (incidents in the last
     LOG_MONITOR_RECURRING_DAYS) is flagged
  6. new incident -> notify incident_created -> investigate (enrich, baseline,
     correlate, RCA, runbook; same pipeline and notifications as alerts)
  7. open log-monitor incidents with no matching errors for
     LOG_MONITOR_RESOLVE_AFTER_SECONDS are resolved (notify incident_resolved)

Pattern history is in memory (a restart relearns it); incidents, grouping and
recurrence live in the incident store.
"""
from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

from starlette.concurrency import run_in_threadpool

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.integrations.loki_client import LokiClient
from app.schemas.incident import BuildContextRequest, IncidentContext, IncidentStatus
from app.schemas.telemetry import LogRecord, Resource, SeverityLevel
from app.services import incident_store
from app.services.baseline import MAD_SCALE, robust_stats
from app.services.context_builder import build_context
from app.services.correlation import _SEV_RANK
from app.services.similarity import log_template

logger = get_logger(__name__)

SOURCE = "log_monitor"
GROUP_KEY = "log_group_key"
HISTORY_POLLS = 60  # per-template counts kept for spike detection
MIN_HISTORY_FOR_SPIKE = 5
MAX_TRACKED_PATTERNS = 5000
MAX_SAMPLES_PER_PATTERN = 5
REINVESTIGATE_AFTER_SECONDS = 300

# LogQL line filter (backticks: raw string). Cheap pre-filter; parse_line decides.
ERRORISH = (
    r"(?i)(error|exception|fatal|panic|fail|refused|timed? ?out|timeout|critical|denied|"
    r"unavailable|traceback|oom|\" 5[0-9][0-9]|status[=: ]5[0-9][0-9])"
)

_LEVEL_WORD = re.compile(r"\b(FATAL|CRITICAL|PANIC|ERROR|ERR|WARNING|WARN|INFO|DEBUG|TRACE)\b")
# "2026-11-03T10:00:00.123Z ", "[2026-11-03 10:00:00,123] ", "Nov 03 10:00:00 " ...
_TS_PREFIX = re.compile(
    r"^\[?(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?|"
    r"[A-Z][a-z]{2} +\d{1,2} \d{2}:\d{2}:\d{2})\]?[ \t]+"
)
_LEVEL_PREFIX = re.compile(r"^\[?(FATAL|CRITICAL|PANIC|ERROR|ERR|WARNING|WARN|INFO|DEBUG|TRACE)\]?:?[ \t]+")
SYMPTOM_CLASSES = {"http_5xx"}  # attached to a cause's incident on the same service
_HTTP_5XX = re.compile(r'(" 5\d\d\b|\bstatus[=: ]5\d\d\b|\bHTTP[/ ]\S* 5\d\d\b)', re.I)
_FAIL_WORDS = re.compile(r"(fail|error|exception|refused|timed? ?out|timeout|denied|unavailable)", re.I)

_LEVELS = {
    "fatal": SeverityLevel.FATAL, "critical": SeverityLevel.FATAL, "panic": SeverityLevel.FATAL,
    "error": SeverityLevel.ERROR, "err": SeverityLevel.ERROR,
    "warning": SeverityLevel.WARN, "warn": SeverityLevel.WARN,
    "info": SeverityLevel.INFO, "debug": SeverityLevel.DEBUG, "trace": SeverityLevel.TRACE,
}

# Checked in order: the first match wins.
ERROR_CLASSES: List[Tuple[str, str, re.Pattern]] = [
    ("database", "Database errors", re.compile(
        r"(sql|database|\bdb\b|db[-_.]|postgres|mysql|mongo|redis|jdbc|hikari|sequelize|"
        r"connection pool|deadlock|:5432|:3306|:27017|:6379)", re.I)),
    ("payment", "Payment failures", re.compile(r"(payment|charge|card declined|gateway|stripe|billing)", re.I)),
    ("resource", "Resource exhaustion", re.compile(
        r"(out ?of ?memory|\boom|heap space|memory limit|no space left|disk full|too many open files)", re.I)),
    ("timeout", "Timeouts", re.compile(r"(timed? ?out|timeout|deadline exceeded|etimedout)", re.I)),
    ("auth", "Authentication / authorization errors", re.compile(
        r"(unauthori[sz]ed|forbidden|invalid token|authentication|access denied|\b40[13]\b)", re.I)),
    ("dependency", "Dependency / connectivity errors", re.compile(
        r"(econnrefused|connection refused|enotfound|unreachable|upstream|connection reset|\b50[23]\b)", re.I)),
    ("http_5xx", "HTTP 5xx errors", _HTTP_5XX),
    ("crash", "Crashes / unhandled exceptions", re.compile(
        r"(panic|segfault|traceback|unhandled|uncaught|fatal)", re.I)),
]
CLASS_TITLES = {name: title for name, title, _ in ERROR_CLASSES} | {"application_error": "Application errors"}


# --------------- Parsing / classification (pure) ---------------------------

@dataclass(frozen=True)
class ParsedLine:
    severity: SeverityLevel
    message: str
    trace_id: Optional[str]


def _level(value: Any) -> Optional[SeverityLevel]:
    return _LEVELS.get(str(value).strip().lower()) if value else None


def parse_line(line: str, labels: Optional[Dict[str, str]] = None) -> ParsedLine:
    """Severity, message and trace id from a JSON or plain-text log line."""
    text = line.strip()
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            message = str(obj.get("message") or obj.get("msg") or obj.get("body") or obj.get("log") or text)
            exception = obj.get("exception") or obj.get("error") or obj.get("stack")
            if exception and isinstance(exception, str):
                last = exception.strip().splitlines()[-1] if exception.strip() else ""
                if last and last not in message:
                    message = f"{message} | {last}"
            severity = (
                _level(obj.get("level")) or _level(obj.get("levelname")) or _level(obj.get("severity"))
                or _level(obj.get("lvl")) or _level((labels or {}).get("level"))
                or _guess(message)
            )
            trace = obj.get("trace_id") or obj.get("traceid") or obj.get("traceId")
            return ParsedLine(severity, message, str(trace) if trace else None)
    match = _LEVEL_WORD.search(text[:160])
    severity = _LEVELS[match.group(1).lower()] if match else _guess(text)
    message = _LEVEL_PREFIX.sub("", _TS_PREFIX.sub("", text, count=1), count=1) or text
    return ParsedLine(severity, message, None)


def _guess(text: str) -> SeverityLevel:
    lower = text.lower()
    if "fatal" in lower or "panic" in lower:
        return SeverityLevel.FATAL
    if "error" in lower or "exception" in lower or "traceback" in lower:
        return SeverityLevel.ERROR
    if "warn" in lower:
        return SeverityLevel.WARN
    return SeverityLevel.INFO


def is_failure(parsed: ParsedLine) -> bool:
    if parsed.severity in (SeverityLevel.ERROR, SeverityLevel.FATAL):
        return True
    if _HTTP_5XX.search(parsed.message):
        return True
    return parsed.severity == SeverityLevel.WARN and bool(_FAIL_WORDS.search(parsed.message))


def classify(message: str) -> str:
    # An access line reporting a 5xx response is a symptom, whatever else it says
    # ("GET /stock status=503" must not count as a dependency error of its own).
    if _HTTP_5XX.search(message):
        return "http_5xx"
    for name, _, pattern in ERROR_CLASSES:
        if pattern.search(message):
            return name
    return "application_error"


def spike_score(history: List[int], count: int) -> float:
    median, mad = robust_stats([float(c) for c in history])
    return (count - median) / max(MAD_SCALE * mad, 1.0)


# --------------- Monitor -----------------------------------------------------

@dataclass
class _Bucket:
    service: str
    template: str
    error_class: str
    count: int = 0
    severity: SeverityLevel = SeverityLevel.WARN
    samples: List[LogRecord] = field(default_factory=list)
    first_ts: Optional[datetime] = None
    last_ts: Optional[datetime] = None


@dataclass
class _Pattern:
    service: str
    template: str
    error_class: str
    first_seen: datetime
    total: int = 0
    history: Deque[int] = field(default_factory=lambda: deque(maxlen=HISTORY_POLLS))


def _ns_to_dt(ns: str | int) -> datetime:
    return datetime.fromtimestamp(int(ns) / 1e9, tz=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class LogMonitor:
    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self._lock = asyncio.Lock()
        self._patterns: Dict[Tuple[str, str], _Pattern] = {}
        self._cursor_ns: Optional[int] = None
        self._tasks: Set[asyncio.Task] = set()
        self._investigating: Set[str] = set()
        self._last_investigated: Dict[str, datetime] = {}
        self._loop_task: Optional[asyncio.Task] = None
        self.stats: Dict[str, Any] = {
            "runs": 0, "lines_scanned": 0, "failures_seen": 0, "incidents_opened": 0,
            "incidents_updated": 0, "incidents_resolved": 0, "last_run_at": None, "last_error": None,
        }

    # ---- Loki ---------------------------------------------------------------

    def _query(self) -> str:
        return f"{self.settings.LOG_MONITOR_QUERY} |~ `{ERRORISH}`"

    async def _read(self, start_ns: int, end_ns: int) -> List[Dict[str, Any]]:
        s = self.settings
        client = LokiClient(s.LOKI_URL, s.OBSERVABILITY_TIMEOUT_SECONDS)
        lines: List[Dict[str, Any]] = []
        page_limit = min(5000, s.LOG_MONITOR_MAX_LINES_PER_RUN)
        cursor = start_ns
        while len(lines) < s.LOG_MONITOR_MAX_LINES_PER_RUN:
            raw = await client.query_range(self._query(), str(cursor), str(end_ns), limit=page_limit, direction="forward")
            page = client.parse_streams(raw)
            lines.extend(page)
            if len(page) < page_limit:
                break
            cursor = max(int(item["timestamp_ns"]) for item in page) + 1
        lines.sort(key=lambda item: int(item["timestamp_ns"]))
        return lines

    # ---- detection ----------------------------------------------------------

    def _service(self, labels: Dict[str, str]) -> str:
        return (
            labels.get(self.settings.LOG_MONITOR_SERVICE_LABEL)
            or labels.get("service_name") or labels.get("container") or labels.get("job") or "unknown"
        )

    def _bucket(self, lines: List[Dict[str, Any]]) -> Dict[Tuple[str, str], _Bucket]:
        excluded = set(self.settings.LOG_MONITOR_EXCLUDE_SERVICES)
        buckets: Dict[Tuple[str, str], _Bucket] = {}
        for item in lines:
            labels = item.get("labels") or {}
            service = self._service(labels)
            if service in excluded:
                continue
            parsed = parse_line(item.get("line") or "", labels)
            if not is_failure(parsed):
                continue
            self.stats["failures_seen"] += 1
            template = log_template(parsed.message)
            key = (service, template)
            bucket = buckets.get(key)
            if bucket is None:
                bucket = buckets[key] = _Bucket(service, template, classify(parsed.message))
            ts = _ns_to_dt(item["timestamp_ns"])
            bucket.count += 1
            bucket.first_ts = bucket.first_ts or ts
            bucket.last_ts = ts
            if _SEV_RANK[parsed.severity] > _SEV_RANK[bucket.severity]:
                bucket.severity = parsed.severity
            if len(bucket.samples) < MAX_SAMPLES_PER_PATTERN:
                bucket.samples.append(LogRecord(
                    timestamp=ts,
                    severity=parsed.severity,
                    body=parsed.message[:1000],
                    attributes={k: str(v) for k, v in labels.items()},
                    resource=Resource(service_name=service),
                    trace_id=parsed.trace_id,
                ))
        return buckets

    def _detect(self, buckets: Dict[Tuple[str, str], _Bucket]) -> Dict[Tuple[str, str], str]:
        """Trigger reason per pattern key ("new_pattern" / "spike"); updates history."""
        s = self.settings
        now = _now()
        triggers: Dict[Tuple[str, str], str] = {}
        for key, bucket in buckets.items():
            pattern = self._patterns.get(key)
            history = list(pattern.history) if pattern else []
            if len(history) < MIN_HISTORY_FOR_SPIKE:
                if bucket.count >= s.LOG_MONITOR_NEW_PATTERN_MIN_COUNT or bucket.severity == SeverityLevel.FATAL:
                    triggers[key] = "new_pattern"
            elif bucket.count >= s.LOG_MONITOR_MIN_COUNT and spike_score(history, bucket.count) >= s.LOG_MONITOR_Z_THRESHOLD:
                triggers[key] = "spike"

        for key, bucket in buckets.items():
            if key not in self._patterns:
                if len(self._patterns) >= MAX_TRACKED_PATTERNS:
                    oldest = min(self._patterns, key=lambda k: self._patterns[k].first_seen)
                    del self._patterns[oldest]
                self._patterns[key] = _Pattern(bucket.service, bucket.template, bucket.error_class, now)
        for key, pattern in self._patterns.items():
            count = buckets[key].count if key in buckets else 0
            pattern.history.append(count)
            pattern.total += count
        return triggers

    async def _group(self, buckets: Dict[Tuple[str, str], _Bucket]) -> Dict[Tuple[str, str], List[_Bucket]]:
        """Group by (service, error class). Symptom classes (HTTP 5xx) join the
        service's cause class: the strongest cause in this poll, else the
        service's open log-monitor incident, so one failure is one incident."""
        by_service: Dict[str, List[_Bucket]] = {}
        for bucket in buckets.values():
            by_service.setdefault(bucket.service, []).append(bucket)
        open_causes: Dict[str, str] = {}
        if any(b.error_class in SYMPTOM_CLASSES for b in buckets.values()):
            for incident in await run_in_threadpool(
                lambda: incident_store.list_by_metadata("source", SOURCE, open_only=True, limit=500)
            ):
                lm = incident.metadata.get("log_monitor") or {}
                if lm.get("error_class") not in SYMPTOM_CLASSES and lm.get("service"):
                    open_causes.setdefault(lm["service"], lm["error_class"])

        groups: Dict[Tuple[str, str], List[_Bucket]] = {}
        for service, members in by_service.items():
            causes = [b for b in members if b.error_class not in SYMPTOM_CLASSES]
            cause = max(causes, key=lambda b: b.count).error_class if causes else open_causes.get(service)
            for b in members:
                cls = cause if (b.error_class in SYMPTOM_CLASSES and cause) else b.error_class
                groups.setdefault((service, cls), []).append(b)
        return groups

    # ---- incidents ------------------------------------------------------------

    async def _handle_group(
        self, group: Tuple[str, str], buckets: List[_Bucket], reasons: Dict[str, str]
    ) -> None:
        service, error_class = group
        group_key = f"{service}:{error_class}"
        existing = await run_in_threadpool(incident_store.find_open_by_metadata, GROUP_KEY, group_key)
        if existing is None and not reasons:
            return  # failures below the trigger thresholds, no open incident to extend

        samples = [r for b in buckets for r in b.samples]
        last_seen = max(b.last_ts for b in buckets if b.last_ts)
        first_seen = min(b.first_ts for b in buckets if b.first_ts)
        count = sum(b.count for b in buckets)
        severity = max((b.severity for b in buckets), key=lambda sv: _SEV_RANK[sv])

        if existing is not None:
            known = set((existing.metadata.get("log_monitor") or {}).get("patterns", {}))
            new_templates = [b.template for b in buckets if b.template not in known]
            await run_in_threadpool(
                incident_store_merge, existing.incident_id, samples, buckets, count, last_seen, severity
            )
            self.stats["incidents_updated"] += 1
            if new_templates and reasons and self._may_reinvestigate(existing.incident_id):
                self._spawn(self._investigate(existing.incident_id))
            return

        incident = await run_in_threadpool(
            self._create, service, error_class, group_key, buckets, reasons, samples,
            count, first_seen, last_seen, severity,
        )
        self.stats["incidents_opened"] += 1
        logger.info("Log monitor opened incident %s for %s (%s)", incident.incident_id, group_key, reasons)
        self._spawn(self._announce_and_investigate(incident, reasons))

    def _create(self, service, error_class, group_key, buckets, reasons, samples, count,
                first_seen, last_seen, severity) -> IncidentContext:
        s = self.settings
        since = _now().timestamp() - s.LOG_MONITOR_RECURRING_DAYS * 86400
        previous = [
            i for i in incident_store.list_by_metadata(GROUP_KEY, group_key, limit=200)
            if i.time_window.start.timestamp() >= since
        ]
        top = max(buckets, key=lambda b: b.count)
        title = f"[Logs] {service}: {CLASS_TITLES.get(error_class, error_class)}"
        if previous:
            title += f" (recurring: {len(previous) + 1}x in {s.LOG_MONITOR_RECURRING_DAYS} days)"
        example = top.samples[0].body if top.samples else top.template
        incident = build_context(
            BuildContextRequest(
                title=title,
                severity=severity,
                affected_services=[service],
                logs=samples,
                notes=f"Opened by the log monitor: {count} matching error line(s); e.g. {example[:300]}",
            )
        )

        def record(fresh: IncidentContext) -> None:
            fresh.metadata = {
                **fresh.metadata,
                "source": SOURCE,
                GROUP_KEY: group_key,
                "log_monitor": {
                    "service": service,
                    "error_class": error_class,
                    "detection": sorted(set(reasons.values())),
                    "patterns": {b.template: b.count for b in buckets},
                    "error_count": count,
                    "first_seen": first_seen.isoformat(),
                    "last_seen": last_seen.isoformat(),
                    "recurring_count": len(previous),
                    "previous_incidents": [i.incident_id for i in previous[:5]],
                    "example": example[:500],
                },
            }

        stored, _ = incident_store.update(incident.incident_id, record)
        return stored

    async def _announce_and_investigate(self, incident: IncidentContext, reasons: Dict[str, str]) -> None:
        from app.services.notifier import notify

        lm = incident.metadata.get("log_monitor") or {}
        await notify("incident_created", incident, {
            "source": SOURCE,
            "error_class": lm.get("error_class"),
            "detection": lm.get("detection"),
            "error_count": lm.get("error_count"),
            "recurring_count": lm.get("recurring_count"),
            "example": lm.get("example"),
        }, self.settings)
        if self.settings.LOG_MONITOR_INVESTIGATE:
            await self._investigate(incident.incident_id)

    def _may_reinvestigate(self, incident_id: str) -> bool:
        last = self._last_investigated.get(incident_id)
        return incident_id not in self._investigating and (
            last is None or (_now() - last).total_seconds() >= REINVESTIGATE_AFTER_SECONDS
        )

    async def _investigate(self, incident_id: str) -> None:
        if not self.settings.LOG_MONITOR_INVESTIGATE or incident_id in self._investigating:
            return
        from app.providers.factory import get_ai_provider
        from app.services.alert_ingest import investigate

        self._investigating.add(incident_id)
        self._last_investigated[incident_id] = _now()
        try:
            await investigate(incident_id, get_ai_provider(), self.settings)
        except Exception:
            logger.exception("Log-monitor investigation failed for %s", incident_id)
        finally:
            self._investigating.discard(incident_id)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _auto_resolve(self) -> None:
        from app.services.notifier import notify

        quiet = self.settings.LOG_MONITOR_RESOLVE_AFTER_SECONDS
        open_incidents = await run_in_threadpool(
            lambda: incident_store.list_by_metadata("source", SOURCE, open_only=True, limit=500)
        )
        now = _now()
        for incident in open_incidents:
            lm = incident.metadata.get("log_monitor") or {}
            try:
                last_seen = datetime.fromisoformat(lm["last_seen"])
            except (KeyError, TypeError, ValueError):
                continue
            idle = (now - last_seen).total_seconds()
            if idle < quiet or incident.incident_id in self._investigating:
                continue

            def resolve(fresh: IncidentContext) -> bool:
                if fresh.status not in (IncidentStatus.OPEN, IncidentStatus.INVESTIGATING):
                    return False
                fresh.status = IncidentStatus.RESOLVED
                line = f"[log-monitor] No matching errors for {int(idle)}s; auto-resolved at {now.isoformat()}"
                fresh.notes = f"{fresh.notes}\n{line}" if fresh.notes else line
                fresh.metadata = {**fresh.metadata, "resolved_at": now.isoformat()}
                return True

            stored, changed = await run_in_threadpool(incident_store.update, incident.incident_id, resolve)
            if changed:
                self.stats["incidents_resolved"] += 1
                first = lm.get("first_seen")
                duration = int((last_seen - datetime.fromisoformat(first)).total_seconds()) if first else None
                logger.info("Log monitor resolved incident %s after %ss quiet", incident.incident_id, int(idle))
                await notify("incident_resolved", stored, {
                    "source": SOURCE, "quiet_seconds": int(idle), "error_duration_seconds": duration,
                }, self.settings)

    # ---- run loop -------------------------------------------------------------

    async def run_once(self) -> Dict[str, Any]:
        async with self._lock:
            s = self.settings
            end_ns = int((_now().timestamp() - s.LOG_MONITOR_LAG_SECONDS) * 1e9)
            start_ns = self._cursor_ns or end_ns - s.LOG_MONITOR_START_LOOKBACK_SECONDS * 1_000_000_000
            summary: Dict[str, Any] = {"lines": 0, "failures": 0, "triggers": 0, "groups": 0}
            try:
                if end_ns > start_ns:
                    lines = await self._read(start_ns, end_ns)
                    self._cursor_ns = end_ns
                    buckets = self._bucket(lines)
                    triggers = self._detect(buckets)
                    groups = await self._group(buckets)
                    for group, members in groups.items():
                        reasons = {b.template: triggers[(b.service, b.template)]
                                   for b in members if (b.service, b.template) in triggers}
                        await self._handle_group(group, members, reasons)
                    summary.update(lines=len(lines), failures=sum(b.count for b in buckets.values()),
                                   triggers=len(triggers), groups=len(groups))
                    self.stats["lines_scanned"] += len(lines)
                await self._auto_resolve()
                self.stats["last_error"] = None
            except Exception as exc:
                logger.warning("Log monitor run failed: %s", exc)
                self.stats["last_error"] = str(exc)
                summary["error"] = str(exc)
            self.stats["runs"] += 1
            self.stats["last_run_at"] = _now().isoformat()
            return summary

    async def run_forever(self) -> None:
        logger.info("Log monitor started: query=%s every %ss", self._query(), self.settings.LOG_MONITOR_INTERVAL_SECONDS)
        while True:
            await self.run_once()
            await asyncio.sleep(self.settings.LOG_MONITOR_INTERVAL_SECONDS)

    def start(self) -> None:
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self.run_forever())

    async def stop(self) -> None:
        tasks = [t for t in [self._loop_task, *self._tasks] if t]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._loop_task = None

    async def wait_idle(self) -> None:
        """Wait for spawned notification/investigation tasks (tests, demo script)."""
        # Wait on unfinished tasks only: awaiting already-done tasks never yields,
        # so their discard callbacks would never run and this would spin forever.
        while pending := [t for t in self._tasks if not t.done()]:
            await asyncio.wait(pending)
        self._tasks = {t for t in self._tasks if not t.done()}

    def status(self) -> Dict[str, Any]:
        patterns = sorted(self._patterns.values(), key=lambda p: p.total, reverse=True)[:25]
        return {
            "enabled": self.settings.LOG_MONITOR_ENABLED,
            "running": self._loop_task is not None and not self._loop_task.done(),
            "query": self._query(),
            "interval_seconds": self.settings.LOG_MONITOR_INTERVAL_SECONDS,
            "cursor": _ns_to_dt(self._cursor_ns).isoformat() if self._cursor_ns else None,
            "investigations_running": len(self._investigating),
            **self.stats,
            "patterns_tracked": len(self._patterns),
            "top_patterns": [
                {"service": p.service, "template": p.template, "error_class": p.error_class,
                 "total": p.total, "last_poll_count": p.history[-1] if p.history else 0,
                 "first_seen": p.first_seen.isoformat()}
                for p in patterns
            ],
        }


def incident_store_merge(incident_id, samples, buckets, count, last_seen, severity) -> None:
    """Add new samples to an open incident and update its log_monitor metadata."""
    build_context(BuildContextRequest(incident_id=incident_id, logs=samples, severity=severity))

    def record(fresh: IncidentContext) -> None:
        lm = dict(fresh.metadata.get("log_monitor") or {})
        patterns = dict(lm.get("patterns") or {})
        for b in buckets:
            patterns[b.template] = patterns.get(b.template, 0) + b.count
        lm.update(
            patterns=patterns,
            error_count=int(lm.get("error_count") or 0) + count,
            last_seen=max(last_seen.isoformat(), lm.get("last_seen") or ""),
        )
        fresh.metadata = {**fresh.metadata, "log_monitor": lm}

    incident_store.update(incident_id, record)


_monitor: Optional[LogMonitor] = None


def get_monitor() -> LogMonitor:
    global _monitor
    if _monitor is None:
        _monitor = LogMonitor()
    return _monitor


def reset_monitor() -> None:
    """Tests: drop state so the next get_monitor() reads fresh settings."""
    global _monitor
    _monitor = None
