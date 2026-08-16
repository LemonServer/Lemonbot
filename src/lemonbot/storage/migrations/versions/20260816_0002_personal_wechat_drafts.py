"""Add non-dispatchable personal WeChat reply drafts."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260816_0002"
down_revision = "20260816_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "drafts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("draft_id", sa.String(36), nullable=False),
        sa.Column("channel", sa.String(64), nullable=False),
        sa.Column("chat_id", sa.String(512), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("reply_to_event_id", sa.String(256), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("draft_id", name="uq_drafts_draft_id"),
        sa.UniqueConstraint(
            "channel", "reply_to_event_id", name="uq_drafts_one_reply_per_event"
        ),
        sa.CheckConstraint("state IN ('pending')", name="ck_drafts_state"),
    )
    op.create_index("ix_drafts_pending", "drafts", ["state", "created_at", "id"])
    op.create_index(
        "ix_drafts_scope",
        "drafts",
        ["channel", "chat_id", "state", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_drafts_scope", table_name="drafts")
    op.drop_index("ix_drafts_pending", table_name="drafts")
    op.drop_table("drafts")
