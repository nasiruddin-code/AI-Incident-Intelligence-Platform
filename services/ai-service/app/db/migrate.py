"""Run Alembic migrations programmatically (called from init_db at startup).

Databases created before migrations existed (by Base.metadata.create_all) have
an `incidents` table but no `alembic_version`; they are stamped at the
baseline revision first, then upgraded like any other database.
On Postgres an advisory lock keeps concurrently starting replicas from
migrating at the same time.
"""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine

from app.core.logging import get_logger

logger = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
BASELINE_REVISION = "0001"
_ADVISORY_LOCK_ID = 727_001  # arbitrary, constant across replicas


def alembic_config(connection: Connection | None = None, url: str | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    if url:
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def upgrade_to_head(engine: Engine) -> None:
    with engine.begin() as conn:
        is_postgres = conn.dialect.name == "postgresql"
        if is_postgres:
            conn.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": _ADVISORY_LOCK_ID})
        inspector = inspect(conn)
        cfg = alembic_config(connection=conn)
        if inspector.has_table("incidents") and not inspector.has_table("alembic_version"):
            logger.info("Existing pre-migration schema found; stamping baseline %s", BASELINE_REVISION)
            command.stamp(cfg, BASELINE_REVISION)
        command.upgrade(cfg, "head")
    logger.info("Database schema is at head")


def downgrade(engine: Engine, revision: str) -> None:
    with engine.begin() as conn:
        command.downgrade(alembic_config(connection=conn), revision)
