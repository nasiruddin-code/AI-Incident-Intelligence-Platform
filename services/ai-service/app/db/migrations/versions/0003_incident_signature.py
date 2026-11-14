"""Incident signature for similar-incident lookup.

Adds incidents.signature (sparse weighted features, see
app.services.similarity). Existing rows are left NULL and filled lazily the
first time a similarity search sees them (and on their next save).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-01
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("incidents") as batch:
        batch.add_column(sa.Column("signature", JSONType, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("incidents") as batch:
        batch.drop_column("signature")
