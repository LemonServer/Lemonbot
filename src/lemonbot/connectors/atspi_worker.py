"""Read-only stdio worker for official Linux WeChat accessibility snapshots."""

from __future__ import annotations

import hashlib
import importlib
import queue
import sys
import threading
import time
from collections.abc import Callable
from functools import partial
from typing import Any, BinaryIO, Literal

from pydantic import ValidationError

from lemonbot.ipc import Envelope, IPCError, read_frame_sync, write_frame_sync

from .atspi_protocol import (
    AtspiInit,
    AtspiReady,
    AtspiShutdown,
    AtspiSnapshot,
    AtspiTargetSpec,
    AtspiTranscriptItem,
    AtspiWorkerError,
)

INIT = "wechat_atspi.init"
READY = "wechat_atspi.ready"
SNAPSHOT = "wechat_atspi.snapshot"
HEALTH = "wechat_atspi.health"
ERROR = "wechat_atspi.error"
SHUTDOWN = "worker.shutdown"
STOPPED = "worker.stopped"


def _safe(call: Callable[[], Any], default: Any = None) -> Any:
    try:
        return call()
    except Exception:
        return default


def _load_atspi() -> Any:
    gi = importlib.import_module("gi")
    gi.require_version("Atspi", "2.0")
    return importlib.import_module("gi.repository.Atspi")


def _node_at(root: Any, selector: tuple[int, ...]) -> Any | None:
    node = root
    for index in selector:
        count = int(_safe(node.get_child_count, 0) or 0)
        if index < 0 or index >= count:
            return None
        node = _safe(partial(node.get_child_at_index, index))
        if node is None:
            return None
    return node


def _text(node: Any) -> str:
    text_iface = _safe(node.get_text_iface)
    if text_iface is not None:
        value = _safe(lambda: text_iface.get_text(0, -1), "")
        if value:
            return str(value).strip()
    return str(_safe(node.get_name, "") or "").strip()


def _interfaces(node: Any) -> tuple[str, ...]:
    values = _safe(node.get_interfaces, []) or []
    return tuple(sorted(str(value).rsplit(".", 1)[-1] for value in values))


def _structure_fingerprint(root: Any, *, max_nodes: int = 256) -> str:
    structures: list[str] = []
    stack: list[tuple[Any, str]] = [(root, "0")]
    while stack and len(structures) < max_nodes:
        node, path = stack.pop()
        role = str(_safe(node.get_role_name, "unknown") or "unknown")
        count = int(_safe(node.get_child_count, 0) or 0)
        if count < 0 or count > max_nodes:
            count = 0
        structures.append(f"{path}|{role}|{','.join(_interfaces(node))}|{count}")
        children: list[tuple[Any, str]] = []
        for index in range(count):
            child = _safe(partial(node.get_child_at_index, index))
            if child is not None:
                children.append((child, f"{path}.{index}"))
        stack.extend(reversed(children))
    return hashlib.sha256("\n".join(structures).encode()).hexdigest()


def _attributes(node: Any) -> dict[str, str]:
    raw = _safe(node.get_attributes, {}) or {}
    if isinstance(raw, dict):
        return {str(key): str(value) for key, value in raw.items()}
    result: dict[str, str] = {}
    for item in raw:
        key, separator, value = str(item).partition(":")
        if separator:
            result[key] = value
    return result


