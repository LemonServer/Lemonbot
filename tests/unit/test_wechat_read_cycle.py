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


def test_cycle_http_requires_token_and_bounded_body(reader, monkeypatch):
    import io
    from types import SimpleNamespace

    obj, calls, _path = reader
    captured = {}

    def server(_address, handler):
        captured["handler"] = handler
        return SimpleNamespace(serve_forever=lambda: None)

    monkeypatch.setattr(bridge, "ThreadingHTTPServer", server)
    monkeypatch.setattr(bridge, "notification_directories", lambda _reader: [])
    monkeypatch.setattr(
        bridge, "ChangeNotifications", lambda _dirs: SimpleNamespace(close=lambda: None)
    )
    bridge.serve("synthetic", obj, None)
    handler = captured["handler"]

    def request(token, body, length=None, path="/read-cycle"):
        import json

        raw = json.dumps(body).encode()
        instance = object.__new__(handler)
        instance.client_address = ("127.0.0.1", 0)
        instance.path = path
        instance.headers = {
            "Authorization": "Bearer " + token,
            "Content-Length": str(length if length is not None else len(raw)),
        }
        instance.rfile, instance.wfile = io.BytesIO(raw), io.BytesIO()
        statuses = []
        instance.send_error = lambda status: statuses.append(status)
        instance.send_response = lambda status: statuses.append(status)
        instance.send_header = lambda *_args: None
        instance.end_headers = lambda: None
        instance.do_POST()
        return statuses, instance.wfile.getvalue()

    assert request("wrong", {"anchors": {"peer": None}})[0] == [401]
    assert calls["auth"] == 0
    assert request("synthetic", {}, length=16385)[0] == [503]
    assert calls["auth"] == 0
    assert request("synthetic", {"anchors": {"peer": None}})[0] == [200]
    assert calls["auth"] == 2
    assert request("wrong", {"account": "self_machine", "chat_ids": ["peer"]}, path="/bindings")[
        0
    ] == [401]
    assert obj.config["chat_ids"] == ["group@chatroom", "peer"]


def test_bridge_binding_sync_is_same_account_and_persists_only_after_validation(reader, tmp_path):
    import json

    obj, _calls, _path = reader
    obj.binding_path = tmp_path / "binding.json"

    def api(path):
        if path == "/api/status/auth":
            return {"status": "logged_in", "loggedInUser": "self_machine"}
        return {"id": path.rsplit("/", 1)[-1], "isGroup": False}

    obj.api = api
    result = obj.configure_targets({"account": "self_machine", "chat_ids": ["peer", "new-peer"]})
    assert result["accountVerified"] is True
    assert json.loads(obj.binding_path.read_text())["chat_ids"] == ["peer", "new-peer"]
    assert obj.binding_path.stat().st_mode & 0o077 == 0
    restored = bridge.SourceReader("synthetic", json.loads(obj.binding_path.read_text()))
    assert restored.canonical_identity == "self"
    old = obj.binding_path.read_bytes()
    for bad in [
        {"account": "other_account", "chat_ids": ["peer"]},
        {"account": "self_machine", "chat_ids": ["peer", "peer"]},
        {"account": "self_machine", "chat_ids": []},
        {"account": "self_machine", "chat_ids": [["peer"]]},
        {"account": "self_machine", "chat_ids": [str(i) for i in range(21)]},
    ]:
        with pytest.raises(ValueError):
            obj.configure_targets(bad)
        assert obj.binding_path.read_bytes() == old
    obj.api = lambda _p: {"status": "logged_out"}
    with pytest.raises(ValueError, match="AccountChanged"):
        obj.configure_targets({"account": "self_machine", "chat_ids": ["peer"]})
    assert obj.binding_path.read_bytes() == old


def test_cycle_supports_more_than_two_explicitly_bound_targets(reader):
    obj, calls, _path = reader
    obj.config["chat_ids"].append("third-peer")
    result = obj.read_cycle({c: None for c in obj.config["chat_ids"]})
    assert len(result["targets"]) == 3
    assert all(t["rows"] == [] for t in result["targets"])
    assert calls["snapshot"] == 1


def test_sync_preserves_verified_self_before_replacing_old_scope(reader, tmp_path):
    import json

    obj, _calls, _path = reader
    obj.canonical_identity = None
    obj.binding_path = tmp_path / "binding.json"
    requests = []

    def api(path):
        requests.append(path)
        if path == "/api/status/auth":
            return {"status": "logged_in", "loggedInUser": "self_machine"}
        if path.startswith("/api/messages/"):
            return [{"isSelf": True, "sender": "self"}]
        return {"id": path.rsplit("/", 1)[-1], "isGroup": False}

    obj.api = api
    obj.configure_targets({"account": "self_machine", "chat_ids": ["new-peer"]})
    assert requests.count("/api/messages/group%40chatroom?limit=100") == 1
    assert not any(p.startswith("/api/messages/new-peer") for p in requests)
    stored = json.loads(obj.binding_path.read_text())
    assert stored["self_identity"] == "self"
    assert bridge.SourceReader("synthetic", stored).canonical_identity == "self"
    with pytest.raises(ValueError, match="CanonicalSelfMismatch"):
        bridge.SourceReader("synthetic", {**stored, "self_identity": "other"})
