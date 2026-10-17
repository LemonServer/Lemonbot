"""Add the default-off unified message archive without rewriting legacy data."""

from alembic import op

revision = "20261015_0007"
down_revision = "20260825_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE context_archive_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_key TEXT NOT NULL,
            scope_json TEXT NOT NULL,
            message_id TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            header_json TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            outbox_message_id TEXT REFERENCES outbox(message_id),
            receipt_id TEXT,
            UNIQUE(scope_key, message_id),
            UNIQUE(outbox_message_id)
        )
    """)
    op.execute("""
        CREATE INDEX ix_context_archive_scope_time
        ON context_archive_messages(scope_key, occurred_at, message_id)
    """)
    op.execute("""
        CREATE TABLE context_archive_parts (
            message_pk INTEGER NOT NULL REFERENCES context_archive_messages(id),
            position INTEGER NOT NULL,
            part_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY(message_pk, part_id),
            UNIQUE(message_pk, position)
        )
    """)
    op.execute("""
        CREATE TABLE context_archive_references (
            message_pk INTEGER NOT NULL REFERENCES context_archive_messages(id),
            position INTEGER NOT NULL,
            target_message_id TEXT NOT NULL,
            target_part_id TEXT,
            payload_json TEXT NOT NULL,
            PRIMARY KEY(message_pk, position)
        )
    """)
    op.execute("""
        CREATE TABLE context_archive_observations (
            scope_key TEXT NOT NULL,
            observation_id TEXT NOT NULL,
            message_pk INTEGER NOT NULL REFERENCES context_archive_messages(id),
            part_id TEXT,
            payload_json TEXT NOT NULL,
            PRIMARY KEY(scope_key, observation_id)
        )
    """)
    op.execute("""
        CREATE INDEX ix_context_archive_observation_source
        ON context_archive_observations(message_pk, part_id)
    """)


def downgrade() -> None:
    # A code rollback must retain new archives, just like old UNKNOWN records.
    raise RuntimeError("archive downgrade would delete user data; use a code/config rollback")
