from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import socket
import threading
from argparse import Namespace
from pathlib import Path

import pytest

REPRO_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "agent_wechat_repro.py"
REPRO_SPEC = importlib.util.spec_from_file_location("agent_wechat_repro", REPRO_SCRIPT)
if REPRO_SPEC is None or REPRO_SPEC.loader is None:
    raise RuntimeError("Unable to load the reproduction helper")
repro = importlib.util.module_from_spec(REPRO_SPEC)
REPRO_SPEC.loader.exec_module(repro)

MARKER = "LB26_0123456789ABCDEF01234567"


def _use_private_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(repro.Path, "home", lambda: tmp_path)
    state = tmp_path / "research-state"
    monkeypatch.setattr(repro, "STATE", state)
    return state


def test_testing_selection_only_accepts_exact_group_name_and_chatroom_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {"name": "testing", "id": "allowed@chatroom"},
        {"name": "testing-copy", "id": "other@chatroom"},
        {"name": "testing", "id": "private@user"},
    ]
    monkeypatch.setattr(repro, "api", lambda method, path: rows)

    assert repro.testing_chat_ids() == ["allowed@chatroom"]


def test_testing_chat_list_hard_limit_fails_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []
    page = [{"name": "unrelated", "id": f"contact-{index}@user"} for index in range(100)]

    def fake_api(method: str, path: str) -> object:
        calls.append(path)
        return page

    monkeypatch.setattr(repro, "api", fake_api)
    with pytest.raises(SystemExit):
        repro.testing_chat_ids()

    assert len(calls) == 10
    assert "ChatListTooLargeToVerifyUniqueness" in capsys.readouterr().out


