from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "wechat_metadata", Path(__file__).parents[2] / "scripts/agent_wechat_metadata.py"
)
assert _spec and _spec.loader
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)


def test_account_directory_alias_resolves_from_unique_self_sender() -> None:
    rows = [{"isSelf": True, "sender": "wxid_example"}, {"isSelf": False, "sender": "other"}]
    assert bridge.canonical_self(rows, "wxid_example_machine") == "wxid_example"
    assert bridge.mentioned_self(
        "<msgsource><atuserlist>wxid_example</atuserlist></msgsource>",
        bridge.canonical_self(rows, "wxid_example_machine"),
    )


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"isSelf": True, "sender": "wrong"}],
        [{"isSelf": True, "sender": "wxid_example"}, {"isSelf": True, "sender": "different"}],
        [{"isSelf": True, "sender": "wxid_examp"}],
    ],
)
def test_canonical_id_cannot_be_guessed_from_an_alias(rows: list[dict[str, object]]) -> None:
    with pytest.raises(ValueError):
        bridge.canonical_self(rows, "wxid_example_machine")


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("", False),
        ("<msgsource><atuserlist>self-account</atuserlist></msgsource>", True),
        ("<msgsource><atuserlist>other,self-account</atuserlist></msgsource>", True),
        ("<msgsource><atuserlist>self-account-extra</atuserlist></msgsource>", False),
        ("<msgsource><atuserlist>other</atuserlist></msgsource>", False),
        ("<msgsource><atuserlist>self-account,notify@all</atuserlist></msgsource>", False),
        ("<msgsource><quote><atuserlist>self-account</atuserlist></quote></msgsource>", False),
        ("<msgsource><text>@self-account</text></msgsource>", False),
    ],
)
def test_only_exact_top_level_mentions_trigger(source: str, expected: bool) -> None:
    assert bridge.mentioned_self(source, "self-account") is expected


@pytest.mark.parametrize(
    "source",
    [
        '<!DOCTYPE x [<!ENTITY a "self-account">]><msgsource/>',
        "<msgsource><atuserlist/><atuserlist/></msgsource>",
        "<unknown/>",
        "x" * 65537,
    ],
)
def test_invalid_source_fails_closed(source: str) -> None:
    with pytest.raises(ValueError):
        bridge.mentioned_self(source, "self-account")


def test_inotify_wakes_on_database_write_without_exposing_filename(tmp_path: Path) -> None:
    database = tmp_path / "message_0.db-wal"
    database.write_bytes(b"initial")
    watcher = bridge.ChangeNotifications([tmp_path])
    try:
        baseline = watcher.wait(None, 0, 0)
        assert baseline["ready"] and baseline["changed"]
        unchanged = watcher.wait(baseline["epoch"], baseline["sequence"], 0.01)
        assert not unchanged["changed"]
        database.write_bytes(b"synthetic change")
        changed = watcher.wait(baseline["epoch"], baseline["sequence"], 2)
        assert changed["changed"]
        assert changed["sequence"] > baseline["sequence"]
        assert set(changed) == {"ready", "changed", "sequence", "epoch"}
    finally:
        watcher.close()


def test_irrelevant_file_does_not_wake_scoped_database_watcher(tmp_path: Path) -> None:
    watcher = bridge.ChangeNotifications([tmp_path])
    try:
        baseline = watcher.wait(None, 0, 0)
        (tmp_path / "unrelated.log").write_text("not a message")
        assert not watcher.wait(baseline["epoch"], baseline["sequence"], 0.05)["changed"]
    finally:
        watcher.close()


def test_closed_watcher_cannot_claim_to_be_healthy(tmp_path: Path) -> None:
    watcher = bridge.ChangeNotifications([tmp_path])
    watcher.close()
    with pytest.raises(ValueError, match="Unavailable"):
        watcher.wait(None, 0, 0)


