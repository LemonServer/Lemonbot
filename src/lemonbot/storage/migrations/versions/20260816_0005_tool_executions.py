"""Persist model-requested tool execution lifecycles."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260816_0005"
down_revision = "20260816_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tool_executions",
        sa.Column("execution_id", sa.String(36), primary_key=True),
        sa.Column("profile", sa.String(32), nullable=False),
        sa.Column("channel", sa.String(64), nullable=False),
        sa.Column("chat_id", sa.String(512), nullable=False),
        sa.Column("event_id", sa.String(256), nullable=False),
        sa.Column("call_id", sa.String(256), nullable=False),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("action_kind", sa.String(128), nullable=False),
        sa.Column("arguments_summary", sa.Text(), nullable=False),
        sa.Column("arguments_sha256", sa.String(64), nullable=False),
        sa.Column("side_effect", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("outcome_code", sa.String(128)),
        sa.Column("result_summary_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "profile",
            "channel",
            "chat_id",
            "event_id",
            "call_id",
            name="uq_tool_executions_call_binding",
        ),
        sa.CheckConstraint(
            "state IN ('requested','executing','succeeded','failed',"
            "'denied','approval_pending','unknown')",
            name="ck_tool_executions_state",
        ),
    )
    op.create_index(
        "ix_tool_executions_scope",
        "tool_executions",
        ["profile", "channel", "chat_id", "event_id"],
    )
    op.create_index(
        "ix_tool_executions_state",
        "tool_executions",
        ["profile", "state", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_tool_executions_state", table_name="tool_executions")
    op.drop_index("ix_tool_executions_scope", table_name="tool_executions")
    op.drop_table("tool_executions")
