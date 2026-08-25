from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest
from typer.testing import CliRunner

from lemonbot.cli import app
from lemonbot.config.paths import RuntimePaths
from lemonbot.data import DataOperationError, delete_conversation, export_profile_data
from lemonbot.runtime_lock import AlreadyRunningError, RuntimeLock
from lemonbot.storage.migrate import upgrade_database

_SCOPED_TABLES = (
    "inbox",
    "outbox",
    "drafts",
    "approvals",
    "tool_executions",
    "messages",
    "memory_records",
    "attachments",
    "proactive_jobs",
    "allowlist",
    "audit_log",
)


def _insert_conversation(
    connection: sqlite3.Connection,
    *,
    channel: str,
    chat_id: str,
    suffix: str,
) -> None:
    now = datetime.now(UTC).isoformat()
    event_id = f"event-{suffix}"
    connection.execute(
        """
        INSERT INTO inbox(
            channel,event_id,chat_id,sender_id,kind,text,occurred_at,metadata_json,
            state,attempts,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            channel,
            event_id,
            chat_id,
            f"sender-{suffix}",
            "text",
            f"inbox-{suffix}",
            now,
            json.dumps({"scope": chat_id}),
            "done",
            1,
            now,
            now,
        ),
    )
    connection.execute(
        """
        INSERT INTO outbox(
            message_id,channel,chat_id,text,reply_to_event_id,metadata_json,
            proactive,state,attempts,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            f"00000000-0000-0000-0000-{int(suffix):012d}",
            channel,
            chat_id,
            f"outbox-{suffix}",
            event_id,
            "{}",
            0,
            "acknowledged",
            1,
            now,
            now,
        ),
    )
    connection.execute(
        """
        INSERT INTO drafts(
            draft_id,channel,chat_id,text,reply_to_event_id,metadata_json,
            state,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            f"10000000-0000-0000-0000-{int(suffix):012d}",
            channel,
            chat_id,
            f"draft-{suffix}",
            f"draft-event-{suffix}",
            "{}",
            "pending",
            now,
            now,
        ),
    )
    connection.execute(
        """
        INSERT INTO messages(
            channel,chat_id,sender_id,role,kind,content,external_id,
            occurred_at,metadata_json
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            channel,
            chat_id,
            f"sender-{suffix}",
            "user",
            "text",
            f"message-{suffix}",
            event_id,
            now,
            "{}",
        ),
    )
    connection.execute(
        """
        INSERT INTO memory_records(
            memory_id,channel,chat_id,kind,text,active,importance,created_at,record_json
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            f"memory-{suffix}",
            channel,
            chat_id,
            "fact",
            f"memory-text-{suffix}",
            1,
            0.8,
            now,
            json.dumps({"channel": channel, "chat_id": chat_id}),
        ),
    )
    connection.execute(
        """
        INSERT INTO proactive_jobs(
            job_id,source,channel,chat_id,reason_event_id,prompt,due_at,
            recurrence_seconds,status,attempts,created_at,updated_at,last_error
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            f"20000000-0000-0000-0000-{int(suffix):012d}",
            "admin_schedule",
            channel,
            chat_id,
            event_id,
            f"prompt-{suffix}",
            now,
            None,
            "pending",
            0,
            now,
            now,
            None,
        ),
    )
    connection.execute(
        """
        INSERT INTO allowlist(channel,chat_id,enabled,label,created_at,updated_at)
        VALUES(?,?,?,?,?,?)
        """,
        (channel, chat_id, 1, f"label-{suffix}", now, now),
    )
    connection.execute(
        """
        INSERT INTO audit_log(
            action,outcome,channel,chat_id,event_id,message_id,rule_id,
            detail_json,occurred_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            "conversation.test",
            "recorded",
            channel,
            chat_id,
            event_id,
            None,
            None,
            json.dumps({"scope": chat_id}),
            now,
        ),
    )


def _insert_attachment(
    connection: sqlite3.Connection,
    *,
    channel: str,
    chat_id: str,
    suffix: str,
    digest: str,
    size: int,
) -> None:
    connection.execute(
        """
        INSERT INTO attachments(
            attachment_id,channel,chat_id,event_id,sha256,media_type,
            original_name,size,status,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            f"30000000-0000-0000-0000-{int(suffix):012d}",
            channel,
            chat_id,
            f"attachment-event-{suffix}",
            digest,
            "text/plain",
            f"attachment-{suffix}.txt",
            size,
            "quarantined",
            datetime.now(UTC).isoformat(),
        ),
    )


