from __future__ import annotations

import importlib.util
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "cycle_bridge", Path(__file__).parents[2] / "scripts/agent_wechat_metadata.py"
)
assert _spec and _spec.loader
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)


@pytest.fixture
def reader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "messages.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE Name2Id(user_name TEXT)")
    db.execute("INSERT INTO Name2Id VALUES ('peer')")
    for table in ("Msg_group", "Msg_private"):
        db.execute(
            f"CREATE TABLE {table}(local_id INTEGER,server_id INTEGER,local_type INTEGER,"
            "create_time INTEGER,message_content BLOB,WCDB_CT_message_content INTEGER,"
            "source BLOB,WCDB_CT_source INTEGER,real_sender_id INTEGER)"
        )
        db.executemany(
            f"INSERT INTO {table} VALUES (?,?,1,?, ?,0,?,0,1)",  # noqa: S608 -- fixed test tables
            [
                (
                    i,
                    i + 1000,
                    i,
                    f"text {i}".encode(),
                    b"<msgsource><atuserlist>self</atuserlist></msgsource>",
                )
                for i in range(1, 106)
            ],
        )
    db.commit()
    db.close()
    obj = bridge.SourceReader(
        "synthetic",
        {
            "account": "self_machine",
            "chat_ids": ["group@chatroom", "peer"],
        },
    )
    obj.canonical_identity = "self"
    calls = {"auth": 0, "snapshot": 0, "query": 0}

    def api(_path):
        calls["auth"] += 1
        return {"status": "logged_in", "loggedInUser": "self_machine"}

    original = bridge._snapshots.snapshot

    @contextmanager
    def snapshot(source):
        calls["snapshot"] += 1
        with original(source) as captured:
            yield captured

    def query(captured, _key, sql, **_kwargs):
        calls["query"] += 1
        with sqlite3.connect(f"file:{captured}?immutable=1", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(sql)]

    monkeypatch.setattr(obj, "api", api)
    monkeypatch.setattr(
        obj,
        "source_table",
        lambda chat, _account: (
            path,
            "synthetic-key",
            "Msg_group" if chat.endswith("@chatroom") else "Msg_private",
        ),
    )
    monkeypatch.setattr(obj, "_query", query)
    monkeypatch.setattr(bridge._snapshots, "snapshot", snapshot)
    return obj, calls, path


def test_two_chats_share_one_snapshot_query_and_auth_pair(reader):
    obj, calls, _path = reader
    result = obj.read_cycle({"group@chatroom": [3], "peer": [100]})
    assert result["accountVerified"]
    group, private = result["targets"]
    assert [r["localId"] for r in group["rows"]] == list(range(105, 3, -1))
    assert [r["localId"] for r in private["rows"]] == list(range(105, 100, -1))
    assert len(group["anchors"]) == 100
    assert all(r["mentionedSelf"] for r in group["rows"])
    assert not any(r["mentionedSelf"] for r in private["rows"])
    assert calls == {"auth": 2, "snapshot": 1, "query": 1}


def test_baseline_emits_no_old_messages_and_anchor_loss_is_target_scoped(reader):
    obj, _calls, _path = reader
    assert all(
        not t["rows"] for t in obj.read_cycle({"group@chatroom": None, "peer": None})["targets"]
    )
    group, private = obj.read_cycle({"group@chatroom": [9999], "peer": [100]})["targets"]
    assert group["error"] == "AnchorLost" and group["rows"] == []
    assert private["error"] is None and len(private["rows"]) == 5


def test_account_change_after_snapshot_discards_every_target(reader, monkeypatch):
    obj, _calls, _path = reader
    accounts = iter(["self_machine", "changed"])
    monkeypatch.setattr(
        obj,
        "api",
        lambda _path: {
            "status": "logged_in",
            "loggedInUser": next(accounts),
        },
    )
    with pytest.raises(ValueError, match="AccountChanged"):
        obj.read_cycle({"group@chatroom": [100], "peer": [100]})


def test_cycle_rejects_unbound_chat_before_any_read(reader):
    obj, calls, _path = reader
    with pytest.raises(ValueError, match="TargetNotBound"):
        obj.read_cycle({"unbound": []})
    assert calls == {"auth": 0, "snapshot": 0, "query": 0}


def test_cycle_has_bounded_pagination(reader):
    obj, _calls, path = reader
    with sqlite3.connect(path) as connection:
        connection.executemany(
            "INSERT INTO Msg_group VALUES (?,?,1,?, ?,0,?,0,1)",
            [(i, i + 1000, i, b"text", b"") for i in range(106, 1101)],
        )
    group = obj.read_cycle({"group@chatroom": [100]})["targets"][0]
    assert group["error"] == "AnchorLost"  # beyond the newest 1000 rows
    group = obj.read_cycle({"group@chatroom": [101]})["targets"][0]
    assert group["error"] is None and len(group["rows"]) == 999
