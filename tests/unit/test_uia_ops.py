from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from typer.testing import CliRunner

import lemonbot.connectors as connectors
from lemonbot.cli import app
from lemonbot.connectors import (
    SelectorBundle,
    UIADriverError,
    UIASnapshot,
    WindowsWeChatUIABackend,
)


def _enrolled_backend() -> WindowsWeChatUIABackend:
    backend = object.__new__(WindowsWeChatUIABackend)
    backend._expected_process_name = "WeChat.exe"
    backend._expected_executable_path = r"C:\Program Files\Tencent\WeChat\WeChat.exe"
    backend._expected_executable_sha256 = "c" * 64
    backend._expected_windows_user = "lab-user"
    backend._expected_account_id = "a" * 64
    backend._enrolled_client_version = "4.1.2.3"
    backend._enrolled_selector_signature = "b" * 64
    return backend


def _enrolled_snapshot() -> UIASnapshot:
    return UIASnapshot(
        windows_user="lab-user",
        session_locked=False,
        process_name="WeChat.exe",
        process_count=1,
        account_id="a" * 64,
        client_version="4.1.2.3",
        window_handle=12345,
        selector_signature="b" * 64,
        executable_path=r"C:\PROGRAM FILES\Tencent\WeChat\WeChat.exe",
        executable_sha256="c" * 64,
        target_chat_id="chat-1",
        target_match_count=1,
    )


def test_checked_in_selector_bundle_is_valid_and_fail_closed() -> None:
    path = Path("config/wechat_uia_selectors.example.json")
    bundle = SelectorBundle.load(path)

    selectors = (bundle.window, *bundle.controls.values())
    for selector in selectors:
        exact_values = (
            selector.control_type,
            selector.automation_id,
            selector.class_name,
            selector.name,
        )
        assert any(value and value.startswith("__ENROLL__:") for value in exact_values)
    assert all(key.startswith("__ENROLL__:") for key in bundle.chat_targets)
    assert all(value.startswith("__ENROLL__:") for value in bundle.chat_targets.values())


