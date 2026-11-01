"""Incident store facade.

Uses PostgreSQL when Settings.DATABASE_URL is set; otherwise in-memory.
Call sites stay unchanged: save / get / list_all / delete / clear.
List-style reads (list_page / list_summaries / stats) return newest-updated
first on both backends and avoid loading full evidence where possible.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.core.logging import get_logger
from app.schemas.incident import IncidentContext, IncidentStatus, IncidentSummary
from app.schemas.telemetry import SeverityLevel

logger = get_logger(__name__)

OPEN_STATUSES = (IncidentStatus.OPEN, IncidentStatus.INVESTIGATING)


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


# --------------- In-memory backend ----------------------------------------

# Insertion order == update order (save re-inserts), so reversed() is newest first.
_store: Dict[str, IncidentContext] = {}


def _mem_save(incident: IncidentContext) -> IncidentContext:
    _store.pop(incident.incident_id, None)
    _store[incident.incident_id] = incident
    return incident


def _mem_get(incident_id: str) -> Optional[IncidentContext]:
    return _store.get(incident_id)


def _mem_list_all() -> List[IncidentContext]:
    return list(reversed(_store.values()))


def _mem_filtered(
    status: Optional[IncidentStatus], severity: Optional[SeverityLevel]
) -> List[IncidentContext]:
    return [
        i
        for i in _mem_list_all()
        if (status is None or i.status == status) and (severity is None or i.severity == severity)
    ]


def _mem_list_page(status, severity, limit, offset) -> Tuple[List[IncidentContext], int]:
    items = _mem_filtered(status, severity)
    return items[offset : offset + limit], len(items)


def _mem_list_summaries(status, severity, limit, offset) -> Tuple[List[IncidentSummary], int]:
    items, total = _mem_list_page(status, severity, limit, offset)
    return [_to_summary(i) for i in items], total


def _avg(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _mem_stats() -> IncidentStats:
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
    if incident_id in _store:
        del _store[incident_id]
        return True
    return False


def _mem_clear() -> None:
    _store.clear()


# --------------- Postgres backend -----------------------------------------

def _pg_save(incident: IncidentContext) -> IncidentContext:
    from app.db.models import IncidentRow
    from app.db.session import get_session

    payload = incident.model_dump(mode="json")
    session = get_session()
    try:
        row = session.get(IncidentRow, incident.incident_id)
        if row is None:
            row = IncidentRow(
                incident_id=incident.incident_id,
                title=incident.title,
                status=incident.status.value,
                severity=incident.severity.value,
                payload=payload,
                notes=incident.notes,
            )
            session.add(row)
        else:
            row.title = incident.title
            row.status = incident.status.value
            row.severity = incident.severity.value
            row.payload = payload
            row.notes = incident.notes
        session.commit()
        return incident
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _pg_get(incident_id: str) -> Optional[IncidentContext]:
    from app.db.models import IncidentRow
    from app.db.session import get_session

    session = get_session()
    try:
        row = session.get(IncidentRow, incident_id)
        if row is None:
            return None
        return IncidentContext.model_validate(row.payload)
    finally:
        session.close()


def _pg_list_all() -> List[IncidentContext]:
    from app.db.models import IncidentRow
    from app.db.session import get_session

    session = get_session()
    try:
        rows = session.query(IncidentRow).order_by(IncidentRow.updated_at.desc()).all()
        return [IncidentContext.model_validate(r.payload) for r in rows]
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
        q = _pg_filtered_query(session, [IncidentRow.payload], status, severity)
        total = q.count()
        rows = q.order_by(IncidentRow.updated_at.desc()).offset(offset).limit(limit).all()
        return [IncidentContext.model_validate(r.payload) for r in rows], total
    finally:
        session.close()


def _pg_list_summaries(status, severity, limit, offset) -> Tuple[List[IncidentSummary], int]:
    """Selects only the summary fields so evidence arrays never leave the DB."""
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
            session.query(IncidentRow.payload)
            .filter(IncidentRow.status.in_([s.value for s in OPEN_STATUSES]))
            .filter(IncidentRow.payload[("metadata", key)].as_string() == value)
            .order_by(IncidentRow.updated_at.desc())
            .first()
        )
        return IncidentContext.model_validate(row.payload) if row else None
    finally:
        session.close()


def _pg_stats() -> IncidentStats:
    """Aggregates computed in SQL; no incident payload is sent to the app."""
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
    from app.db.models import IncidentRow
    from app.db.session import get_session

    session = get_session()
    try:
        row = session.get(IncidentRow, incident_id)
        if row is None:
            return False
        session.delete(row)
        session.commit()
        return True
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _pg_clear() -> None:
    from app.db.models import IncidentRow
    from app.db.session import get_session

    session = get_session()
    try:
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
    if _use_db():
        return _pg_save(incident)
    return _mem_save(incident)


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
    for incident in _mem_list_all():
        if incident.status in OPEN_STATUSES and incident.metadata.get(key) == value:
            return incident
    return None


def stats() -> IncidentStats:
    if _use_db():
        return _pg_stats()
    return _mem_stats()


def delete(incident_id: str) -> bool:
    if _use_db():
        return _pg_delete(incident_id)
    return _mem_delete(incident_id)


def clear() -> None:
    """Clear all incidents. Used by tests; also wipes DB table if enabled."""
    if _use_db():
        _pg_clear()
    _mem_clear()