def test_source_lookup_is_cached_but_account_guard_is_not(monkeypatch: pytest.MonkeyPatch) -> None:
    queries: list[str] = []
    account = "owner_machine"
    reader = bridge.SourceReader("synthetic", {"account": account, "chat_ids": ["group@chatroom"]})

    def api(path: str) -> object:
        if path == "/api/status/auth":
            return {"status": "logged_in", "loggedInUser": account}
        return [{"isSelf": True, "sender": "owner"}]

    def query(_path: Path, _key: str, sql: str, **_kwargs: object) -> list[dict[str, object]]:
        queries.append(sql)
        if "FROM wechat_keys" in sql:
            return [
                {
                    "account_dir": "owner_machine_suffix",
                    "db_name": "message_0.db",
                    "hex_key": "a" * 64,
                }
            ]
        if "sqlite_master" in sql:
            return [{"name": "found"}]
        return [
            {
                "local_id": 7,
                "source": b"<msgsource><atuserlist>owner</atuserlist></msgsource>".hex(),
                "compressed": 0,
            }
        ]

    monkeypatch.setattr(reader, "api", api)
    monkeypatch.setattr(reader, "query", query)
    monkeypatch.setattr(Path, "is_dir", lambda _path: True)
    for _ in range(2):
        assert reader.mentions("group@chatroom", 0, [7])["rows"] == [
            {"localId": 7, "mentionedSelf": True}
        ]
    assert sum("FROM wechat_keys" in sql for sql in queries) == 1
    account = "different_account"
    with pytest.raises(ValueError, match="AccountChanged"):
        reader.mentions("group@chatroom", 0, [7])


def test_snapshot_messages_preserve_sender_and_strip_only_confirmed_group_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = bridge.SourceReader(
        "synthetic", {"account": "self_machine", "chat_ids": ["group@chatroom", "peer"]}
    )
    reader.canonical_identity = "self"
    monkeypatch.setattr(
        reader, "api", lambda _path: {"status": "logged_in", "loggedInUser": "self_machine"}
    )
    monkeypatch.setattr(
        reader, "source_table", lambda _chat, _account: (Path("db"), "key", "Msg_x")
    )
    monkeypatch.setattr(
        reader,
        "query",
        lambda *_args: [
            {
                "local_id": 12,
                "server_id": 100,
                "local_type": 0x100000001,
                "create_time": 1700000000,
                "sender": "peer",
                "content": 'peer:\n修复验收：请记住群聊短词“青柠”，用一句话确认。'.encode().hex(),
                "compressed": 0,
                "source": b"<msgsource><atuserlist>self</atuserlist></msgsource>".hex(),
                "source_compressed": 0,
            }
        ],
    )
    group = reader.messages("group@chatroom", 0)
    assert group["accountVerified"] is True
    assert group["rows"][0]["content"] == '修复验收：请记住群聊短词“青柠”，用一句话确认。'
    assert group["rows"][0]["sender"] == "peer"
    assert group["rows"][0]["isSelf"] is False
    assert group["rows"][0]["type"] == 1
    assert group["rows"][0]["mentionedSelf"] is True
    assert reader.messages("peer", 0)["rows"][0]["mentionedSelf"] is False
    assert reader.messages("peer", 0)["rows"][0]["content"].startswith("peer:\n")
    with pytest.raises(ValueError, match="TargetNotBound"):
        reader.messages("unrelated", 0)


def test_snapshot_messages_discard_results_if_account_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = bridge.SourceReader(
        "synthetic", {"account": "self_machine", "chat_ids": ["group@chatroom"]}
    )
    reader.canonical_identity = "self"
    responses = iter(
        [
            {"status": "logged_in", "loggedInUser": "self_machine"},
            {"status": "logged_in", "loggedInUser": "different"},
        ]
    )
    monkeypatch.setattr(reader, "api", lambda _path: next(responses))
    monkeypatch.setattr(
        reader, "source_table", lambda _chat, _account: (Path("db"), "key", "Msg_x")
    )
    monkeypatch.setattr(reader, "query", lambda *_args: [])
    with pytest.raises(ValueError, match="AccountChanged"):
        reader.messages("group@chatroom", 0)
