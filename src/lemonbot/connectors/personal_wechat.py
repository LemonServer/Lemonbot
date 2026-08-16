"""Fail-closed personal WeChat Win32/UI Automation connector skeleton.

Personal WeChat automation carries account risk and UI selectors change across
versions.  This module deliberately contains no hooks, injection, reverse
protocol, version downgrade, or risk-control evasion.  Sending is impossible
until an administrator enables a stage, enrolls an exact account/client/UI
signature, supplies an allowlist, and configures a UIA backend that can prove
pre- and post-send state.
"""

from __future__ import annotations

import importlib
import inspect
import ntpath
import platform
import re
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PureWindowsPath
from typing import Protocol, runtime_checkable
from uuid import UUID

from lemonbot.domain.models import (
    ConnectorHealth,
    DeliveryReceipt,
    DeliveryStatus,
    InboundEvent,
    OutboundMessage,
    utc_now,
)

from ._dedup import BoundedDeduplicator
from .base import Connector
from .errors import (
    ConnectorDependencyError,
    ConnectorDisabledError,
)


class PersonalWeChatStage(StrEnum):
    DISABLED = "disabled"
    OBSERVE = "observe"
    DRAFT = "draft"
    REPLY = "reply"
    PROACTIVE = "proactive"


_STAGE_ORDER = {
    PersonalWeChatStage.DISABLED: 0,
    PersonalWeChatStage.OBSERVE: 1,
    PersonalWeChatStage.DRAFT: 2,
    PersonalWeChatStage.REPLY: 3,
    PersonalWeChatStage.PROACTIVE: 4,
}

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


def _windows_path_key(value: str) -> str:
    """Return a filesystem-independent, case-insensitive Windows path key."""

    return ntpath.normcase(ntpath.normpath(value))


def _validate_enrolled_executable_path(value: str) -> None:
    path = PureWindowsPath(value)
    if (
        not path.is_absolute()
        or len(path.drive) != 2
        or not path.drive.endswith(":")
        or any(part in {".", ".."} for part in path.parts)
        or value.startswith(("\\\\", "\\\\?\\", "\\\\.\\"))
    ):
        raise ValueError("expected_executable_path must be an absolute local-drive path")


@dataclass(frozen=True, slots=True)
class PersonalWeChatConfig:
    enabled: bool = False
    stage: PersonalWeChatStage = PersonalWeChatStage.DISABLED
    channel: str = "wechat_personal_lab"
    expected_process_name: str = "WeChat.exe"
    expected_executable_path: str | None = None
    expected_executable_sha256: str | None = None
    expected_windows_user: str | None = None
    expected_account_id: str | None = None
    enrolled_client_version: str | None = None
    enrolled_selector_signature: str | None = None
    allowed_chat_ids: frozenset[str] = frozenset()
    max_text_chars: int = 1_500
    dedup_capacity: int = 10_000
    observed_event_capacity: int = 5_000
    receipt_capacity: int = 10_000

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_chat_ids", frozenset(self.allowed_chat_ids))
        if not self.channel.strip():
            raise ValueError("personal WeChat channel must not be empty")
        if not self.expected_process_name.strip():
            raise ValueError("expected_process_name must not be empty")
        if self.expected_executable_path is not None:
            _validate_enrolled_executable_path(self.expected_executable_path)
            if (
                PureWindowsPath(self.expected_executable_path).name.casefold()
                != self.expected_process_name.casefold()
            ):
                raise ValueError(
                    "expected executable filename must match expected_process_name"
                )
        if self.expected_executable_sha256 is not None and not _SHA256_PATTERN.fullmatch(
            self.expected_executable_sha256
        ):
            raise ValueError("expected_executable_sha256 must be 64 lowercase hex characters")
        if self.max_text_chars < 1 or self.dedup_capacity < 1:
            raise ValueError("personal WeChat limits must be positive")
        if self.observed_event_capacity < 1 or self.receipt_capacity < 1:
            raise ValueError("personal WeChat capacities must be positive")
        if any(not chat_id.strip() for chat_id in self.allowed_chat_ids):
            raise ValueError("allowed chat IDs must be non-empty stable identifiers")


@dataclass(frozen=True, slots=True)
class UIASnapshot:
    """Facts a backend must obtain directly from the desktop before acting."""

    windows_user: str
    session_locked: bool
    process_name: str
    process_count: int
    account_id: str | None
    client_version: str | None
    window_handle: int | None
    selector_signature: str | None
    executable_path: str | None
    executable_sha256: str | None
    target_chat_id: str | None = None
    target_match_count: int = 0


