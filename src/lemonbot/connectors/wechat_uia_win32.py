"""Version-enrolled Win32 UIA backend for the personal-WeChat lab connector.

The backend never injects code, reads WeChat's database, uses the clipboard, or
falls back to coordinate macros. Every control is selected through an
administrator-enrolled structural bundle. If ValuePattern, target identity, or
post-send readback is unavailable, sending fails closed.
"""

from __future__ import annotations

import asyncio
import getpass
import hashlib
import json
import ntpath
import os
import time
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lemonbot.domain.models import EventKind, InboundEvent, utc_now

from .personal_wechat import UIASendAttempt, UIASnapshot


class UIADriverError(RuntimeError):
    pass


class ControlSelector(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    control_type: str | None = None
    automation_id: str | None = None
    class_name: str | None = None
    name: str | None = None
    max_depth: int = Field(default=8, ge=1, le=20)

    @model_validator(mode="after")
    def require_stable_property(self) -> ControlSelector:
        if not any((self.control_type, self.automation_id, self.class_name, self.name)):
            raise ValueError("a UIA selector must contain at least one exact property")
        return self


class SelectorBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1, le=1)
    window: ControlSelector
    controls: dict[str, ControlSelector]
    signature_controls: tuple[str, ...] = (
        "account",
        "conversation_list",
        "message_list",
        "search_edit",
        "input_edit",
        "send_button",
    )
    chat_targets: dict[str, str]

    @model_validator(mode="after")
    def require_driver_contract(self) -> SelectorBundle:
        required = {
            "account",
            "conversation_list",
            "unread_badge",
            "message_list",
            "message_item",
            "message_text",
            "message_sender",
            "message_timestamp",
            "incoming_marker",
            "search_edit",
            "search_result",
            "chat_header",
            "input_edit",
            "send_button",
        }
        missing = required.difference(self.controls)
        if missing:
            raise ValueError(f"selector bundle is missing controls: {sorted(missing)}")
        if not set(self.signature_controls).issubset(self.controls):
            raise ValueError("signature_controls contains an unknown selector")
        if not self.chat_targets or any(
            not key.strip() or not value.strip() for key, value in self.chat_targets.items()
        ):
            raise ValueError("selector bundle requires stable chat-id to exact-label mappings")
        return self

    @classmethod
    def load(cls, path: Path) -> SelectorBundle:
        raw = path.expanduser().resolve(strict=True).read_bytes()
        if len(raw) > 1024 * 1024:
            raise ValueError("selector bundle exceeds 1 MiB")
        return cls.model_validate_json(raw)