def _write_object(paths: RuntimePaths, content: bytes) -> tuple[str, Path]:
    digest = hashlib.sha256(content).hexdigest()
    location = paths.objects / digest[:2] / digest
    location.parent.mkdir(parents=True, exist_ok=True)
    location.write_bytes(content)
    return digest, location


def _insert_approval(
    connection: sqlite3.Connection,
    *,
    profile: str,
    channel: str,
    chat_id: str,
    suffix: str,
) -> None:
    now = datetime.now(UTC).isoformat()
    arguments = json.dumps({"private": f"parameter-{suffix}"}, separators=(",", ":"))
    digest = hashlib.sha256(arguments.encode()).hexdigest()
    connection.execute(
        """
        INSERT INTO approvals(
            approval_id,profile,channel,chat_id,event_id,tool_name,action_kind,
            arguments_summary,arguments_sha256,arguments_json,state,claim_token,
            outcome_code,created_at,expires_at,claimed_at,resolved_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            f"40000000-0000-0000-0000-{int(suffix):012d}",
            profile,
            channel,
            chat_id,
            f"approval-event-{suffix}",
            "vault.create",
            "write_file",
            "private=<string>",
            digest,
            arguments,
            "pending",
            None,
            None,
            now,
            (datetime.now(UTC).replace(year=2027)).isoformat(),
            None,
            None,
            now,
        ),
    )


def test_data_export_is_consistent_profile_scoped_backup_format(tmp_path: Path) -> None:
    paths = RuntimePaths(root=tmp_path / "runtime", profile="lab")
    upgrade_database(paths.database)
    paths.ensure()
    content = b"raw attachment bytes"
    digest, _object_path = _write_object(paths, content)
    with closing(sqlite3.connect(paths.database)) as connection:
        _insert_conversation(
            connection,
            channel="wechat_personal_lab",
            chat_id="chat-1",
            suffix="1",
        )
        _insert_attachment(
            connection,
            channel="wechat_personal_lab",
            chat_id="chat-1",
            suffix="1",
            digest=digest,
            size=len(content),
        )
        connection.commit()
    (paths.root / "config-with-secret.toml").write_text(
        "local_config_marker='not-archived'",
        "utf-8",
    )

    archive_path = export_profile_data(paths, tmp_path / "profile-export.zip")

    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        assert names == {
            "manifest.json",
            "database/lab.db",
            f"objects/{digest[:2]}/{digest}",
        }
        assert all(
            not PurePosixPath(name).is_absolute() and ".." not in PurePosixPath(name).parts
            for name in names
        )
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["format"] == 1
        assert manifest["profile"] == "lab"
        assert archive.read(f"objects/{digest[:2]}/{digest}") == content
        exported_database = tmp_path / "exported.db"
        exported_database.write_bytes(archive.read("database/lab.db"))
    with closing(sqlite3.connect(exported_database)) as connection:
        assert connection.execute("SELECT content FROM messages").fetchone() == ("message-1",)
        assert connection.execute("SELECT action FROM audit_log").fetchone() == (
            "conversation.test",
        )
        assert connection.execute("SELECT record_json FROM memory_records").fetchone() is not None
    assert "config-with-secret.toml" not in names


def test_data_operations_require_the_runtime_lock(tmp_path: Path) -> None:
    paths = RuntimePaths(root=tmp_path / "runtime", profile="lab")
    upgrade_database(paths.database)
    with closing(sqlite3.connect(paths.database)) as connection:
        _insert_conversation(connection, channel="test", chat_id="chat", suffix="1")
        connection.commit()

    with RuntimeLock(paths.lock_file):
        with pytest.raises(AlreadyRunningError):
            export_profile_data(paths, tmp_path / "blocked.zip")
        with pytest.raises(AlreadyRunningError):
            delete_conversation(paths, channel="test", chat_id="chat")

    with closing(sqlite3.connect(paths.database)) as connection:
        assert connection.execute("SELECT count(*) FROM messages").fetchone() == (1,)


def test_delete_conversation_scrubs_scope_and_only_unreferenced_objects(
    tmp_path: Path,
) -> None:
    paths = RuntimePaths(root=tmp_path / "runtime", profile="prod")
    upgrade_database(paths.database)
    paths.ensure()
    target_only = b"target-only-attachment-secret"
    shared = b"shared-attachment"
    target_digest, target_path = _write_object(paths, target_only)
    shared_digest, shared_path = _write_object(paths, shared)
    with closing(sqlite3.connect(paths.database)) as connection:
        _insert_conversation(connection, channel="wecom", chat_id="target-chat", suffix="1")
        _insert_conversation(connection, channel="wecom", chat_id="other-chat", suffix="2")
        _insert_attachment(
            connection,
            channel="wecom",
            chat_id="target-chat",
            suffix="1",
            digest=target_digest,
            size=len(target_only),
        )
        _insert_attachment(
            connection,
            channel="wecom",
            chat_id="target-chat",
            suffix="3",
            digest=shared_digest,
            size=len(shared),
        )
        _insert_attachment(
            connection,
            channel="wecom",
            chat_id="other-chat",
            suffix="2",
            digest=shared_digest,
            size=len(shared),
        )
        connection.commit()

    result = delete_conversation(paths, channel="wecom", chat_id="target-chat")

    assert result.total_rows == 10
    assert result.objects_removed == 1
    assert result.object_cleanup_failures == 0
    assert not target_path.exists()
    assert shared_path.read_bytes() == shared
    with closing(sqlite3.connect(paths.database)) as connection:
        connection.row_factory = sqlite3.Row
        for table in _SCOPED_TABLES:
            target = connection.execute(
                f'SELECT count(*) FROM "{table}" WHERE channel=? AND chat_id=?',  # noqa: S608
                ("wecom", "target-chat"),
            ).fetchone()
            assert target is not None and target[0] == 0
        assert (
            connection.execute(
                "SELECT count(*) FROM messages WHERE chat_id='other-chat'"
            ).fetchone()[0]
            == 1
        )
        summary = connection.execute(
            """
            SELECT outcome, chat_id, detail_json FROM audit_log
            WHERE action='data.delete_conversation'
            """
        ).fetchone()
        assert summary is not None
        assert summary["outcome"] == "completed"
        assert summary["chat_id"] is None
        assert "target-chat" not in summary["detail_json"]
        assert (
            connection.execute(
                "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'message'"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM memory_fts WHERE memory_fts MATCH 'memory'"
            ).fetchone()[0]
            == 1
        )
    assert b"target-chat" not in paths.database.read_bytes()
    wal = paths.database.with_name(paths.database.name + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0


def test_delete_refuses_missing_scope_and_unsafe_export_location(tmp_path: Path) -> None:
    paths = RuntimePaths(root=tmp_path / "runtime", profile="lab")
    upgrade_database(paths.database)
    paths.ensure()

    with pytest.raises(DataOperationError, match="object store"):
        export_profile_data(paths, paths.objects / "unsafe.zip")
    (paths.objects / "credential.txt").write_text("must-not-export", "utf-8")
    with pytest.raises(DataOperationError, match="unexpected entry"):
        export_profile_data(paths, tmp_path / "unsafe-content.zip")
    with pytest.raises(DataOperationError, match="no persisted records"):
        delete_conversation(paths, channel="wechat_personal_lab", chat_id="missing")


def test_delete_cli_requires_confirmation_and_does_not_echo_chat_id(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    paths = RuntimePaths(root=root, profile="lab")
    upgrade_database(paths.database)
    with closing(sqlite3.connect(paths.database)) as connection:
        _insert_conversation(connection, channel="test", chat_id="cli-private-chat", suffix="1")
        connection.commit()
    config = tmp_path / "lab.toml"
    config.write_text(
        "\n".join(
            (
                "schema_version = 1",
                'profile = "lab"',
                "[runtime]",
                'connector = "fake"',
                f'data_root = "{root.as_posix()}"',
            )
        ),
        "utf-8",
    )
    runner = CliRunner()

    refused = runner.invoke(
        app,
        ["data", "delete-conversation", "test", "cli-private-chat", "--config", str(config)],
    )
    assert refused.exit_code == 2
    with closing(sqlite3.connect(paths.database)) as connection:
        assert connection.execute("SELECT count(*) FROM messages").fetchone() == (1,)

    deleted = runner.invoke(
        app,
        [
            "data",
            "delete-conversation",
            "test",
            "cli-private-chat",
            "--config",
            str(config),
            "--confirm",
        ],
    )
    assert deleted.exit_code == 0, deleted.output
    assert "cli-private-chat" not in deleted.output
    assert '"rows_deleted": 8' in deleted.output


def test_delete_fails_closed_for_unknown_conversation_scoped_table(tmp_path: Path) -> None:
    paths = RuntimePaths(root=tmp_path / "runtime", profile="lab")
    upgrade_database(paths.database)
    with closing(sqlite3.connect(paths.database)) as connection:
        _insert_conversation(connection, channel="test", chat_id="future-chat", suffix="1")
        connection.execute(
            "CREATE TABLE future_conversation_data(channel TEXT, chat_id TEXT, value TEXT)"
        )
        connection.execute(
            "INSERT INTO future_conversation_data VALUES('test','future-chat','must-remain')"
        )
        connection.commit()

    with pytest.raises(DataOperationError, match="future_conversation_data"):
        delete_conversation(paths, channel="test", chat_id="future-chat")

    with closing(sqlite3.connect(paths.database)) as connection:
        assert connection.execute("SELECT count(*) FROM messages").fetchone() == (1,)
        assert connection.execute("SELECT value FROM future_conversation_data").fetchone() == (
            "must-remain",
        )


def test_approvals_are_exported_and_known_to_conversation_deletion(tmp_path: Path) -> None:
    paths = RuntimePaths(root=tmp_path / "runtime", profile="lab")
    upgrade_database(paths.database)
    paths.ensure()
    with closing(sqlite3.connect(paths.database)) as connection:
        _insert_approval(
            connection,
            profile="lab",
            channel="wechat_personal_lab",
            chat_id="target-chat",
            suffix="1",
        )
        _insert_approval(
            connection,
            profile="lab",
            channel="wechat_personal_lab",
            chat_id="other-chat",
            suffix="2",
        )
        connection.commit()

    archive_path = export_profile_data(paths, tmp_path / "approvals-export.zip")
    with zipfile.ZipFile(archive_path) as archive:
        exported_database = tmp_path / "approvals-export.db"
        exported_database.write_bytes(archive.read("database/lab.db"))
    with closing(sqlite3.connect(exported_database)) as connection:
        assert connection.execute(
            "SELECT json_extract(arguments_json, '$.private') FROM approvals "
            "WHERE chat_id='target-chat'"
        ).fetchone() == ("parameter-1",)

    result = delete_conversation(
        paths,
        channel="wechat_personal_lab",
        chat_id="target-chat",
    )

    assert result.rows_deleted["approvals"] == 1
    with closing(sqlite3.connect(paths.database)) as connection:
        assert connection.execute("SELECT chat_id FROM approvals ORDER BY chat_id").fetchall() == [
            ("other-chat",)
        ]
