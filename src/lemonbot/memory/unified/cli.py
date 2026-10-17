"""Scoped inspection, backup-copy import and guarded stage-8 offline migration."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from lemonbot.backup import BackupError, create_backup
from lemonbot.config.paths import RuntimePaths
from lemonbot.config.settings import load_settings
from lemonbot.connectors.wechat_agent import CHANNEL, LabPaused
from lemonbot.connectors.wechat_agent_conversation import load_targets
from lemonbot.connectors.wechat_agent_state import load_binding, private_read
from lemonbot.runtime_lock import AlreadyRunningError, RuntimeLock
from lemonbot.storage.migrate import upgrade_database

from .archive import ArchiveError, SQLiteMessageArchive
from .migration import migrate_legacy_copy
from .models import ConversationScope, Message, SourceRef


def validate_copy(database: Path, paths: RuntimePaths) -> Path:
    """Stage 2 never migrates a configured runtime DB or hard-linked alias."""
    path = database.expanduser().resolve(strict=True)
    if not path.is_file() or path.stat().st_nlink != 1:
        raise ArchiveError("MigrationRequiresIndependentCopy")
    if path == paths.database.resolve() or (
        paths.database.exists() and path.samefile(paths.database)
    ):
        raise ArchiveError("LiveDatabaseMigrationForbiddenInStage2")
    if not path.is_relative_to(paths.backups.resolve()):
        raise ArchiveError("MigrationCopyMustBeInProfileBackups")
    return path


async def migrate_offline(args: argparse.Namespace) -> dict[str, object]:
    """Prepared in stage 7; execute on a stopped profile only during stage 8."""
    if not args.confirm_stopped:
        raise ArchiveError("ExplicitStoppedConfirmationRequired")
    settings = load_settings(args.config)
    if settings.context_memory.enabled or settings.context_memory.archive_enabled:
        raise ArchiveError("DisableUnifiedFlowBeforeMigration")
    paths = RuntimePaths.from_settings(settings)
    path = paths.database
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ArchiveError("OfflineMigrationRequiresOwnedRegularDatabase")
    if path.stat().st_uid != os.getuid():
        raise ArchiveError("OfflineMigrationRequiresOwnedRegularDatabase")
    state = Path(settings.wechat_agent.state_dir).expanduser()
    key = private_read(state / "comparison-key")
    binding = load_binding(state / "binding.json")
    targets = load_targets(state / "conversation-binding.json", binding.account_ref, key)
    with RuntimeLock(paths.lock_file):
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            paused = any(
                json.loads(r[0]).get("paused")
                for r in db.execute(
                    "SELECT value_json FROM runtime_state WHERE key IN (?,?)",
                    ("pause:global", "pause:channel:" + CHANNEL),
                )
            )
            busy = db.execute(
                "SELECT (SELECT count(*) FROM inbox WHERE state IN ('pending','processing')) + "
                "(SELECT count(*) FROM outbox WHERE state IN ('pending','reserved','dispatching'))"
            ).fetchone()[0]
            if not paused or busy:
                raise ArchiveError("OfflineMigrationRequiresPausedDrainedProfile")
        path.chmod(0o600)
        backup = await asyncio.to_thread(
            create_backup, paths, paths.backups / ("context-before-" + uuid4().hex + ".zip")
        )
        await asyncio.to_thread(upgrade_database, path)
        report = await migrate_legacy_copy(SQLiteMessageArchive(path), targets=targets, key=key)
        return {"mode": "paused_offline_migration", "backup": str(backup), **report.counts()}


async def execute(args: argparse.Namespace) -> dict[str, object]:
    if args.command == "migrate-offline":
        return await migrate_offline(args)
    if args.command == "migrate-copy":
        if not args.confirm_copy:
            raise ArchiveError("ExplicitBackupCopyConfirmationRequired")
        settings = load_settings(args.config)
        path = validate_copy(args.database, RuntimePaths.from_settings(settings))
        state = Path(settings.wechat_agent.state_dir).expanduser()
        key = private_read(state / "comparison-key")
        binding = load_binding(state / "binding.json")
        targets = load_targets(state / "conversation-binding.json", binding.account_ref, key)
        path.chmod(0o600)
        await asyncio.to_thread(upgrade_database, path)
        report = await migrate_legacy_copy(SQLiteMessageArchive(path), targets=targets, key=key)
        return {"mode": "offline_backup_copy", **report.counts()}
    scope = ConversationScope(
        channel=args.channel,
        account_id=args.account,
        conversation_id=args.conversation,
        kind=args.kind,
    )
    archive = SQLiteMessageArchive(args.database)
    messages: tuple[Message, ...]
    if args.message_id:
        found = await archive.get_message(SourceRef(scope=scope, message_id=args.message_id))
        messages = () if found is None else (found,)
    else:
        messages = await archive.list_messages(
            scope=scope, before_message_id=args.before, limit=args.limit
        )
    return {
        "mode": "read_only",
        "count": len(messages),
        "messages": [
            message.model_dump(mode="json")
            if args.include_content
            else {
                "message_id": message.message_id,
                "direction": message.direction,
                "occurred_at": message.occurred_at.isoformat(),
                "parts": [part.kind for part in message.parts],
                "reference_count": len(message.references),
            }
            for message in messages
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser(
        "inspect", help="Read archive; no schema changes or external calls"
    )
    inspect.add_argument("--database", type=Path, required=True)
    for name in ("channel", "account", "conversation"):
        inspect.add_argument("--" + name, required=True)
    inspect.add_argument("--kind", choices=("private", "group"), required=True)
    inspect.add_argument("--message-id")
    inspect.add_argument("--before")
    inspect.add_argument("--limit", type=int, default=20)
    inspect.add_argument("--include-content", action="store_true")
    migrate = commands.add_parser("migrate-copy", help="Import only an independent profile backup")
    migrate.add_argument("--database", type=Path, required=True)
    migrate.add_argument("--config", type=Path)
    migrate.add_argument("--confirm-copy", action="store_true")
    offline = commands.add_parser(
        "migrate-offline",
        help="Stage-8 only: backup and migrate a stopped, paused, drained profile",
    )
    offline.add_argument("--config", type=Path)
    offline.add_argument("--confirm-stopped", action="store_true")
    args = parser.parse_args()
    try:
        result = asyncio.run(execute(args))
    except ArchiveError as error:
        parser.exit(1, str(error) + "\n")
    except (sqlite3.Error, OSError, ValueError, LabPaused, AlreadyRunningError, BackupError):
        # Parser/config/DB exceptions can contain private paths, IDs or input.
        parser.exit(1, "Archive operation refused; check config, binding and schema.\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
