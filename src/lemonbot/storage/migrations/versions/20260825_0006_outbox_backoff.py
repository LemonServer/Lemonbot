"""Persist outbox eligibility to prevent deferred dispatch busy loops."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260825_0006"
down_revision = "20260816_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("outbox") as batch:
        batch.add_column(sa.Column("next_attempt_at", sa.DateTime(timezone=True)))
        batch.create_index(
            "ix_outbox_eligibility",
            ["state", "next_attempt_at", "created_at", "id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("outbox") as batch:
        batch.drop_index("ix_outbox_eligibility")
        batch.drop_column("next_attempt_at")