class WindowsWeChatUIABackend:
    def __init__(
        self,
        *,
        bundle: SelectorBundle,
        expected_process_name: str,
        expected_executable_path: str | None = None,
        expected_executable_sha256: str | None = None,
        expected_windows_user: str | None = None,
        expected_account_id: str | None = None,
        enrolled_client_version: str | None = None,
        enrolled_selector_signature: str | None = None,
        channel: str = "wechat_personal_lab",
        poll_seconds: float = 15,
    ) -> None:
        if os.name != "nt":
            raise UIADriverError("the Win32 UIA backend requires Windows")
        if poll_seconds < 5:
            raise ValueError("UIA reconciliation interval cannot be below five seconds")
        self._bundle = bundle
        self._expected_process_name = expected_process_name
        self._expected_executable_path = expected_executable_path
        self._expected_executable_sha256 = expected_executable_sha256
        self._expected_windows_user = expected_windows_user
        self._expected_account_id = expected_account_id
        self._enrolled_client_version = enrolled_client_version
        self._enrolled_selector_signature = enrolled_selector_signature
        self._channel = channel
        self._poll_seconds = poll_seconds
        self._closed = False
        self._operation_lock = asyncio.Lock()

    async def inspect(self) -> UIASnapshot:
        async with self._operation_lock:
            return await asyncio.to_thread(self._inspect_sync, None)

    async def prepare_target(self, chat_id: str) -> UIASnapshot:
        async with self._operation_lock:
            return await asyncio.to_thread(self._prepare_target_sync, chat_id)

    async def send_text(self, chat_id: str, text: str) -> UIASendAttempt:
        async with self._operation_lock:
            return await asyncio.to_thread(self._send_text_sync, chat_id, text)

    async def events(self) -> AsyncIterator[InboundEvent]:
        while not self._closed:
            async with self._operation_lock:
                try:
                    events = await asyncio.to_thread(self._poll_events_sync)
                except Exception:
                    events = ()
            for event in events:
                yield event
            await asyncio.sleep(self._poll_seconds)

    async def close(self) -> None:
        self._closed = True

    def _automation(self) -> Any:
        try:
            import uiautomation as automation
        except ImportError as exc:
            raise UIADriverError("uiautomation is not installed") from exc
        return automation

    def _processes(self) -> list[Any]:
        import psutil  # type: ignore[import-untyped]

        expected = self._expected_process_name.casefold()
        return [
            process
            for process in psutil.process_iter(["pid", "name", "exe"])
            if (process.info.get("name") or "").casefold() == expected
        ]

    @staticmethod
    def _property(control: Any, name: str, default: Any = "") -> Any:
        try:
            return getattr(control, name)
        except Exception:
            return default

    def _walk(self, root: Any, max_depth: int) -> Iterable[tuple[Any, int]]:
        stack: list[tuple[Any, int]] = [(root, 0)]
        visited = 0
        while stack:
            control, depth = stack.pop()
            yield control, depth
            visited += 1
            if visited > 5000:
                raise UIADriverError("UIA tree exceeds the safety traversal limit")
            if depth >= max_depth:
                continue
            try:
                children = control.GetChildren()
            except Exception:
                children = []
            stack.extend((child, depth + 1) for child in reversed(children))

    def _matches(self, control: Any, selector: ControlSelector) -> bool:
        values = {
            "control_type": str(self._property(control, "ControlTypeName")),
            "automation_id": str(self._property(control, "AutomationId")),
            "class_name": str(self._property(control, "ClassName")),
            "name": str(self._property(control, "Name")),
        }
        return all(
            expected is None or values[key] == expected
            for key, expected in (
                ("control_type", selector.control_type),
                ("automation_id", selector.automation_id),
                ("class_name", selector.class_name),
                ("name", selector.name),
            )
        )

    def _find_all(self, root: Any, selector: ControlSelector) -> list[Any]:
        return [
            control
            for control, _depth in self._walk(root, selector.max_depth)
            if self._matches(control, selector)
        ]

    def _find_one(self, root: Any, key: str) -> Any:
        matches = self._find_all(root, self._bundle.controls[key])
        if len(matches) != 1:
            raise UIADriverError(f"selector {key!r} matched {len(matches)} controls")
        return matches[0]

    def _window(self, automation: Any, process_id: int) -> Any:
        root = automation.GetRootControl()
        matches = [
            control
            for control, depth in self._walk(root, self._bundle.window.max_depth)
            if depth > 0
            and int(self._property(control, "ProcessId", 0)) == process_id
            and self._matches(control, self._bundle.window)
        ]
        if len(matches) != 1:
            raise UIADriverError(f"enrolled window selector matched {len(matches)} windows")
        return matches[0]

    @staticmethod
    def _path_key(value: str) -> str:
        return ntpath.normcase(ntpath.normpath(value))

    @staticmethod
    def _executable_identity(process: Any) -> tuple[str | None, str | None]:
        raw = process.info.get("exe")
        if not raw:
            return None, None
        candidate = Path(str(raw))
        try:
            if (
                not candidate.is_absolute()
                or any(
                    part.is_symlink() or part.is_junction()
                    for part in (candidate, *candidate.parents)
                )
            ):
                return None, None
            resolved = candidate.resolve(strict=True)
            if (
                not resolved.is_file()
                or resolved.is_symlink()
                or resolved.is_junction()
            ):
                return None, None
            with resolved.open("rb") as executable:
                before = os.fstat(executable.fileno())
                digest = hashlib.file_digest(executable, "sha256").hexdigest()
                after = os.fstat(executable.fileno())
            identity_before = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            )
            identity_after = (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            )
            if identity_before != identity_after:
                return None, None
            return str(resolved), digest
        except (OSError, RuntimeError):
            return None, None

    @staticmethod
    def _client_version(process: Any) -> str | None:
        executable = process.info.get("exe")
        if not executable:
            return None
        try:
            import win32api  # type: ignore[import-untyped]

            info = win32api.GetFileVersionInfo(executable, "\\")
            major = info["FileVersionMS"] >> 16
            minor = info["FileVersionMS"] & 0xFFFF
            build = info["FileVersionLS"] >> 16
            patch = info["FileVersionLS"] & 0xFFFF
            return f"{major}.{minor}.{build}.{patch}"
        except Exception:
            return None

    @staticmethod
    def _session_locked() -> bool:
        import ctypes

        user32 = ctypes.WinDLL("user32.dll", use_last_error=True)
        desktop = user32.OpenInputDesktop(0, False, 0x0100)
        if not desktop:
            return True
        try:
            return not bool(user32.SwitchDesktop(desktop))
        finally:
            user32.CloseDesktop(desktop)

    def _selector_signature(self, window: Any) -> str:
        values: list[dict[str, str]] = []
        for key in self._bundle.signature_controls:
            control = self._find_one(window, key)
            values.append(
                {
                    "key": key,
                    "type": str(self._property(control, "ControlTypeName")),
                    "automation_id": str(self._property(control, "AutomationId")),
                    "class_name": str(self._property(control, "ClassName")),
                }
            )
        encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _account_signature(self, window: Any) -> str | None:
        account = self._find_one(window, "account")
        value = str(self._property(account, "Name")).strip()
        return hashlib.sha256(value.encode()).hexdigest() if value else None

    def _base_state(self) -> tuple[Any, Any, Any]:
        processes = self._processes()
        if len(processes) != 1:
            raise UIADriverError(f"expected one enrolled WeChat process, found {len(processes)}")
        process = processes[0]
        automation = self._automation()
        initializer = automation.UIAutomationInitializerInThread()
        try:
            window = self._window(automation, process.pid)
            return initializer, process, window
        except BaseException:
            initializer.Uninitialize()
            raise

    def _snapshot_from_state(
        self,
        process: Any,
        window: Any,
        target_chat_id: str | None,
    ) -> UIASnapshot:
        targets = 0
        selected = None
        if target_chat_id is not None:
            label = self._bundle.chat_targets.get(target_chat_id)
            if label:
                header = self._find_one(window, "chat_header")
                targets = int(str(self._property(header, "Name")) == label)
                selected = target_chat_id if targets == 1 else None
        executable_path, executable_sha256 = self._executable_identity(process)
        return UIASnapshot(
            windows_user=getpass.getuser(),
            session_locked=self._session_locked(),
            process_name=process.info.get("name") or "",
            process_count=1,
            account_id=self._account_signature(window),
            client_version=self._client_version(process),
            window_handle=int(self._property(window, "NativeWindowHandle", 0)) or None,
            selector_signature=self._selector_signature(window),
            executable_path=executable_path,
            executable_sha256=executable_sha256,
            target_chat_id=selected,
            target_match_count=targets,
        )

    def _identity_reasons(
        self,
        snapshot: UIASnapshot,
        target_chat_id: str | None,
    ) -> list[str]:
        enrollment = (
            self._expected_executable_path,
            self._expected_executable_sha256,
            self._expected_windows_user,
            self._expected_account_id,
            self._enrolled_client_version,
            self._enrolled_selector_signature,
        )
        if any(value is None or not value for value in enrollment):
            return ["UIA enrollment is incomplete"]
        reasons: list[str] = []
        if snapshot.session_locked:
            reasons.append("Windows session is locked")
        if snapshot.process_count != 1:
            reasons.append("process count changed")
        if snapshot.process_name.casefold() != self._expected_process_name.casefold():
            reasons.append("process name changed")
        if snapshot.window_handle is None or snapshot.window_handle <= 0:
            reasons.append("window handle is unavailable")
        assert self._expected_executable_path is not None
        if (
            snapshot.executable_path is None
            or self._path_key(snapshot.executable_path)
            != self._path_key(self._expected_executable_path)
        ):
            reasons.append("executable path changed")
        if snapshot.executable_sha256 != self._expected_executable_sha256:
            reasons.append("executable hash changed")
        if snapshot.windows_user != self._expected_windows_user:
            reasons.append("Windows user changed")
        if snapshot.account_id != self._expected_account_id:
            reasons.append("account changed")
        if snapshot.client_version != self._enrolled_client_version:
            reasons.append("client version changed")
        if snapshot.selector_signature != self._enrolled_selector_signature:
            reasons.append("selector signature changed")
        if target_chat_id is not None and (
            snapshot.target_match_count != 1
            or snapshot.target_chat_id != target_chat_id
        ):
            reasons.append("target chat is missing or ambiguous")
        return reasons

    def _require_enrolled_state(
        self,
        target_chat_id: str | None,
    ) -> tuple[Any, Any, Any, UIASnapshot]:
        initializer, process, window = self._base_state()
        try:
            snapshot = self._snapshot_from_state(process, window, target_chat_id)
            reasons = self._identity_reasons(snapshot, target_chat_id)
            if reasons:
                raise UIADriverError(
                    "enrolled UIA identity check failed: " + "; ".join(reasons)
                )
            return initializer, process, window, snapshot
        except BaseException:
            initializer.Uninitialize()
            raise

    def _inspect_sync(self, target_chat_id: str | None) -> UIASnapshot:
        initializer, process, window = self._base_state()
        try:
            return self._snapshot_from_state(process, window, target_chat_id)
        finally:
            initializer.Uninitialize()

    def _prepare_target_sync(self, chat_id: str) -> UIASnapshot:
        label = self._bundle.chat_targets.get(chat_id)
        if label is None:
            raise UIADriverError("chat id is not enrolled in the selector bundle")
        initializer, _process, window, before = self._require_enrolled_state(None)
        try:
            window.SetActive()
            search = self._find_one(window, "search_edit")
            pattern = search.GetValuePattern()
            if pattern is None or pattern.IsReadOnly or not pattern.SetValue(label, waitTime=0):
                raise UIADriverError("search control does not support safe ValuePattern input")
            time.sleep(0.5)
            candidates = self._find_all(window, self._bundle.controls["search_result"])
            exact = [item for item in candidates if str(self._property(item, "Name")) == label]
            if len(exact) != 1:
                raise UIADriverError(f"target search result is ambiguous ({len(exact)} matches)")
            exact[0].Click(simulateMove=False, waitTime=0)
            time.sleep(0.5)
        finally:
            initializer.Uninitialize()
        initializer, _process, _window, after = self._require_enrolled_state(chat_id)
        try:
            if after.window_handle != before.window_handle:
                raise UIADriverError("WeChat window changed while selecting the target")
            return after
        finally:
            initializer.Uninitialize()

    def _message_texts(self, window: Any, expected: str) -> int:
        message_list = self._find_one(window, "message_list")
        items = self._find_all(message_list, self._bundle.controls["message_item"])
        count = 0
        for item in items:
            texts = self._find_all(item, self._bundle.controls["message_text"])
            if len(texts) == 1 and str(self._property(texts[0], "Name")) == expected:
                count += 1
        return count

    def _send_text_sync(self, chat_id: str, text: str) -> UIASendAttempt:
        if not text or len(text) > 1500:
            return UIASendAttempt(attempted=False, detail="text is empty or exceeds 1500 chars")
        try:
            prepared = self._prepare_target_sync(chat_id)
            initializer, _process, window, edit_snapshot = self._require_enrolled_state(
                chat_id
            )
        except Exception:
            return UIASendAttempt(
                attempted=False,
                detail="enrolled identity or target verification failed before editing",
            )
        if edit_snapshot.window_handle != prepared.window_handle:
            initializer.Uninitialize()
            return UIASendAttempt(
                attempted=False,
                detail="WeChat window changed before editing",
            )
        attempted = False
        try:
            existing = self._message_texts(window, text)
            editor = self._find_one(window, "input_edit")
            value = editor.GetValuePattern()
            if value is None or value.IsReadOnly or not value.SetValue(text, waitTime=0):
                return UIASendAttempt(
                    attempted=False,
                    detail="message editor has no safe writable ValuePattern",
                )
            if value.Value != text:
                return UIASendAttempt(attempted=False, detail="editor value verification failed")
        except Exception:
            return UIASendAttempt(
                attempted=False,
                detail="UIA editor preparation failed before sending",
            )
        finally:
            initializer.Uninitialize()

        try:
            initializer, _process, window, commit_snapshot = self._require_enrolled_state(
                chat_id
            )
        except Exception:
            return UIASendAttempt(
                attempted=False,
                detail="enrolled identity or target changed before sending",
            )
        try:
            if commit_snapshot.window_handle != prepared.window_handle:
                return UIASendAttempt(
                    attempted=False,
                    detail="WeChat window changed before sending",
                )
            editor = self._find_one(window, "input_edit")
            value = editor.GetValuePattern()
            if value is None or value.IsReadOnly or value.Value != text:
                return UIASendAttempt(
                    attempted=False,
                    detail="editor value changed before sending",
                )
            send = self._find_one(window, "send_button")
            label = self._bundle.chat_targets.get(chat_id)
            header = self._find_one(window, "chat_header")
            final_identity_changed = (
                self._session_locked()
                or getpass.getuser() != self._expected_windows_user
                or self._account_signature(window) != self._expected_account_id
                or int(self._property(window, "NativeWindowHandle", 0))
                != prepared.window_handle
                or label is None
                or str(self._property(header, "Name")) != label
            )
            if final_identity_changed:
                return UIASendAttempt(
                    attempted=False,
                    detail="identity or target changed at the send commit point",
                )
            attempted = True
            send.Click(simulateMove=False, waitTime=0)
            time.sleep(0.8)
            readback = text if self._message_texts(window, text) > existing else None
        except Exception:
            return UIASendAttempt(
                attempted=attempted,
                detail="UIA send state is uncertain",
            )
        finally:
            initializer.Uninitialize()
        after: UIASnapshot | None
        try:
            initializer, _process, _window, after = self._require_enrolled_state(chat_id)
            try:
                if after.window_handle != prepared.window_handle:
                    after = None
            finally:
                initializer.Uninitialize()
        except Exception:
            after = None
        return UIASendAttempt(
            attempted=attempted,
            post_snapshot=after,
            readback_text=readback,
            detail=None if readback else "post-send readback did not find a new exact message",
        )

    def _poll_events_sync(self) -> tuple[InboundEvent, ...]:
        initializer, process, window, baseline = self._require_enrolled_state(None)
        events: list[InboundEvent] = []
        try:
            conversations = self._find_one(window, "conversation_list")
            for chat_id, label in self._bundle.chat_targets.items():
                current = self._snapshot_from_state(process, window, None)
                if self._identity_reasons(current, None):
                    raise UIADriverError("enrolled UIA identity changed while polling")
                if current.window_handle != baseline.window_handle:
                    raise UIADriverError("WeChat window changed while polling")
                candidates = [
                    control
                    for control, _depth in self._walk(conversations, 5)
                    if str(self._property(control, "Name")) == label
                ]
                if len(candidates) != 1:
                    continue
                unread = self._find_all(candidates[0], self._bundle.controls["unread_badge"])
                if len(unread) != 1:
                    continue
                candidates[0].Click(simulateMove=False, waitTime=0)
                time.sleep(0.4)
                selected = self._snapshot_from_state(process, window, chat_id)
                if self._identity_reasons(selected, chat_id):
                    raise UIADriverError("target identity changed while polling")
                message_list = self._find_one(window, "message_list")
                items = self._find_all(message_list, self._bundle.controls["message_item"])
                for item in reversed(items):
                    if len(self._find_all(item, self._bundle.controls["incoming_marker"])) != 1:
                        continue
                    texts = self._find_all(item, self._bundle.controls["message_text"])
                    senders = self._find_all(item, self._bundle.controls["message_sender"])
                    times = self._find_all(item, self._bundle.controls["message_timestamp"])
                    if len(texts) != 1 or len(senders) != 1 or len(times) != 1:
                        break
                    text = str(self._property(texts[0], "Name"))
                    sender = str(self._property(senders[0], "Name"))
                    displayed_time = str(self._property(times[0], "Name"))
                    if not text or not sender or not displayed_time:
                        break
                    sender_id = hashlib.sha256(sender.encode()).hexdigest()
                    fingerprint = hashlib.sha256(
                        f"{chat_id}\0{sender_id}\0{displayed_time}\0{text}".encode()
                    ).hexdigest()
                    events.append(
                        InboundEvent(
                            channel=self._channel,
                            event_id=f"uia:{fingerprint}",
                            chat_id=chat_id,
                            sender_id=sender_id,
                            text=text,
                            kind=EventKind.TEXT,
                            occurred_at=utc_now(),
                            metadata={
                                "vendor": "wechat_win32_uia",
                                "displayed_timestamp": displayed_time,
                                "identity_strength": "experimental_uia",
                            },
                        )
                    )
                    break
        finally:
            initializer.Uninitialize()
        return tuple(events)
