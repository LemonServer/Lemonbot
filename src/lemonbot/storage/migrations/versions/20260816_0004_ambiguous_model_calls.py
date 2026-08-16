"""Track model-call commit boundaries for crash-safe recovery."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260816_0004"
down_revision = "20260816_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("inbox") as batch:
        batch.add_column(sa.Column("model_started_at", sa.DateTime(timezone=True)))
    with op.batch_alter_table("proactive_jobs") as batch:
        batch.add_column(sa.Column("model_started_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    with op.batch_alter_table("proactive_jobs") as batch:
        batch.drop_column("model_started_at")
    with op.batch_alter_table("inbox") as batch:
        batch.drop_column("model_started_at")
