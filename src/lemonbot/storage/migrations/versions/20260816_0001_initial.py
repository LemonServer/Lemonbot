"""Initial durable core, memory and budget schema."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260816_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inbox",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("channel", sa.String(64), nullable=False),
        sa.Column("event_id", sa.String(256), nullable=False),
        sa.Column("chat_id", sa.String(512), nullable=False),
        sa.Column("sender_id", sa.String(512), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("text", sa.Text()),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("channel", "event_id", name="uq_inbox_channel_event"),
    )
    op.create_index("ix_inbox_claim", "inbox", ["state", "occurred_at", "id"])
    op.create_index(
        "ix_inbox_chat_order",
        "inbox",
        ["channel", "chat_id", "state", "occurred_at", "id"],
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("channel", sa.String(64), nullable=False),
        sa.Column("chat_id", sa.String(512), nullable=False),
        sa.Column("sender_id", sa.String(512)),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("external_id", sa.String(512)),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
    )
    op.create_index(
        "ix_messages_recent", "messages", ["channel", "chat_id", "occurred_at", "id"]
    )
    op.create_index("ix_messages_external", "messages", ["channel", "external_id"])
    op.create_table(
        "outbox",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("message_id", sa.String(36), nullable=False),
        sa.Column("channel", sa.String(64), nullable=False),
        sa.Column("chat_id", sa.String(512), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("reply_to_event_id", sa.String(256)),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("proactive", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True)),
        sa.Column("dispatch_started_at", sa.DateTime(timezone=True)),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("external_id", sa.String(512)),
        sa.Column("failure_detail", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("message_id", name="uq_outbox_message_id"),
        sa.UniqueConstraint("channel", "reply_to_event_id", name="uq_outbox_one_reply_per_event"),
    )
    op.create_index("ix_outbox_dispatch", "outbox", ["state", "created_at", "id"])
    op.create_index(
        "ix_outbox_rate", "outbox", ["channel", "chat_id", "created_at", "state"]
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("outcome", sa.String(128), nullable=False),
        sa.Column("channel", sa.String(64)),
        sa.Column("chat_id", sa.String(512)),
        sa.Column("event_id", sa.String(256)),
        sa.Column("message_id", sa.String(36)),
        sa.Column("rule_id", sa.String(128)),
        sa.Column("detail_json", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_time", "audit_log", ["occurred_at", "id"])
    op.create_table(
        "allowlist",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("channel", sa.String(64), nullable=False),
        sa.Column("chat_id", sa.String(512), nullable=False),
        sa.Column("enabled", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(512)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("channel", "chat_id", name="uq_allowlist_channel_chat"),
    )
    op.create_table(
        "runtime_state",
        sa.Column("key", sa.String(512), primary_key=True),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute(
        "CREATE VIRTUAL TABLE messages_fts USING fts5("
        "content, content='messages', content_rowid='id', tokenize='trigram')"
    )
    op.execute(
        "CREATE TRIGGER messages_fts_ai AFTER INSERT ON messages BEGIN "
        "INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content); END"
    )
    op.execute(
        "CREATE TRIGGER messages_fts_ad AFTER DELETE ON messages BEGIN "
        "INSERT INTO messages_fts(messages_fts, rowid, content) "
        "VALUES('delete', old.id, old.content); END"
    )
    op.execute(
        "CREATE TRIGGER messages_fts_au AFTER UPDATE OF content ON messages BEGIN "
        "INSERT INTO messages_fts(messages_fts, rowid, content) "
        "VALUES('delete', old.id, old.content); "
        "INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content); END"
    )
    op.create_table(
        "memory_records",
        sa.Column("memory_id", sa.Text(), primary_key=True),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("active", sa.Integer(), nullable=False),
        sa.Column("importance", sa.Float(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("record_json", sa.Text(), nullable=False),
        sa.CheckConstraint("active IN (0, 1)", name="ck_memory_active"),
    )
    op.create_index(
        "ix_memory_scope",
        "memory_records",
        ["channel", "chat_id", "active", "kind", "created_at"],
    )
    op.execute(
        "CREATE VIRTUAL TABLE memory_fts USING fts5("
        "text, content='memory_records', content_rowid='rowid', tokenize='trigram')"
    )
    op.execute(
        "CREATE TRIGGER memory_records_ai AFTER INSERT ON memory_records BEGIN "
        "INSERT INTO memory_fts(rowid, text) VALUES (new.rowid, new.text); END"
    )
    op.execute(
        "CREATE TRIGGER memory_records_ad AFTER DELETE ON memory_records BEGIN "
        "INSERT INTO memory_fts(memory_fts, rowid, text) "
        "VALUES('delete', old.rowid, old.text); END"
    )
    op.execute(
        "CREATE TRIGGER memory_records_au AFTER UPDATE OF text ON memory_records BEGIN "
        "INSERT INTO memory_fts(memory_fts, rowid, text) "
        "VALUES('delete', old.rowid, old.text); "
        "INSERT INTO memory_fts(rowid, text) VALUES (new.rowid, new.text); END"
    )
    op.create_table(
        "model_budget_ledger",
        sa.Column("reservation_id", sa.Text(), primary_key=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("day_key", sa.Text(), nullable=False),
        sa.Column("month_key", sa.Text(), nullable=False),
        sa.Column("reserved_cny", sa.Text(), nullable=False),
        sa.Column("charged_cny", sa.Text()),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("settled_at", sa.Text()),
        sa.CheckConstraint(
            "state IN ('reserved','released','settled','unknown')",
            name="ck_model_budget_state",
        ),
    )
    op.create_index(
        "ix_model_budget_day", "model_budget_ledger", ["day_key", "state"]
    )
    op.create_index(
        "ix_model_budget_month", "model_budget_ledger", ["month_key", "state"]
    )
    op.create_table(
        "proactive_jobs",
        sa.Column("job_id", sa.Text(), primary_key=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("reason_event_id", sa.Text(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("due_at", sa.Text(), nullable=False),
        sa.Column("recurrence_seconds", sa.Integer()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.CheckConstraint(
            "source IN ('admin_schedule','user_subscription','stored_commitment')",
            name="ck_proactive_source",
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','completed','dead','cancelled')",
            name="ck_proactive_status",
        ),
    )
    op.create_index("ix_proactive_due", "proactive_jobs", ["status", "due_at"])
    op.create_table(
        "attachments",
        sa.Column("attachment_id", sa.Text(), primary_key=True),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("original_name", sa.Text()),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("channel", "event_id", "sha256", name="uq_attachment_event_hash"),
        sa.CheckConstraint(
            "status IN ('quarantined','sanitized','rejected')",
            name="ck_attachment_status",
        ),
    )
    op.create_index(
        "ix_attachment_scope",
        "attachments",
        ["channel", "chat_id", "event_id", "attachment_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_attachment_scope", table_name="attachments")
    op.drop_table("attachments")
    op.drop_index("ix_proactive_due", table_name="proactive_jobs")
    op.drop_table("proactive_jobs")
    op.drop_index("ix_model_budget_month", table_name="model_budget_ledger")
    op.drop_index("ix_model_budget_day", table_name="model_budget_ledger")
    op.drop_table("model_budget_ledger")
    op.execute("DROP TRIGGER IF EXISTS memory_records_au")
    op.execute("DROP TRIGGER IF EXISTS memory_records_ad")
    op.execute("DROP TRIGGER IF EXISTS memory_records_ai")
    op.execute("DROP TABLE IF EXISTS memory_fts")
    op.drop_index("ix_memory_scope", table_name="memory_records")
    op.drop_table("memory_records")
    op.execute("DROP TRIGGER IF EXISTS messages_fts_au")
    op.execute("DROP TRIGGER IF EXISTS messages_fts_ad")
    op.execute("DROP TRIGGER IF EXISTS messages_fts_ai")
    op.execute("DROP TABLE IF EXISTS messages_fts")
    op.drop_table("runtime_state")
    op.drop_table("allowlist")
    op.drop_index("ix_audit_time", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_outbox_rate", table_name="outbox")
    op.drop_index("ix_outbox_dispatch", table_name="outbox")
    op.drop_table("outbox")
    op.drop_index("ix_messages_external", table_name="messages")
    op.drop_index("ix_messages_recent", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_inbox_chat_order", table_name="inbox")
    op.drop_index("ix_inbox_claim", table_name="inbox")
    op.drop_table("inbox")
