"""ORM models for persistent incidents.

Schema changes go through Alembic migrations in app/db/migrations/ —
never edit a table here without adding a migration.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON

JSONType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class IncidentRow(Base):
    __tablename__ = "incidents"

    incident_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # IncidentContext without logs/metrics/traces (those live in incident_evidence)
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False)
    # Optimistic locking: every successful save increments it; a save carrying
    # a stale version is rejected instead of overwriting a concurrent change.
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class IncidentEvidenceRow(Base):
    """One log record, metric point or span attached to an incident (append-only)."""

    __tablename__ = "incident_evidence"
    __table_args__ = (
        UniqueConstraint("incident_id", "kind", "dedup_key", name="uq_incident_evidence_key"),
        Index("ix_incident_evidence_incident_kind", "incident_id", "kind"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    incident_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("incidents.incident_id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # log | metric | span
    service: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # sha256 of the item's canonical JSON: re-adding the same evidence is a no-op
    dedup_key: Mapped[str] = mapped_column(String(64), nullable=False)
    data: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