@dataclass(frozen=True, slots=True)
class UIASendAttempt:
    """Evidence returned after a backend attempts exactly one send."""

    attempted: bool
    post_snapshot: UIASnapshot | None = None
    readback_text: str | None = None
    external_id: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class UIAPreflightReport:
    safe: bool
    reasons: tuple[str, ...]
    snapshot: UIASnapshot | None


@runtime_checkable
class PersonalWeChatUIABackend(Protocol):
    """Narrow boundary for a separately tested, version-specific UIA driver."""

    def events(self) -> AsyncIterator[InboundEvent]: ...

    async def inspect(self) -> UIASnapshot: ...

    async def prepare_target(self, chat_id: str) -> UIASnapshot: ...

    async def send_text(self, chat_id: str, text: str) -> UIASendAttempt: ...

    async def close(self) -> None: ...


def personal_wechat_dependency_diagnostic() -> str | None:
    """Return a non-secret dependency diagnostic without importing at startup."""

    if platform.system() != "Windows":
        return "personal WeChat UIA requires a Windows 11 x64 runtime"
    missing: list[str] = []
    for module_name, package_name in (
        ("uiautomation", "uiautomation"),
        ("win32gui", "pywin32"),
    ):
        try:
            importlib.import_module(module_name)
        except (ImportError, ModuleNotFoundError):
            missing.append(package_name)
    if missing:
        return "missing optional UIA dependencies: " + ", ".join(sorted(set(missing)))
    return None