def test_uia_executable_identity_uses_resolved_path_and_content_hash(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "WeChat.exe"
    content = b"enrolled executable bytes"
    executable.write_bytes(content)
    process = type("Process", (), {"info": {"exe": str(executable)}})()

    path, digest = WindowsWeChatUIABackend._executable_identity(process)

    assert path == str(executable.resolve())
    assert digest == hashlib.sha256(content).hexdigest()


def test_uia_identity_gate_pins_case_insensitive_path_and_exact_hash() -> None:
    backend = _enrolled_backend()
    snapshot = _enrolled_snapshot()

    assert backend._identity_reasons(snapshot, "chat-1") == []
    assert "executable hash changed" in backend._identity_reasons(
        replace(snapshot, executable_sha256="d" * 64),
        "chat-1",
    )


def test_uia_send_revalidates_identity_after_editor_fill_before_click(
    monkeypatch: Any,
) -> None:
    backend = _enrolled_backend()
    snapshot = _enrolled_snapshot()

    class Initializer:
        def Uninitialize(self) -> None:
            pass

    class ValuePattern:
        IsReadOnly = False
        Value = ""

        def SetValue(self, value: str, *, waitTime: int) -> bool:
            assert waitTime == 0
            self.Value = value
            return True

    class Editor:
        value = ValuePattern()

        def GetValuePattern(self) -> ValuePattern:
            return self.value

    class SendButton:
        clicks = 0

        def Click(self, **_kwargs: object) -> None:
            self.clicks += 1

    editor = Editor()
    send = SendButton()
    require_calls = 0

    def require_state(_target: str | None) -> tuple[object, object, object, UIASnapshot]:
        nonlocal require_calls
        require_calls += 1
        if require_calls == 2:
            raise UIADriverError("identity changed")
        return Initializer(), object(), object(), snapshot

    monkeypatch.setattr(backend, "_prepare_target_sync", lambda _chat: snapshot)
    monkeypatch.setattr(backend, "_require_enrolled_state", require_state)
    monkeypatch.setattr(backend, "_message_texts", lambda _window, _text: 0)
    monkeypatch.setattr(
        backend,
        "_find_one",
        lambda _window, key: editor if key == "input_edit" else send,
    )

    result = backend._send_text_sync("chat-1", "safe draft")

    assert not result.attempted
    assert require_calls == 2
    assert editor.value.Value == "safe draft"
    assert send.clicks == 0


def test_uia_inspect_emits_only_non_text_enrollment_facts(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    config = tmp_path / "lab.toml"
    config.write_text(
        "\n".join(
            (
                "schema_version = 1",
                'profile = "lab"',
                "[runtime]",
                'connector = "fake"',
                "[wechat_uia]",
                'selector_bundle_path = ""',
            )
        ),
        encoding="utf-8",
    )

    class FakeSelectorBundle:
        @classmethod
        def load(cls, path: Path) -> object:
            assert path.name == "selectors.json"
            return object()

    class FakeBackend:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def inspect(self) -> UIASnapshot:
            return UIASnapshot(
                windows_user="never-print-windows-user",
                session_locked=False,
                process_name="never-print-process-name",
                process_count=1,
                account_id="a" * 64,
                client_version="4.1.2.3",
                window_handle=12345,
                selector_signature="b" * 64,
                executable_path=r"C:\Program Files\Tencent\WeChat\WeChat.exe",
                executable_sha256="c" * 64,
                target_chat_id="never-print-chat",
                target_match_count=1,
            )

        async def close(self) -> None:
            pass

    monkeypatch.setattr(connectors, "SelectorBundle", FakeSelectorBundle)
    monkeypatch.setattr(connectors, "WindowsWeChatUIABackend", FakeBackend)

    result = CliRunner().invoke(
        app,
        [
            "uia",
            "inspect",
            "--config",
            str(config),
            "--selector-bundle",
            str(tmp_path / "selectors.json"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert '"account_sha256": "' + "a" * 64 + '"' in result.output
    assert '"selector_sha256": "' + "b" * 64 + '"' in result.output
    assert '"executable_sha256": "' + "c" * 64 + '"' in result.output
    assert '"executable_path": "C:\\\\Program Files' in result.output
    assert '"client_version": "4.1.2.3"' in result.output
    assert '"window_handle": 12345' in result.output
    assert '"session_locked": false' in result.output
    assert "never-print" not in result.output


def test_uia_poll_emits_every_burst_message_and_distinguishes_duplicates(
    monkeypatch: Any,
) -> None:
    backend = _enrolled_backend()
    backend._channel = "wechat_personal_lab"
    backend._bundle = SimpleNamespace(
        chat_targets={"chat-1": "Chat One"},
        controls={
            "unread_badge": "unread",
            "message_item": "item",
            "incoming_marker": "incoming",
            "message_text": "text",
            "message_sender": "sender",
            "message_timestamp": "time",
        },
    )

    class Initializer:
        def Uninitialize(self) -> None:
            pass

    class Control:
        def __init__(self, kind: str, *, name: str = "", automation_id: str = "") -> None:
            self.kind = kind
            self.Name = name
            self.AutomationId = automation_id
            self.NativeWindowHandle = 0

        def Click(self, **_kwargs: object) -> None:
            pass

    conversation_list = Control("conversation-list")
    conversation = Control("conversation", name="Chat One")
    message_list = Control("message-list")
    first = Control("message", automation_id="message-1")
    second = Control("message", automation_id="message-2")
    third = Control("message", automation_id="message-3")
    messages = [first, second, third]
    fields = {
        first: {
            "text": [Control("text", name="same")],
            "sender": [Control("sender", name="Alice")],
            "time": [Control("time", name="10:00")],
        },
        second: {
            "text": [Control("text", name="same")],
            "sender": [Control("sender", name="Alice")],
            "time": [Control("time", name="10:00")],
        },
        third: {
            "text": [Control("text", name="without timestamp")],
            "sender": [Control("sender", name="Bob")],
            "time": [],
        },
    }
    baseline = _enrolled_snapshot()
    window = object()

    monkeypatch.setattr(
        backend,
        "_require_enrolled_state",
        lambda _chat: (Initializer(), object(), window, baseline),
    )
    monkeypatch.setattr(backend, "_identity_reasons", lambda *_args: [])
    monkeypatch.setattr(
        backend,
        "_snapshot_from_state",
        lambda _process, _window, _chat: baseline,
    )
    monkeypatch.setattr(
        backend,
        "_find_one",
        lambda _window, key: conversation_list if key == "conversation_list" else message_list,
    )
    monkeypatch.setattr(
        backend,
        "_walk",
        lambda control, _depth: [(conversation, 1)] if control is conversation_list else [],
    )

    def find_all(control: Control, selector: str) -> list[Control]:
        if control is conversation and selector == "unread":
            return [Control("badge")]
        if control is message_list and selector == "item":
            return messages
        if control in messages and selector == "incoming":
            return [Control("incoming")]
        if control in messages:
            return fields[control][selector]
        return []

    monkeypatch.setattr(backend, "_find_all", find_all)

    events = backend._poll_events_sync()

    assert [event.text for event in events] == ["same", "same", "without timestamp"]
    assert len({event.event_id for event in events}) == 3
    assert events[2].metadata["displayed_timestamp"] == ""
