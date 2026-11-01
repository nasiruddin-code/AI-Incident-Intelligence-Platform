"""Incident store facade.

Uses PostgreSQL when Settings.DATABASE_URL is set; otherwise in-memory.
List-style reads (list_page / list_summaries / stats) return newest-updated
first on both backends and avoid loading full evidence where possible.

Concurrency: optimistic locking. Every incident carries a `version`; save()
succeeds only if the stored version still equals the one that was read, and
then increments it. Otherwise ConcurrentModificationError is raised instead
of silently overwriting another writer's change. Code that modifies an
existing incident should use update(incident_id, mutate), which re-reads and
re-applies `mutate` when it loses such a race.

Evidence (logs / metrics / spans) is append-only. In Postgres it lives in
incident_evidence (one row each, de-duplicated by content); a save writes
only evidence that is new, never rewrites what is already stored. Both
backends return copies: mutating a returned incident has no effect until it
is saved.
"""
from __future__ import annotations

import random
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple, TypeVar

from app.core.logging import get_logger
from app.schemas.incident import IncidentContext, IncidentStatus, IncidentSummary
from app.schemas.telemetry import SeverityLevel
from app.services.evidence import evidence_key

logger = get_logger(__name__)

OPEN_STATUSES = (IncidentStatus.OPEN, IncidentStatus.INVESTIGATING)
EVIDENCE_FIELDS = (("logs", "log"), ("metrics", "metric"), ("traces", "span"))
T = TypeVar("T")


class ConcurrentModificationError(Exception):
    """The incident changed since it was read; re-read and retry (see update())."""


@dataclass
class IncidentStats:
    """Aggregates backing the Prometheus /metrics endpoint."""
    total: int = 0
    open: int = 0
    by_severity: Dict[str, int] = field(default_factory=dict)
    rca_confidence_avg: float = 0.0
    rca_runs: int = 0
    runbook_confidence_avg: float = 0.0
    runbook_runs: int = 0
    severity_score_avg: float = 0.0


def _to_summary(incident: IncidentContext) -> IncidentSummary:
    return IncidentSummary(
        incident_id=incident.incident_id,
        title=incident.title,
        status=incident.status,
        severity=incident.severity,
        affected_services=incident.affected_services,
        scenario=incident.scenario,
        evidence_summary=incident.evidence_summary,
    )