class PersonalWeChatConnector(Connector):
    """Safety broker for an enrolled UIA backend; disabled by default."""

    def __init__(
        self,
        config: PersonalWeChatConfig | None = None,
        *,
        backend: PersonalWeChatUIABackend | None = None,
        platform_name: str | None = None,
    ) -> None:
        self.config = config or PersonalWeChatConfig()
        self._backend = backend
        self._platform_name = platform_name or platform.system()
        self._seen = BoundedDeduplicator(self.config.dedup_capacity)
        self._observed_events: OrderedDict[str, str] = OrderedDict()
        self._receipts: OrderedDict[UUID, DeliveryReceipt] = OrderedDict()
        self._replied_events = BoundedDeduplicator(self.config.observed_event_capacity)
        self._closed = False
        self._last_error: str | None = None

    def _backend_diagnostic(self) -> str | None:
        if self._backend is not None:
            return None
        dependency_problem = personal_wechat_dependency_diagnostic()
        if dependency_problem:
            return dependency_problem
        return (
            "UIA dependencies are present, but no enrolled version-specific backend "
            "is configured; sending remains fail-closed"
        )

    def _require_backend(self) -> PersonalWeChatUIABackend:
        problem = self._backend_diagnostic()
        if problem is not None or self._backend is None:
            raise ConnectorDependencyError(problem or "UIA backend is unavailable")
        return self._backend

    def _stage_at_least(self, stage: PersonalWeChatStage) -> bool:
        return _STAGE_ORDER[self.config.stage] >= _STAGE_ORDER[stage]

    def _configuration_reasons(self, target_chat_id: str | None) -> list[str]:
        reasons: list[str] = []
        if self._platform_name != "Windows":
            reasons.append("runtime is not Windows")
        if not self.config.enabled:
            reasons.append("connector kill switch is disabled")
        if self.config.stage is PersonalWeChatStage.DISABLED:
            reasons.append("connector stage is disabled")
        if self.config.expected_account_id is None:
            reasons.append("WeChat account has not been enrolled")
        if self.config.expected_windows_user is None:
            reasons.append("Windows user has not been enrolled")
        if self.config.expected_executable_path is None:
            reasons.append("WeChat executable path has not been enrolled")
        if self.config.expected_executable_sha256 is None:
            reasons.append("WeChat executable hash has not been enrolled")
        if self.config.enrolled_client_version is None:
            reasons.append("WeChat client version has not been enrolled")
        if self.config.enrolled_selector_signature is None:
            reasons.append("UI selector signature has not been enrolled")
        if not self.config.allowed_chat_ids:
            reasons.append("chat allowlist is empty")
        if target_chat_id is not None:
            if target_chat_id not in self.config.allowed_chat_ids:
                reasons.append("target chat is not allowlisted")
        return reasons

    async def preflight(self, target_chat_id: str | None = None) -> UIAPreflightReport:
        """Prove account, client, window and target identity without sending."""

        reasons = self._configuration_reasons(target_chat_id)
        if reasons:
            return UIAPreflightReport(False, tuple(reasons), None)
        try:
            backend = self._require_backend()
            snapshot = await backend.inspect()
            reasons.extend(self._snapshot_reasons(snapshot, None))
            if reasons:
                return UIAPreflightReport(False, tuple(reasons), snapshot)
            if target_chat_id is not None:
                snapshot = await backend.prepare_target(target_chat_id)
        except Exception as exc:
            detail = _safe_detail(exc)
            self._last_error = detail
            return UIAPreflightReport(False, (detail,), None)
        reasons.extend(self._snapshot_reasons(snapshot, target_chat_id))
        return UIAPreflightReport(not reasons, tuple(reasons), snapshot)

    def _snapshot_reasons(
        self, snapshot: UIASnapshot, target_chat_id: str | None
    ) -> list[str]:
        reasons: list[str] = []
        if snapshot.session_locked:
            reasons.append("Windows session is locked")
        if snapshot.process_count != 1:
            reasons.append("expected exactly one WeChat process")
        if snapshot.process_name.casefold() != self.config.expected_process_name.casefold():
            reasons.append("unexpected WeChat process identity")
        if (
            self.config.expected_executable_path is not None
            and (
                snapshot.executable_path is None
                or _windows_path_key(snapshot.executable_path)
                != _windows_path_key(self.config.expected_executable_path)
            )
        ):
            reasons.append("WeChat executable path changed since enrollment")
        if snapshot.executable_sha256 != self.config.expected_executable_sha256:
            reasons.append("WeChat executable hash changed since enrollment")
        if snapshot.window_handle is None or snapshot.window_handle <= 0:
            reasons.append("WeChat window handle is unavailable")
        if (
            self.config.expected_windows_user is not None
            and snapshot.windows_user != self.config.expected_windows_user
        ):
            reasons.append("Windows user changed since enrollment")
        if snapshot.account_id != self.config.expected_account_id:
            reasons.append("WeChat account changed since enrollment")
        if snapshot.client_version != self.config.enrolled_client_version:
            reasons.append("WeChat client version changed since enrollment")
        if snapshot.selector_signature != self.config.enrolled_selector_signature:
            reasons.append("WeChat UI selectors changed since enrollment")
        if target_chat_id is not None:
            if snapshot.target_match_count != 1:
                reasons.append("target chat is missing or ambiguous")
            if snapshot.target_chat_id != target_chat_id:
                reasons.append("selected chat does not match broker-owned target")
        return reasons

    async def events(self) -> AsyncIterator[InboundEvent]:
        if not self.config.enabled or self.config.stage is PersonalWeChatStage.DISABLED:
            raise ConnectorDisabledError(
                "personal WeChat UIA is disabled; enable observe mode explicitly"
            )
        backend = self._require_backend()
        async for event in backend.events():
            if self._closed:
                return
            if event.channel != self.config.channel:
                self._last_error = "UIA backend emitted an event for another channel"
                continue
            if event.chat_id not in self.config.allowed_chat_ids:
                self._last_error = "UIA backend observed a non-allowlisted chat"
                continue
            report = await self.preflight(event.chat_id)
            if not report.safe:
                self._last_error = "; ".join(report.reasons)
                continue
            if not self._seen.add(event.event_id):
                continue
            self._observed_events[event.event_id] = event.chat_id
            self._observed_events.move_to_end(event.event_id)
            if len(self._observed_events) > self.config.observed_event_capacity:
                self._observed_events.popitem(last=False)
            yield event

    async def deliver(self, message: OutboundMessage) -> DeliveryReceipt:
        previous = self._receipts.get(message.message_id)
        if previous is not None:
            return previous
        reason = self._delivery_precondition(message)
        if reason is not None:
            return self._remember(
                DeliveryReceipt(
                    message_id=message.message_id,
                    status=DeliveryStatus.FAILED,
                    detail=reason,
                )
            )
        assert self._backend is not None
        report = await self.preflight(message.chat_id)
        if not report.safe or report.snapshot is None:
            return self._remember(
                DeliveryReceipt(
                    message_id=message.message_id,
                    status=DeliveryStatus.FAILED,
                    detail="preflight failed: " + "; ".join(report.reasons),
                )
            )
        if message.reply_to_event_id is not None:
            # Reserve before the first side effect.  Even a timeout cannot cause
            # this event to be sent again under another message ID.
            self._replied_events.add(message.reply_to_event_id)
        try:
            attempt = await self._backend.send_text(message.chat_id, message.text)
        except Exception as exc:
            return self._remember(
                DeliveryReceipt(
                    message_id=message.message_id,
                    status=DeliveryStatus.UNKNOWN,
                    detail=(
                        "UIA send may have occurred; inspect the chat before retrying: "
                        + _safe_detail(exc)
                    ),
                )
            )
        return self._remember(self._verify_attempt(message, report.snapshot, attempt))

    def _delivery_precondition(self, message: OutboundMessage) -> str | None:
        if message.channel != self.config.channel:
            return "message channel does not match the personal WeChat lab channel"
        if self._closed:
            return "personal WeChat connector is closed"
        if not self.config.enabled:
            return "personal WeChat connector kill switch is disabled"
        if len(message.text) > self.config.max_text_chars:
            return f"message exceeds UIA limit ({self.config.max_text_chars} chars)"
        problem = self._backend_diagnostic()
        if problem is not None:
            return problem
        if not self._stage_at_least(PersonalWeChatStage.REPLY):
            return "current stage may observe or draft but cannot send"
        if message.chat_id not in self.config.allowed_chat_ids:
            return "target chat is not allowlisted"
        if message.reply_to_event_id is None:
            if not self._stage_at_least(PersonalWeChatStage.PROACTIVE):
                return "proactive sending is disabled at the current stage"
        else:
            observed_chat = self._observed_events.get(message.reply_to_event_id)
            if observed_chat != message.chat_id:
                return "reply does not match a recently observed event in this chat"
            if message.reply_to_event_id in self._replied_events:
                return "this inbound event already has a send attempt"
        return None

    def _verify_attempt(
        self,
        message: OutboundMessage,
        before: UIASnapshot,
        attempt: UIASendAttempt,
    ) -> DeliveryReceipt:
        if not attempt.attempted:
            return DeliveryReceipt(
                message_id=message.message_id,
                status=DeliveryStatus.FAILED,
                external_id=attempt.external_id,
                detail=attempt.detail or "UIA backend declined before sending",
            )
        after = attempt.post_snapshot
        reasons: list[str] = []
        if after is None:
            reasons.append("post-send UI snapshot is missing")
        else:
            reasons.extend(self._snapshot_reasons(after, message.chat_id))
            if after.window_handle != before.window_handle:
                reasons.append("WeChat window changed during send")
            if after.account_id != before.account_id:
                reasons.append("WeChat account changed during send")
            if (
                after.executable_path is None
                or before.executable_path is None
                or _windows_path_key(after.executable_path)
                != _windows_path_key(before.executable_path)
            ):
                reasons.append("WeChat executable path changed during send")
            if after.executable_sha256 != before.executable_sha256:
                reasons.append("WeChat executable hash changed during send")
        if attempt.readback_text != message.text:
            reasons.append("sent message could not be read back exactly")
        if reasons:
            return DeliveryReceipt(
                message_id=message.message_id,
                status=DeliveryStatus.UNKNOWN,
                external_id=attempt.external_id,
                detail=(
                    "UIA outcome is uncertain; do not retry automatically: "
                    + "; ".join(reasons)
                ),
            )
        return DeliveryReceipt(
            message_id=message.message_id,
            status=DeliveryStatus.ACKNOWLEDGED,
            external_id=attempt.external_id,
            acknowledged_at=utc_now(),
        )

    def _remember(self, receipt: DeliveryReceipt) -> DeliveryReceipt:
        self._receipts[receipt.message_id] = receipt
        self._receipts.move_to_end(receipt.message_id)
        if len(self._receipts) > self.config.receipt_capacity:
            self._receipts.popitem(last=False)
        return receipt

    async def health(self) -> ConnectorHealth:
        if not self.config.enabled:
            return ConnectorHealth(
                healthy=True,
                detail="disabled by default (safe state)",
                account_id=self.config.expected_account_id,
            )
        if self._closed:
            return ConnectorHealth(
                healthy=False,
                detail="closed",
                account_id=self.config.expected_account_id,
            )
        report = await self.preflight()
        detail = "preflight passed" if report.safe else "; ".join(report.reasons)
        return ConnectorHealth(
            healthy=report.safe,
            detail=detail,
            account_id=self.config.expected_account_id,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._backend is not None:
            result = self._backend.close()
            if inspect.isawaitable(result):
                await result


def _safe_detail(error: object) -> str:
    # UIA exceptions can contain window titles/contact names.  Keep only the
    # exception type for logs and operator-facing health responses.
    return f"{type(error).__name__}: UIA operation failed"[:1_900]


# The longer name makes the transport explicit at call sites while preserving
# the concise public name above.
PersonalWeChatUIAConnector = PersonalWeChatConnector