class AtspiWorkerService:
    def __init__(self, reader: BinaryIO, writer: BinaryIO) -> None:
        self._reader = reader
        self._writer = writer
        self._write_lock = threading.Lock()
        self._stop = threading.Event()
        self._trigger: queue.Queue[None] = queue.Queue(maxsize=1)
        self._generation = 0
        self._account_fingerprint = ""

    def _write(self, envelope: Envelope) -> None:
        with self._write_lock:
            write_frame_sync(self._writer, envelope)

    def _reply(self, request: Envelope, message_type: str, payload: Any) -> None:
        self._write(
            Envelope(
                request_id=request.request_id,
                message_type=message_type,
                payload=payload.model_dump(mode="json"),
            )
        )

    def _signal(self, *_args: object) -> None:
        try:
            self._trigger.put_nowait(None)
        except queue.Full:
            pass

    @staticmethod
    def _apps(atspi: Any, pids: frozenset[int]) -> tuple[Any, ...]:
        desktop = atspi.get_desktop(0)
        count = min(max(int(_safe(desktop.get_child_count, 0) or 0), 0), 1_000)
        matches = []
        for index in range(count):
            app = _safe(partial(desktop.get_child_at_index, index))
            if app is not None and int(_safe(app.get_process_id, 0) or 0) in pids:
                matches.append(app)
        return tuple(matches)

    def _target_snapshot(
        self, apps: tuple[Any, ...], target: AtspiTargetSpec
    ) -> AtspiSnapshot | None:
        matches: list[tuple[Any, Any]] = []
        for app in apps:
            header = _node_at(app, target.header_selector)
            transcript = _node_at(app, target.transcript_selector)
            if header is None or transcript is None:
                continue
            fingerprint = hashlib.sha256(_text(header).encode("utf-8")).hexdigest()
            if fingerprint == target.header_fingerprint:
                matches.append((header, transcript))
        if len(matches) != 1:
            return None
        _header, transcript = matches[0]
        count = min(max(int(_safe(transcript.get_child_count, 0) or 0), 0), 500)
        items: list[AtspiTranscriptItem] = []
        for index in range(count):
            item = _safe(partial(transcript.get_child_at_index, index))
            if item is None:
                continue
            signature = _structure_fingerprint(item)
            if signature == target.self_item_signature:
                direction: Literal["inbound", "self"] = "self"
                body_path = target.self_body_relative_path
            elif signature == target.inbound_item_signature:
                direction = "inbound"
                body_path = target.inbound_body_relative_path
            else:
                continue
            body = _node_at(item, body_path)
            if body is None:
                continue
            body_text = _text(body)
            if not body_text or len(body_text) > 100_000:
                continue
            sender_ref = None
            if target.sender_relative_path is not None and target.sender_attribute_key:
                sender = _node_at(item, target.sender_relative_path)
                if sender is not None:
                    identity = _attributes(sender).get(target.sender_attribute_key, "")
                    if identity:
                        sender_ref = hashlib.sha256(
                            f"{self._account_fingerprint}\0{identity}".encode()
                        ).hexdigest()
            items.append(
                AtspiTranscriptItem(
                    direction=direction,
                    sender_ref=sender_ref,
                    text=body_text,
                    structure_fingerprint=signature,
                )
            )
        self._generation += 1
        return AtspiSnapshot(
            target_ref=target.target_ref,
            chat_kind=target.chat_kind,
            header_fingerprint=target.header_fingerprint,
            generation=self._generation,
            items=tuple(items),
        )

    def _reader_loop(self, request_id: object) -> None:
        del request_id
        try:
            envelope = read_frame_sync(self._reader)
            if envelope.message_type == SHUTDOWN:
                AtspiShutdown.model_validate(envelope.payload)
                self._stop.set()
        except (IPCError, ValidationError, OSError):
            self._stop.set()

    def run(self) -> int:
        try:
            request = read_frame_sync(self._reader)
            if request.message_type != INIT:
                return 2
            config = AtspiInit.model_validate(request.payload)
            self._account_fingerprint = config.account_fingerprint
            atspi = _load_atspi()
            atspi.init()
            apps = self._apps(atspi, frozenset(config.expected_pids))
            if not apps:
                self._reply(request, ERROR, AtspiWorkerError(code="wechat_app_not_found"))
                return 1
            listener = atspi.EventListener.new(self._signal, None)
            registered = 0
            for app in apps:
                for event_type in (
                    "object:text-changed",
                    "object:children-changed",
                    "object:property-change",
                    "object:state-changed",
                    "window",
                ):
                    try:
                        listener.register_with_app(event_type, [], app)
                        registered += 1
                    except Exception:  # noqa: S112 - registration is probed event-by-event
                        continue
            if registered == 0:
                self._reply(request, ERROR, AtspiWorkerError(code="event_scope_unavailable"))
                return 1
            matched_pids = tuple(
                sorted({int(_safe(app.get_process_id, 0) or 0) for app in apps})
            )
            self._reply(
                request,
                READY,
                AtspiReady(worker_version="1", matched_pids=matched_pids),
            )
            threading.Thread(
                target=self._reader_loop,
                args=(request.request_id,),
                daemon=True,
            ).start()
            self._signal()
            while not self._stop.is_set():
                try:
                    self._trigger.get(timeout=config.reconcile_seconds)
                    time.sleep(config.debounce_ms / 1_000)
                except queue.Empty:
                    pass
                for target in config.targets:
                    snapshot = self._target_snapshot(apps, target)
                    if snapshot is not None:
                        self._write(
                            Envelope(
                                message_type=SNAPSHOT,
                                payload=snapshot.model_dump(mode="json"),
                            )
                        )
            self._write(Envelope(message_type=STOPPED, payload={"stopped": True}))
            return 0
        except (IPCError, ValidationError, OSError):
            return 2
        except Exception:
            try:
                self._write(
                    Envelope(
                        message_type=ERROR,
                        payload=AtspiWorkerError(code="worker_failure").model_dump(mode="json"),
                    )
                )
            except Exception:  # noqa: S110 - no exception text may reach worker output
                pass
            return 1


def main() -> int:
    return AtspiWorkerService(sys.stdin.buffer, sys.stdout.buffer).run()


if __name__ == "__main__":
    raise SystemExit(main())
