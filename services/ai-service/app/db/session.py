"""SQLAlchemy engine / session helpers."""
from __future__ import annotations

from functools import lru_cache
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_engine: Optional[Engine] = None
_SessionLocal: Optional[sessionmaker] = None


def is_db_enabled() -> bool:
    url = get_settings().DATABASE_URL
    return bool(url and url.strip())


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        settings = get_settings()
        if not settings.DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not configured")
        _engine = create_engine(
            settings.DATABASE_URL,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
        logger.info("SQLAlchemy engine created")
    return _engine


def get_session() -> Session:
    if _SessionLocal is None:
        get_engine()
    assert _SessionLocal is not None
    return _SessionLocal()


def init_db() -> None:
    """Create tables if they do not exist."""
    if not is_db_enabled():
        logger.info("DATABASE_URL not set — using in-memory incident store")
        return
    from app.db.models import Base

    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables ensured")
