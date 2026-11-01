"""Incident store facade.

Uses PostgreSQL when Settings.DATABASE_URL is set; otherwise in-memory.
Call sites stay unchanged: save / get / list_all / delete / clear.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from app.core.logging import get_logger
from app.schemas.incident import IncidentContext

logger = get_logger(__name__)

# --------------- In-memory backend ----------------------------------------

_store: Dict[str, IncidentContext] = {}


def _mem_save(incident: IncidentContext) -> IncidentContext:
    _store[incident.incident_id] = incident
    return incident


def _mem_get(incident_id: str) -> Optional[IncidentContext]:
    return _store.get(incident_id)


def _mem_list_all() -> List[IncidentContext]:
    return list(_store.values())


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


def delete(incident_id: str) -> bool:
    if _use_db():
        return _pg_delete(incident_id)
    return _mem_delete(incident_id)


def clear() -> None:
    """Clear all incidents. Used by tests; also wipes DB table if enabled."""
    if _use_db():
        _pg_clear()
    _mem_clear()