def test_login_websocket_handshake_accepts_only_valid_server_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    address = listener.getsockname()
    captured: dict[str, str] = {}
    server_errors: list[Exception] = []

    def fake_server() -> None:
        try:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(3)
                request = bytearray()
                while b"\r\n\r\n" not in request:
                    request.extend(connection.recv(1))
                request_lines = bytes(request).decode("ascii").split("\r\n")
                captured["request"] = request_lines[0]
                headers = {
                    key.strip().lower(): value.strip()
                    for line in request_lines[1:]
                    if ":" in line
                    for key, value in [line.split(":", 1)]
                }
                accept = base64.b64encode(
                    hashlib.sha1(  # noqa: S324 - required by RFC 6455
                        (headers["sec-websocket-key"] + repro.WEBSOCKET_GUID).encode()
                    ).digest()
                ).decode()
                connection.sendall(
                    (
                        "HTTP/1.1 101 Switching Protocols\r\n"
                        "Upgrade: websocket\r\n"
                        "Connection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                    ).encode("ascii")
                )
                for event in (
                    {"status": {"message": "private status"}},
                    {"login_success": {"userId": "private-account-id"}},
                ):
                    payload = json.dumps(event).encode()
                    connection.sendall(bytes((0x81, len(payload))) + payload)
        except Exception as error:  # pragma: no cover - asserted in the test thread
            server_errors.append(error)
        finally:
            listener.close()

    thread = threading.Thread(target=fake_server)
    thread.start()
    real_connect = repro.socket.create_connection
    monkeypatch.setattr(repro, "auth_token", lambda: "private-token")
    monkeypatch.setattr(
        repro.socket,
        "create_connection",
        lambda _address, timeout=8: real_connect(address, timeout=timeout),
    )

    events = repro.run_login_websocket(5)
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert server_errors == []
    assert "newAccount=false" in captured["request"]
    assert "token=private-token" in captured["request"]
    assert events == {"status": 1, "login_success": 1}


def test_login_event_parser_retains_only_event_names() -> None:
    payload = json.dumps(
        {
            "status": {"message": "private status text"},
            "data": {"type": "qr", "qrData": "private qr payload"},
            "login_success": {"userId": "private-account-id"},
        }
    ).encode()

    assert repro.login_event_kinds(payload) == {"status", "qr", "login_success"}


def test_login_flow_skips_websocket_when_postconditions_already_hold(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(repro, "docker_daemon", lambda: "29.1.3")
    monkeypatch.setattr(repro, "container_info", lambda: {"State": {"Running": True}})
    responses = iter(
        [
            {"status": "logged_in", "loggedInUser": "private-account-id"},
            [{"id": "private-chat-id", "name": "private chat"}],
        ]
    )
    monkeypatch.setattr(repro, "api", lambda method, path: next(responses))
    monkeypatch.setattr(
        repro, "run_login_websocket", lambda timeout_seconds: pytest.fail("unexpected login flow")
    )

    repro.cmd_login_flow(Namespace(timeout_seconds=45))

    output = capsys.readouterr().out
    assert '"result": "already_initialized"' in output
    assert "private-account-id" not in output
    assert "private-chat-id" not in output
    assert "private chat" not in output


def test_login_flow_does_not_skip_when_auth_state_is_logged_out(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(repro, "docker_daemon", lambda: "29.1.3")
    monkeypatch.setattr(repro, "container_info", lambda: {"State": {"Running": True}})
    responses = iter(
        [
            {"status": "logged_out", "loggedInUser": "private-account-id"},
            [{"id": "private-chat-id", "name": "private chat"}],
            {"status": "logged_in", "loggedInUser": "private-account-id"},
            [{"id": "private-chat-id", "name": "private chat"}],
        ]
    )
    monkeypatch.setattr(repro, "api", lambda method, path: next(responses))
    monkeypatch.setattr(repro, "run_login_websocket", lambda timeout_seconds: {"login_success": 1})

    repro.cmd_login_flow(Namespace(timeout_seconds=45))

    output = capsys.readouterr().out
    assert '"result": "postconditions_met"' in output
    assert '"auth_status": "logged_in"' in output
    assert "private-account-id" not in output
    assert "private-chat-id" not in output
    assert "private chat" not in output


def test_testing_selection_refuses_ambiguous_group_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _use_private_state(tmp_path, monkeypatch)
    rows = [
        {"name": "testing", "id": "first@chatroom"},
        {"name": "testing", "id": "second@chatroom"},
    ]
    monkeypatch.setattr(repro, "api", lambda method, path: rows)

    with pytest.raises(SystemExit):
        repro.cmd_select_testing(Namespace())

    assert not (state / "testing-chat.json").exists()
    output = capsys.readouterr().out
    assert '"error": "TestingGroupAmbiguous"' in output
    assert '"match_count": 2' in output
    assert "first@chatroom" not in output
    assert "second@chatroom" not in output


def test_testing_selection_reports_empty_chat_list_without_identifiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _use_private_state(tmp_path, monkeypatch)
    monkeypatch.setattr(repro, "api", lambda method, path: [])

    with pytest.raises(SystemExit):
        repro.cmd_select_testing(Namespace())

    assert not (state / "testing-chat.json").exists()
    assert '"error": "TestingGroupNotFound"' in capsys.readouterr().out


def test_testing_chat_rechecks_unique_group_before_reading_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _use_private_state(tmp_path, monkeypatch)
    repro.ensure_state()
    repro.private_write(state / "testing-chat.json", json.dumps({"id": "first@chatroom"}).encode())
    message_reads: list[str] = []

    def fake_api(method: str, path: str, body: dict[str, object] | None = None) -> object:
        if path.startswith("/api/chats?"):
            return [
                {"name": "testing", "id": "first@chatroom"},
                {"name": "testing", "id": "second@chatroom"},
            ]
        message_reads.append(path)
        return []

    monkeypatch.setattr(repro, "api", fake_api)
    with pytest.raises(SystemExit):
        repro.cmd_observe(Namespace(marker=MARKER))

    assert message_reads == []


def test_send_timeout_is_recorded_and_the_same_marker_is_never_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _use_private_state(tmp_path, monkeypatch)
    monkeypatch.setattr(repro, "testing_chat", lambda: "fixed-testing-id@chatroom")
    api_calls: list[tuple[str, str]] = []

    def timed_out_api(method: str, path: str, body: dict[str, object] | None = None) -> object:
        api_calls.append((method, path))
        raise SystemExit(2)

    monkeypatch.setattr(repro, "api", timed_out_api)
    args = Namespace(confirm_testing=True, marker=MARKER)

    with pytest.raises(SystemExit):
        repro.cmd_send(args)
    ledger = json.loads((state / "sent-markers.json").read_text())
    assert ledger[MARKER]["state"] == "unknown_no_retry"

    with pytest.raises(SystemExit):
        repro.cmd_send(args)

    assert api_calls == [("POST", "/api/messages/send")]
    assert "MarkerAlreadyAttemptedNoRetry" in capsys.readouterr().out


def test_send_without_testing_confirmation_never_reaches_group_or_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _use_private_state(tmp_path, monkeypatch)
    group_lookups: list[bool] = []
    api_calls: list[bool] = []
    monkeypatch.setattr(repro, "testing_chat", lambda: group_lookups.append(True))
    monkeypatch.setattr(repro, "api", lambda *args, **kwargs: api_calls.append(True))

    with pytest.raises(SystemExit):
        repro.cmd_send(Namespace(confirm_testing=False, marker=MARKER))

    assert group_lookups == []
    assert api_calls == []
    assert not state.exists()


def test_observe_never_prints_raw_message_or_identifiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _use_private_state(tmp_path, monkeypatch)
    monkeypatch.setattr(repro, "testing_chat", lambda: "private-group-id@chatroom")
    paths: list[str] = []

    def fake_api(method: str, path: str, body: dict[str, object] | None = None) -> object:
        paths.append(path)
        return [
            {
                "content": f"secret-message-body {MARKER}",
                "localId": "private-message-id",
                "sender": "private-sender-id",
                "isSelf": False,
            }
        ]

    monkeypatch.setattr(repro, "api", fake_api)
    repro.cmd_observe(Namespace(marker=MARKER))

    output = capsys.readouterr().out
    result = json.loads(output)
    assert paths == ["/api/messages/private-group-id%40chatroom?limit=100&offset=0"]
    assert result["matches"][0]["is_self"] is False
    assert result["matches"][0]["sender_ref"].startswith("ref_")
    for secret in (
        "secret-message-body",
        "private-group-id",
        "private-message-id",
        "private-sender-id",
    ):
        assert secret not in output
    assert state.exists()


def test_container_profile_keeps_default_capabilities_and_only_adds_ptrace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _use_private_state(tmp_path, monkeypatch)
    image = "ghcr.io/thisnick/agent-wechat@sha256:" + "a" * 64
    expected = {
        "Config": {"Image": image, "Labels": {"io.lemonbot.agent-wechat-repro": "1"}},
        "HostConfig": {
            "CapDrop": [],
            "CapAdd": ["CAP_SYS_PTRACE"],
            "SecurityOpt": ["seccomp=unconfined"],
            "Privileged": False,
            "PidMode": "",
            "IpcMode": "private",
            "Devices": [],
            "RestartPolicy": {"Name": "no"},
            "PortBindings": {"6174/tcp": [{"HostIp": "127.0.0.1", "HostPort": "6174"}]},
        },
        "Mounts": [
            {"Destination": "/data", "Type": "volume", "Name": repro.DATA_VOLUME, "RW": True},
            {
                "Destination": "/home/wechat",
                "Type": "volume",
                "Name": repro.HOME_VOLUME,
                "RW": True,
            },
            {
                "Destination": "/data/auth-token",
                "Type": "bind",
                "Source": str(state / "api-token"),
                "RW": False,
            },
        ],
    }
    repro.validate_container(expected, {"image": image})

    mismatched = {
        **expected,
        "HostConfig": {**expected["HostConfig"], "CapDrop": ["ALL"]},
    }
    with pytest.raises(SystemExit):
        repro.validate_container(mismatched, {"image": image})
