"""Read-only unified-context health/status/provenance. No services, credentials or cloud."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lemonbot.config.paths import RuntimePaths
from lemonbot.config.settings import AppSettings, load_settings
from lemonbot.embeddings.artifacts import EmbeddingError, verify_artifacts
from lemonbot.embeddings.config import embedding_paths

from .archive import ArchiveError, SQLiteMessageArchive
from .maintenance import UnifiedMemoryStore
from .models import ConversationScope, Observation, SourceRef
from .stats_cli import statistics

HEAD = "20261015_0008"
TABLES = (
    "context_archive_messages",
    "context_archive_parts",
    "context_archive_references",
    "context_archive_observations",
    "unified_memories",
    "unified_supersessions",
    "unified_jobs",
    "unified_receipts",
    "unified_content_cache",
    "unified_topics",
    "unified_delivery_proofs",
    "unified_stage_usage",
)


def database_status(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        integrity = db.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        foreign_keys = not db.execute("PRAGMA foreign_key_check").fetchall()
        existing = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        versions = (
            [r[0] for r in db.execute("SELECT version_num FROM alembic_version")]
            if ("alembic_version" in existing)
            else []
        )
        ready = versions == [HEAD] and set(TABLES) <= existing
        result: dict[str, Any] = {
            "ok": integrity and foreign_keys and ready,
            "schema_ready": ready,
            "integrity": integrity,
            "foreign_keys": foreign_keys,
            "counts": {
                t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]  # noqa: S608
                for t in TABLES
                if t in existing
            },
            "pause": {"global": False, "paused_channels": 0},
        }
        if ready:
            result["capacity"] = {
                "memory_scopes_over_1000": db.execute(
                    "SELECT count(*) FROM (SELECT scope FROM unified_memories "
                    "GROUP BY scope HAVING count(*)>1000)"
                ).fetchone()[0],
                "largest_archive_scope": db.execute(
                    "SELECT coalesce(max(n),0) FROM (SELECT count(*) n "
                    "FROM context_archive_messages GROUP BY scope_key)"
                ).fetchone()[0],
            }
        if "runtime_state" in existing:
            for key, raw in db.execute(
                "SELECT key,value_json FROM runtime_state WHERE key LIKE 'pause:%'"
            ):
                if json.loads(raw).get("paused"):
                    if key == "pause:global":
                        result["pause"]["global"] = True
                    elif key.startswith("pause:channel:"):
                        result["pause"]["paused_channels"] += 1
        for table in ("inbox", "outbox"):
            if table in existing:
                result[table] = dict(
                    db.execute(f"SELECT state,count(*) FROM {table} GROUP BY state")  # noqa: S608
                )
        if "unified_delivery_proofs" in existing:
            result["ack_maintenance_pending"] = db.execute(
                "SELECT count(*) FROM unified_delivery_proofs p JOIN outbox o "
                "ON o.message_id=p.outbox_id WHERE o.state='acknowledged' AND NOT EXISTS "
                "(SELECT 1 FROM unified_receipts r WHERE r.outbox_id=p.outbox_id)"
            ).fetchone()[0]
    if ready:
        result["usage"] = statistics(path)
    result["mode"] = "read_only_not_live_acceptance"
    return result


def health(
    settings: AppSettings, paths: RuntimePaths, *, database: Path | None = None
) -> dict[str, Any]:
    options = settings.context_memory
    result: dict[str, Any] = {
        "mode": "offline_configuration_not_live_acceptance",
        "enabled": options.enabled,
        "archive_enabled": options.archive_enabled,
        "consolidation_enabled": options.consolidation_enabled,
        "model": settings.models.flash_model,
        "limits": {
            "input_tokens": settings.models.max_input_tokens,
            "output_tokens": settings.models.max_output_tokens,
            "classifier_calls_per_turn": 1,
        },
        "embedding": {
            "configured_backend": options.embedding_backend,
            "precision": options.embedding_precision,
            "device_probe": "not_run",
        },
        "budget": {
            "daily_cny": str(settings.models.budget.daily_limit_cny),
            "monthly_cny": str(settings.models.budget.monthly_limit_cny),
            "consolidation_daily_cny": str(options.consolidation_daily_cny),
        },
    }
    checks = [{"name": "configuration", "ok": True, "required": True}]
    if options.enabled:
        root, python = embedding_paths(settings)
        valid = True
        try:
            verify_artifacts(root / "model")
        except (EmbeddingError, OSError):
            valid = False
        checks.append({"name": "embedding_artifacts", "ok": valid, "required": True})
        checks.append(
            {
                "name": "embedding_worker",
                "ok": python.is_file() and os.access(python, os.X_OK),
                "required": True,
            }
        )
    if database is not None or options.archive_enabled:
        try:
            result["database"] = database_status(database or paths.database)
            valid = result["database"]["ok"]
            capacity = result["database"].get("capacity", {})
            valid = (
                valid
                and not capacity.get("memory_scopes_over_1000", 0)
                and (capacity.get("largest_archive_scope", 0) <= options.retrieval_max_messages)
            )
        except (OSError, sqlite3.Error, ValueError):
            valid = False
            result["database"] = {"ok": False, "reason": "DatabaseUnavailableOrSchemaInvalid"}
        checks.append({"name": "archive_schema", "ok": valid, "required": True})
    result["checks"] = checks
    result["ok"] = all(c["ok"] for c in checks if c["required"])
    return result


async def source_status(
    path: Path,
    scope: ConversationScope,
    *,
    message_id: str | None = None,
    memory_id: str | None = None,
) -> dict[str, Any]:
    if not database_status(path)["ok"]:
        raise ArchiveError("ContextSchemaNotReady")
    if (message_id is None) == (memory_id is None):
        raise ArchiveError("ExactlyOneSourceRequired")
    archive = SQLiteMessageArchive(path)
    store = UnifiedMemoryStore(archive)
    if memory_id:
        memory = await store.get_memory(scope=scope, memory_id=memory_id)
        if memory is None:
            raise ArchiveError("ContextSourceNotFound")
        sources = memory.sources
        result: dict[str, Any] = {
            "kind": "memory",
            "active": memory.active,
            "source_count": len(sources),
            "author_count": len(memory.author_ids),
        }
    else:
        assert message_id is not None
        source = SourceRef(scope=scope, message_id=message_id)
        message = await archive.get_message(source)
        if message is None:
            raise ArchiveError("ContextSourceNotFound")
        receipt_verified = None
        if message.direction == "outbound":
            async with archive._read() as db:
                from .archive import scope_key

                row = await (
                    await db.execute(
                        "SELECT receipt_id FROM context_archive_messages "
                        "WHERE scope_key=? AND message_id=?",
                        (scope_key(scope), message.message_id),
                    )
                ).fetchone()
                if row is None or await archive._outbox(db, message, row[0]) is None:
                    raise ArchiveError("ContextReceiptMissing")
                receipt_verified = True
        sources = (source,)
        result = {
            "kind": "message",
            "eligible": message.eligible_as_evidence,
            "receipt_verified": receipt_verified,
            "parts": [p.kind for p in message.parts],
            "reference_count": len(message.references),
            "attachment_metadata": {
                "count": sum(p.attachment is not None for p in message.parts),
                "expired": sum(
                    p.attachment is not None and p.attachment.expires_at <= datetime.now(UTC)
                    for p in message.parts
                ),
                "original_bytes_probe": "not_run",
            },
        }
    observations: list[Observation] = []
    for source in sources:
        observations.extend(await archive.get_observations(source))
    now = datetime.now(UTC)
    result["observations"] = {
        "total": len(observations),
        "available": sum(
            o.status == "available"
            and o.created_at <= now
            and (o.expires_at is None or o.expires_at > now)
            for o in observations
        ),
    }
    try:
        notices = await store.correction_notices(sources)
        result["corrections"] = {
            "count": len(notices),
            "latest_available": sum(n.latest is not None for n in notices),
        }
    except ValueError:
        result["corrections"] = {"reason": "CorrectionChainUnavailable"}
    result["mode"] = "read_only_no_content"
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--config", type=Path)
    doctor.add_argument("--database", type=Path)
    for name in ("status", "source"):
        command = commands.add_parser(name)
        command.add_argument("--database", type=Path, required=True)
        if name == "source":
            for identifier in ("channel", "account", "conversation"):
                command.add_argument("--" + identifier, required=True)
            command.add_argument("--kind", choices=("private", "group"), required=True)
            exclusive = command.add_mutually_exclusive_group(required=True)
            exclusive.add_argument("--message-id")
            exclusive.add_argument("--memory-id")
    args = parser.parse_args()
    try:
        if args.command == "doctor":
            settings = load_settings(args.config)
            result = health(settings, RuntimePaths.from_settings(settings), database=args.database)
        elif args.command == "status":
            result = database_status(args.database)
        else:
            scope = ConversationScope(
                channel=args.channel,
                account_id=args.account,
                conversation_id=args.conversation,
                kind=args.kind,
            )
            result = asyncio.run(
                source_status(
                    args.database, scope, message_id=args.message_id, memory_id=args.memory_id
                )
            )
    except (OSError, sqlite3.Error, ValueError):
        parser.exit(1, "Unified context operation refused; verify source, schema and config.\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if not result.get("ok", True):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
