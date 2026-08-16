"""Add durable one-time tool approvals."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260816_0003"
down_revision = "20260816_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "approvals",
        sa.Column("approval_id", sa.String(36), primary_key=True),
        sa.Column("profile", sa.String(32), nullable=False),
        sa.Column("channel", sa.String(64), nullable=False),
        sa.Column("chat_id", sa.String(512), nullable=False),
        sa.Column("event_id", sa.String(256), nullable=False),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("action_kind", sa.String(128), nullable=False),
        sa.Column("arguments_summary", sa.Text(), nullable=False),
        sa.Column("arguments_sha256", sa.String(64), nullable=False),
        sa.Column("arguments_json", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("claim_token", sa.String(36)),
        sa.Column("outcome_code", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("claim_token", name="uq_approvals_claim_token"),
        sa.UniqueConstraint(
            "profile",
            "channel",
            "chat_id",
            "event_id",
            "tool_name",
            "action_kind",
            "arguments_sha256",
            name="uq_approvals_action_binding",
        ),
        sa.CheckConstraint(
            "state IN ('pending','executing','approved','denied','unknown')",
            name="ck_approvals_state",
        ),
        sa.CheckConstraint(
            "(state = 'pending' AND claim_token IS NULL AND claimed_at IS NULL "
            "AND resolved_at IS NULL) OR "
            "(state = 'executing' AND claim_token IS NOT NULL AND claimed_at IS NOT NULL "
            "AND resolved_at IS NULL) OR "
            "(state IN ('approved','unknown') AND claim_token IS NOT NULL "
            "AND claimed_at IS NOT NULL AND resolved_at IS NOT NULL) OR "
            "(state = 'denied' AND resolved_at IS NOT NULL)",
            name="ck_approvals_lifecycle_fields",
        ),
    )
    op.create_index(
        "ix_approvals_pending",
        "approvals",
        ["profile", "state", "expires_at", "created_at"],
    )
    op.create_index(
        "ix_approvals_scope",
        "approvals",
        ["profile", "channel", "chat_id", "event_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_approvals_scope", table_name="approvals")
    op.drop_index("ix_approvals_pending", table_name="approvals")
    op.drop_table("approvals")
