"""Evidence rows + optimistic locking.

- incidents.version: incremented on every save; stale saves are rejected
- incident_evidence: one row per log / metric / span (previously arrays
  inside incidents.payload, rewritten in full on every save)
- data: existing payload arrays are moved into incident_evidence and
  stripped from payload; downgrade folds them back

Dedup keys use the same canonical JSON as app.services.evidence, via the
telemetry schemas, so rows written here and at runtime agree.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-01
"""
import hashlib
from datetime import datetime

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
KINDS = (("logs", "log"), ("metrics", "metric"), ("traces", "span"))

incidents = sa.table(
    "incidents",
    sa.column("incident_id", sa.String),
    sa.column("payload", JSONType),
)
evidence = sa.table(
    "incident_evidence",
    sa.column("id", sa.BigInteger),
    sa.column("incident_id", sa.String),
    sa.column("kind", sa.String),
    sa.column("service", sa.String),
    sa.column("ts", sa.DateTime(timezone=True)),
    sa.column("dedup_key", sa.String),
    sa.column("data", JSONType),
)


def _normalise(kind: str, item: dict):
    """Validate through the runtime schema: canonical JSON, key, service, ts."""
    from app.schemas.telemetry import LogRecord, MetricPoint, Span

    model = {"log": LogRecord, "metric": MetricPoint, "span": Span}[kind].model_validate(item)
    canonical = model.model_dump_json()
    ts: datetime = model.start_time if kind == "span" else model.timestamp
    return (
        hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        model.resource.service_name,
        ts,
        model.model_dump(mode="json"),
    )


def upgrade() -> None:
    with op.batch_alter_table("incidents") as batch:
        batch.add_column(sa.Column("version", sa.Integer(), nullable=False, server_default="1"))

    op.create_table(
        "incident_evidence",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column(
            "incident_id",
            sa.String(64),
            sa.ForeignKey("incidents.incident_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("service", sa.String(200), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dedup_key", sa.String(64), nullable=False),
        sa.Column("data", JSONType, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("incident_id", "kind", "dedup_key", name="uq_incident_evidence_key"),
    )
    op.create_index("ix_incident_evidence_incident_kind", "incident_evidence", ["incident_id", "kind"])

    conn = op.get_bind()
    for incident_id, payload in conn.execute(sa.select(incidents.c.incident_id, incidents.c.payload)).all():
        payload = dict(payload or {})
        rows, seen = [], set()
        for key, kind in KINDS:
            for item in payload.pop(key, None) or []:
                dedup_key, service, ts, data = _normalise(kind, item)
                if (kind, dedup_key) in seen:
                    continue
                seen.add((kind, dedup_key))
                rows.append({"incident_id": incident_id, "kind": kind, "service": service,
                             "ts": ts, "dedup_key": dedup_key, "data": data})
        if rows:
            conn.execute(evidence.insert(), rows)
        conn.execute(incidents.update().where(incidents.c.incident_id == incident_id).values(payload=payload))


def downgrade() -> None:
    conn = op.get_bind()
    for incident_id, payload in conn.execute(sa.select(incidents.c.incident_id, incidents.c.payload)).all():
        payload = dict(payload or {})
        items = conn.execute(
            sa.select(evidence.c.kind, evidence.c.data)
            .where(evidence.c.incident_id == incident_id)
            .order_by(evidence.c.id)
        ).all()
        for key, kind in KINDS:
            payload[key] = [data for k, data in items if k == kind]
        conn.execute(incidents.update().where(incidents.c.incident_id == incident_id).values(payload=payload))

    op.drop_index("ix_incident_evidence_incident_kind", table_name="incident_evidence")
    op.drop_table("incident_evidence")
    with op.batch_alter_table("incidents") as batch:
        batch.drop_column("version")
