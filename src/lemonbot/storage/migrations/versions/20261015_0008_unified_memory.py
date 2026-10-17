"""Append source-bearing memory, maintenance jobs and accounting; preserve raw data."""

from alembic import op

revision = "20261015_0008"
down_revision = "20261015_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE unified_delivery_proofs (
        outbox_id TEXT PRIMARY KEY REFERENCES outbox(message_id),
        payload TEXT NOT NULL, receipt TEXT NOT NULL)""")
    op.execute("""CREATE TABLE unified_memories (
        scope TEXT NOT NULL, memory_id TEXT NOT NULL, payload TEXT NOT NULL,
        evidence TEXT NOT NULL, PRIMARY KEY(scope,memory_id))""")
    op.execute("""CREATE TABLE unified_supersessions (
        scope TEXT NOT NULL, old_id TEXT NOT NULL, new_id TEXT NOT NULL,
        PRIMARY KEY(scope,old_id))""")
    op.execute("""CREATE TABLE unified_jobs (
        job_id TEXT PRIMARY KEY, scope TEXT NOT NULL, started_at TEXT NOT NULL,
        day TEXT NOT NULL, state TEXT NOT NULL, charged TEXT NOT NULL, sources TEXT NOT NULL)""")
    op.execute("CREATE INDEX unified_jobs_scope ON unified_jobs(scope,started_at)")
    op.execute("""CREATE TABLE unified_receipts (
        outbox_id TEXT PRIMARY KEY, scope TEXT NOT NULL, created_at TEXT NOT NULL)""")
    op.execute("""CREATE TABLE unified_content_cache (
        scope TEXT NOT NULL, cache_key TEXT NOT NULL, source TEXT NOT NULL,
        payload TEXT NOT NULL, PRIMARY KEY(scope,cache_key))""")
    op.execute("""CREATE TABLE unified_topics (
        scope TEXT NOT NULL, version TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(scope,version))""")
    op.execute("""CREATE TABLE unified_stage_usage (
        operation_id TEXT PRIMARY KEY, stage TEXT NOT NULL, created_at TEXT NOT NULL,
        elapsed_ms REAL NOT NULL, prompt_tokens INTEGER NOT NULL,
        completion_tokens INTEGER NOT NULL, cached_tokens INTEGER NOT NULL,
        estimated_cny TEXT NOT NULL, outcome TEXT NOT NULL)""")


def downgrade() -> None:
    raise RuntimeError("memory downgrade would delete provenance; use a code/config rollback")