def _avg(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


# --------------- In-memory backend ----------------------------------------

# Insertion order == update order (save re-inserts), so reversed() is newest first.
_store: Dict[str, IncidentContext] = {}
_mem_lock = threading.RLock()  # re-entrant: update() holds it across get + save


def _mem_save(incident: IncidentContext) -> IncidentContext:
    with _mem_lock:
        current = _store.get(incident.incident_id)
        stored_version = current.version if current else 0
        if incident.version != stored_version:
            raise ConcurrentModificationError(
                f"Incident {incident.incident_id} is at version {stored_version}, "
                f"save was based on version {incident.version}"
            )
        stored = incident.model_copy(deep=True)
        stored.version = stored_version + 1
        _store.pop(incident.incident_id, None)
        _store[incident.incident_id] = stored
    incident.version = stored.version
    return incident


def _mem_get(incident_id: str) -> Optional[IncidentContext]:
    with _mem_lock:
        current = _store.get(incident_id)
        return current.model_copy(deep=True) if current else None


def _mem_list_all() -> List[IncidentContext]:
    with _mem_lock:
        return [i.model_copy(deep=True) for i in reversed(_store.values())]


def _mem_filtered(
    status: Optional[IncidentStatus], severity: Optional[SeverityLevel]
) -> List[IncidentContext]:
    with _mem_lock:
        items = list(reversed(_store.values()))
    return [
        i
        for i in items
        if (status is None or i.status == status) and (severity is None or i.severity == severity)
    ]


def _mem_list_page(status, severity, limit, offset) -> Tuple[List[IncidentContext], int]:
    items = _mem_filtered(status, severity)
    return [i.model_copy(deep=True) for i in items[offset : offset + limit]], len(items)


def _mem_list_summaries(status, severity, limit, offset) -> Tuple[List[IncidentSummary], int]:
    items = _mem_filtered(status, severity)
    return [_to_summary(i) for i in items[offset : offset + limit]], len(items)


def _mem_find_open_by_metadata(key: str, value: str) -> Optional[IncidentContext]:
    for incident in _mem_filtered(None, None):
        if incident.status in OPEN_STATUSES and incident.metadata.get(key) == value:
            return incident.model_copy(deep=True)
    return None


def _mem_stats() -> IncidentStats:
    with _mem_lock:
        incidents = list(_store.values())

    def meta_values(key: str) -> List[float]:
        return [float(i.metadata[key]) for i in incidents if i.metadata.get(key) is not None]

    rca = meta_values("rca_confidence")
    rb = meta_values("runbook_confidence")
    return IncidentStats(
        total=len(incidents),
        open=sum(1 for i in incidents if i.status in OPEN_STATUSES),
        by_severity=dict(Counter(i.severity.value for i in incidents)),
        rca_confidence_avg=_avg(rca),
        rca_runs=len(rca),
        runbook_confidence_avg=_avg(rb),
        runbook_runs=len(rb),
        severity_score_avg=_avg(meta_values("severity_score")),
    )


def _mem_delete(incident_id: str) -> bool:
    with _mem_lock:
        return _store.pop(incident_id, None) is not None


def _mem_clear() -> None:
    with _mem_lock:
        _store.clear()


# --------------- Postgres backend -----------------------------------------

def _header_payload(incident: IncidentContext) -> dict:
    return incident.model_dump(mode="json", exclude={"logs", "metrics", "traces", "version"})


def _evidence_rows(incident: IncidentContext, existing_keys: set) -> List[dict]:
    rows = []
    for field_name, kind in EVIDENCE_FIELDS:
        for item in getattr(incident, field_name):
            key = evidence_key(item)
            if (kind, key) in existing_keys:
                continue
            existing_keys.add((kind, key))
            rows.append({
                "incident_id": incident.incident_id,
                "kind": kind,
                "service": item.resource.service_name,
                "ts": item.start_time if kind == "span" else item.timestamp,
                "dedup_key": key,
                "data": item.model_dump(mode="json"),
            })
    return rows


def _assemble(payload: dict, version: int, evidence: Iterable[Tuple[str, dict]]) -> IncidentContext:
    data = dict(payload)
    data["version"] = version
    for field_name, _ in EVIDENCE_FIELDS:
        data[field_name] = []
    by_kind = {kind: field_name for field_name, kind in EVIDENCE_FIELDS}
    for kind, item in evidence:
        data[by_kind[kind]].append(item)
    return IncidentContext.model_validate(data)


def _load_evidence(session, incident_ids: List[str]) -> Dict[str, List[Tuple[str, dict]]]:
    from app.db.models import IncidentEvidenceRow as E

    out: Dict[str, List[Tuple[str, dict]]] = {i: [] for i in incident_ids}
    if not incident_ids:
        return out
    rows = (
        session.query(E.incident_id, E.kind, E.data)
        .filter(E.incident_id.in_(incident_ids))
        .order_by(E.id)
        .all()
    )
    for incident_id, kind, data in rows:
        out[incident_id].append((kind, data))
    return out


def _pg_write(session, incident: IncidentContext) -> None:
    """Version-checked write of header + new evidence inside the caller's transaction."""
    from sqlalchemy import func, insert, update as sql_update

    from app.db.models import IncidentEvidenceRow as E
    from app.db.models import IncidentRow

    expected = incident.version
    values = {
        "title": incident.title,
        "status": incident.status.value,
        "severity": incident.severity.value,
        "payload": _header_payload(incident),
        "notes": incident.notes,
    }
    if expected == 0:
        if session.get(IncidentRow, incident.incident_id) is not None:
            raise ConcurrentModificationError(f"Incident {incident.incident_id} already exists")
        session.add(IncidentRow(incident_id=incident.incident_id, version=1, **values))
        session.flush()
        existing_keys: set = set()
    else:
        result = session.execute(
            sql_update(IncidentRow)
            .where(IncidentRow.incident_id == incident.incident_id, IncidentRow.version == expected)
            .values(version=expected + 1, updated_at=func.now(), **values)
        )
        if result.rowcount != 1:
            raise ConcurrentModificationError(
                f"Incident {incident.incident_id} changed since version {expected}"
            )
        existing_keys = set(
            session.query(E.kind, E.dedup_key).filter(E.incident_id == incident.incident_id).all()
        )

    rows = _evidence_rows(incident, existing_keys)
    if rows:
        session.execute(insert(E), rows)


def _pg_update_locked(incident_id: str, mutate: Callable[[IncidentContext], T]) -> Tuple[IncidentContext, T]:
    """Postgres read-modify-write under a row lock: concurrent writers queue
    (SELECT ... FOR UPDATE) instead of failing and retrying."""
    from app.db.models import IncidentRow
    from app.db.session import get_session

    session = get_session()
    try:
        row = (
            session.query(IncidentRow.payload, IncidentRow.version)
            .filter(IncidentRow.incident_id == incident_id)
            .with_for_update()
            .one_or_none()
        )
        if row is None:
            raise KeyError(f"Incident {incident_id} not found")
        incident = _assemble(row.payload, row.version, _load_evidence(session, [incident_id])[incident_id])
        result = mutate(incident)
        _pg_write(session, incident)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    incident.version += 1
    return incident, result


def _pg_save(incident: IncidentContext) -> IncidentContext:
    from sqlalchemy.exc import IntegrityError

    from app.db.session import get_session

    expected = incident.version
    session = get_session()
    try:
        _pg_write(session, incident)
        session.commit()
    except IntegrityError as exc:
        # Another writer inserted the same incident or evidence concurrently.
        session.rollback()
        raise ConcurrentModificationError(str(exc.orig)) from exc
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    incident.version = expected + 1
    return incident


def _pg_get(incident_id: str) -> Optional[IncidentContext]:
    from app.db.models import IncidentRow
    from app.db.session import get_session

    session = get_session()
    try:
        row = session.get(IncidentRow, incident_id)
        if row is None:
            return None
        evidence = _load_evidence(session, [incident_id])[incident_id]
        return _assemble(row.payload, row.version, evidence)
    finally:
        session.close()


def _pg_list_rows(session, rows) -> List[IncidentContext]:
    evidence = _load_evidence(session, [r.incident_id for r in rows])
    return [_assemble(r.payload, r.version, evidence[r.incident_id]) for r in rows]


def _pg_list_all() -> List[IncidentContext]:
    from app.db.models import IncidentRow
    from app.db.session import get_session

    session = get_session()
    try:
        rows = (
            session.query(IncidentRow.incident_id, IncidentRow.payload, IncidentRow.version)
            .order_by(IncidentRow.updated_at.desc())
            .all()
        )
        return _pg_list_rows(session, rows)
    finally:
        session.close()


def _pg_filtered_query(session, columns, status, severity):
    from app.db.models import IncidentRow

    q = session.query(*columns)
    if status is not None:
        q = q.filter(IncidentRow.status == status.value)
    if severity is not None:
        q = q.filter(IncidentRow.severity == severity.value)
    return q


def _pg_list_page(status, severity, limit, offset) -> Tuple[List[IncidentContext], int]:
    from app.db.models import IncidentRow
    from app.db.session import get_session

    session = get_session()
    try:
        q = _pg_filtered_query(
            session, [IncidentRow.incident_id, IncidentRow.payload, IncidentRow.version], status, severity
        )
        total = q.count()
        rows = q.order_by(IncidentRow.updated_at.desc()).offset(offset).limit(limit).all()
        return _pg_list_rows(session, rows), total
    finally:
        session.close()


def _pg_list_summaries(status, severity, limit, offset) -> Tuple[List[IncidentSummary], int]:
    """Selects only the summary fields; evidence is never read."""
    from app.db.models import IncidentRow
    from app.db.session import get_session

    p = IncidentRow.payload
    columns = [
        IncidentRow.incident_id,
        IncidentRow.title,
        IncidentRow.status,
        IncidentRow.severity,
        p["affected_services"].label("affected_services"),
        p["scenario"].as_string().label("scenario"),
        p["evidence_summary"].as_string().label("evidence_summary"),
    ]
    session = get_session()
    try:
        q = _pg_filtered_query(session, columns, status, severity)
        total = q.count()
        rows = q.order_by(IncidentRow.updated_at.desc()).offset(offset).limit(limit).all()
        items = [
            IncidentSummary(
                incident_id=r.incident_id,
                title=r.title,
                status=r.status,
                severity=r.severity,
                affected_services=r.affected_services or [],
                scenario=r.scenario,
                evidence_summary=r.evidence_summary,
            )
            for r in rows
        ]
        return items, total
    finally:
        session.close()


def _pg_find_open_by_metadata(key: str, value: str) -> Optional[IncidentContext]:
    from app.db.models import IncidentRow
    from app.db.session import get_session

    session = get_session()
    try:
        row = (
            session.query(IncidentRow.incident_id, IncidentRow.payload, IncidentRow.version)
            .filter(IncidentRow.status.in_([s.value for s in OPEN_STATUSES]))
            .filter(IncidentRow.payload[("metadata", key)].as_string() == value)
            .order_by(IncidentRow.updated_at.desc())
            .first()
        )
        return _pg_list_rows(session, [row])[0] if row else None
    finally:
        session.close()


def _pg_stats() -> IncidentStats:
    """Aggregates computed in SQL; evidence is never read."""
    from sqlalchemy import func

    from app.db.models import IncidentRow
    from app.db.session import get_session

    def meta(key: str):
        return IncidentRow.payload[("metadata", key)].as_float()

    rca, rb, sev = meta("rca_confidence"), meta("runbook_confidence"), meta("severity_score")
    session = get_session()
    try:
        counts = (
            session.query(IncidentRow.severity, IncidentRow.status, func.count())
            .group_by(IncidentRow.severity, IncidentRow.status)
            .all()
        )
        agg = session.query(
            func.avg(rca), func.count(rca), func.avg(rb), func.count(rb), func.avg(sev)
        ).one()
    finally:
        session.close()

    by_severity: Counter = Counter()
    open_values = {s.value for s in OPEN_STATUSES}
    open_count = 0
    for severity, status, n in counts:
        by_severity[severity] += n
        if status in open_values:
            open_count += n
    return IncidentStats(
        total=sum(by_severity.values()),
        open=open_count,
        by_severity=dict(by_severity),
        rca_confidence_avg=float(agg[0] or 0.0),
        rca_runs=int(agg[1] or 0),
        runbook_confidence_avg=float(agg[2] or 0.0),
        runbook_runs=int(agg[3] or 0),
        severity_score_avg=float(agg[4] or 0.0),
    )


def _pg_delete(incident_id: str) -> bool:
    from app.db.models import IncidentEvidenceRow as E
    from app.db.models import IncidentRow
    from app.db.session import get_session

    session = get_session()
    try:
        # Explicit evidence delete: SQLite does not enforce ON DELETE CASCADE by default.
        session.query(E).filter(E.incident_id == incident_id).delete()
        deleted = session.query(IncidentRow).filter(IncidentRow.incident_id == incident_id).delete()
        session.commit()
        return bool(deleted)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _pg_clear() -> None:
    from app.db.models import IncidentEvidenceRow as E
    from app.db.models import IncidentRow
    from app.db.session import get_session

    session = get_session()
    try:
        session.query(E).delete()
        session.query(IncidentRow).delete()
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --------------- Public API -----------------------------------------------

def _use_db() -> bool:
    try:
        from app.db.session import is_db_enabled
        return is_db_enabled()
    except Exception:
        return False


def save(incident: IncidentContext) -> IncidentContext:
    """Store `incident` if nobody changed it since it was read (incident.version).

    New incidents have version 0. On success incident.version is the new
    stored version; on a race ConcurrentModificationError is raised.
    """
    if _use_db():
        return _pg_save(incident)
    return _mem_save(incident)


def update(
    incident_id: str,
    mutate: Callable[[IncidentContext], T],
    *,
    retries: int = 10,
) -> Tuple[IncidentContext, T]:
    """Read-modify-write that never loses a concurrent change.

    `mutate` changes the incident in place (it may return a value); keep it
    quick — slow work such as AI calls belongs before update(). Writers are
    serialized: Postgres holds a row lock (SELECT ... FOR UPDATE) for the
    duration, the in-memory store a process lock. Elsewhere (SQLite) it falls
    back to optimistic retries, re-reading and re-applying `mutate`, so mutate
    must be safe to re-run on a fresh copy. Raises KeyError if the incident
    does not exist.
    """
    if _use_db():
        from app.db.session import get_engine

        if get_engine().dialect.name == "postgresql":
            return _pg_update_locked(incident_id, mutate)
    else:
        with _mem_lock:
            incident = _mem_get(incident_id)
            if incident is None:
                raise KeyError(f"Incident {incident_id} not found")
            result = mutate(incident)
            _mem_save(incident)
            return incident, result

    for attempt in range(retries):
        incident = get(incident_id)
        if incident is None:
            raise KeyError(f"Incident {incident_id} not found")
        result = mutate(incident)
        try:
            save(incident)
            return incident, result
        except ConcurrentModificationError:
            if attempt == retries - 1:
                raise
            logger.info("Incident %s changed concurrently; retrying update (%d)", incident_id, attempt + 1)
            time.sleep(random.uniform(0.005, 0.03) * (attempt + 1))
    raise AssertionError("unreachable")


def get(incident_id: str) -> Optional[IncidentContext]:
    if _use_db():
        return _pg_get(incident_id)
    return _mem_get(incident_id)


def list_all() -> List[IncidentContext]:
    if _use_db():
        return _pg_list_all()
    return _mem_list_all()


def list_page(
    *,
    status: Optional[IncidentStatus] = None,
    severity: Optional[SeverityLevel] = None,
    limit: int = 50,
    offset: int = 0,
) -> Tuple[List[IncidentContext], int]:
    """Full incidents for one page, newest first. Returns (items, total_matching)."""
    if _use_db():
        return _pg_list_page(status, severity, limit, offset)
    return _mem_list_page(status, severity, limit, offset)


def list_summaries(
    *,
    status: Optional[IncidentStatus] = None,
    severity: Optional[SeverityLevel] = None,
    limit: int = 50,
    offset: int = 0,
) -> Tuple[List[IncidentSummary], int]:
    """Like list_page but without logs/metrics/traces. Returns (items, total_matching)."""
    if _use_db():
        return _pg_list_summaries(status, severity, limit, offset)
    return _mem_list_summaries(status, severity, limit, offset)


def find_open_by_metadata(key: str, value: str) -> Optional[IncidentContext]:
    """Most recently updated OPEN/INVESTIGATING incident whose metadata[key] == value."""
    if _use_db():
        return _pg_find_open_by_metadata(key, value)
    return _mem_find_open_by_metadata(key, value)


def stats() -> IncidentStats:
    if _use_db():
        return _pg_stats()
    return _mem_stats()


def delete(incident_id: str) -> bool:
    if _use_db():
        return _pg_delete(incident_id)
    return _mem_delete(incident_id)


def clear() -> None:
    """Clear all incidents. Used by tests; also wipes DB tables if enabled."""
    if _use_db():
        _pg_clear()
    _mem_clear()
