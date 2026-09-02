"""Standalone, consent-bound xdg-desktop-portal ScreenCast probe.

The probe requests one window stream and consumes a bounded number of frames
in memory.  In calibration mode it derives anonymous layout fingerprints and
may send an in-memory label crop to a local OCR minimizer.  It never persists
pixels, emits OCR text, imports model credentials, or exposes remote control.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import io
import json
import os
import re
import secrets
import subprocess
from collections import Counter
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"
REQUEST_IFACE = "org.freedesktop.portal.Request"
SESSION_IFACE = "org.freedesktop.portal.Session"


class PortalProbeError(RuntimeError):
    """A sanitized probe failure safe to categorize without its detail."""


class PortalDenied(PortalProbeError):
    pass


class PortalTimeout(PortalProbeError):
    pass


class PortalProtocolError(PortalProbeError):
    pass


class PortalCaptureError(PortalProbeError):
    pass


_CAPTURE_ERROR_CODES = {
    "row does not fit the frame": "PortalRowOutOfFrame",
    "row has no pixels": "PortalRowUnresolved",
    "no stable row region": "PortalRowUnresolved",
    "row region is ambiguous": "PortalRowUnresolved",
    "row region has no stable edge": "PortalRowUnresolved",
    "row region has no vertical body": "PortalRowUnresolved",
    "row layout changed during capture": "PortalRowChanged",
    "local OCR runtime is unavailable": "PortalOCRFailure",
    "local OCR response is invalid": "PortalOCRFailure",
    "local OCR failed": "PortalOCRFailure",
    "local OCR response is unsafe": "PortalOCRFailure",
}


def _safe_error_code(exc: Exception) -> str:
    safe_types = {
        PortalDenied,
        PortalTimeout,
        PortalProtocolError,
        ValueError,
    }
    if type(exc) in safe_types:
        return type(exc).__name__
    if type(exc) is PortalCaptureError:
        return _CAPTURE_ERROR_CODES.get(str(exc), "PortalCaptureError")
    return "PortalProbeError"


def _load_gi() -> tuple[Any, Any, Any]:
    gi = importlib.import_module("gi")
    gi.require_version("Gio", "2.0")
    gi.require_version("Gst", "1.0")
    gio = importlib.import_module("gi.repository.Gio")
    glib = importlib.import_module("gi.repository.GLib")
    gst = importlib.import_module("gi.repository.Gst")
    return gio, glib, gst


def _sender_component(unique_name: str) -> str:
    component = unique_name.removeprefix(":").replace(".", "_")
    if re.fullmatch(r"[A-Za-z0-9_]+", component) is None:
        raise PortalProtocolError("invalid D-Bus sender")
    return component


def _request_path(unique_name: str, token: str) -> str:
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", token) is None:
        raise PortalProtocolError("invalid request token")
    return f"/org/freedesktop/portal/desktop/request/{_sender_component(unique_name)}/{token}"


def _unpack(value: Any) -> Any:
    return value.unpack() if hasattr(value, "unpack") else value


def _single_stream(results: dict[str, Any]) -> int:
    streams = _unpack(results.get("streams"))
    if not isinstance(streams, (list, tuple)) or len(streams) != 1:
        raise PortalProtocolError("portal must return exactly one stream")
    stream = streams[0]
    if not isinstance(stream, (list, tuple)) or len(stream) != 2:
        raise PortalProtocolError("invalid stream descriptor")
    node_id = _unpack(stream[0])
    if not isinstance(node_id, int) or isinstance(node_id, bool) or not 0 < node_id < 2**32:
        raise PortalProtocolError("invalid PipeWire node")
    return node_id


def _parse_row(value: str) -> tuple[str, tuple[int, int, int, int]]:
    label, separator, coordinates = value.partition(":")
    if not separator or label not in {"self", "peer", "peer_continuation"}:
        raise ValueError("row label is invalid")
    parts = coordinates.split(",")
    if len(parts) != 4:
        raise ValueError("row coordinates are invalid")
    try:
        x, y, width, height = (int(part) for part in parts)
    except ValueError:
        raise ValueError("row coordinates are invalid") from None
    if not 0 <= x <= 32_768 or not 0 <= y <= 32_768:
        raise ValueError("row origin is invalid")
    if not 1 <= width <= 32_768 or not 1 <= height <= 32_768:
        raise ValueError("row size is invalid")
    return label, (x, y, width, height)


@dataclass(frozen=True, slots=True)
class _RowLayout:
    fingerprint: str
    bubble_box: tuple[int, int, int, int]


def _label_crop_png(
    rgba: bytes | memoryview,
    *,
    frame_width: int,
    frame_height: int,
    stride: int,
    row: tuple[int, int, int, int],
    layout: _RowLayout,
) -> bytes | None:
    _row_x, row_y, _row_width, _row_height = row
    bubble_x, bubble_y, bubble_width, _bubble_height = layout.bubble_box
    crop_left = bubble_x
    crop_right = min(frame_width, crop_left + min(bubble_width, 512))
    crop_bottom = bubble_y
    crop_top = max(row_y, crop_bottom - 128)
    crop_width = crop_right - crop_left
    crop_height = crop_bottom - crop_top
    if crop_width < 8 or crop_height < 8 or crop_bottom > frame_height:
        return None
    compact = bytearray(crop_width * crop_height * 4)
    target_offset = 0
    for pixel_y in range(crop_top, crop_bottom):
        source_offset = pixel_y * stride + crop_left * 4
        row_bytes = crop_width * 4
        compact[target_offset : target_offset + row_bytes] = rgba[
            source_offset : source_offset + row_bytes
        ]
        target_offset += row_bytes
    image_module = importlib.import_module("PIL.Image")
    image = image_module.frombytes("RGBA", (crop_width, crop_height), bytes(compact)).convert("RGB")
    output = io.BytesIO()
    image.save(output, format="PNG")
    payload = output.getvalue()
    if len(payload) > 4 * 1024 * 1024:
        raise PortalCaptureError("label crop is too large")
    return payload


def _local_label_ocr(
    png: bytes | None,
    *,
    python_executable: str,
    session_salt: bytes,
    session_ref: str,
) -> dict[str, object]:
    if png is None:
        return {
            "text_count": 0,
            "ambiguous": False,
            "unverified_display_sender": None,
        }
    executable = Path(python_executable)
    worker = Path(__file__).with_name("local_ocr_worker.py")
    if not executable.is_absolute() or not executable.is_file() or not worker.is_file():
        raise PortalCaptureError("local OCR runtime is unavailable")
    completed = subprocess.run(  # noqa: S603
        [
            str(executable),
            "-I",
            str(worker),
            "--session-salt",
            session_salt.hex(),
            "--session-ref",
            session_ref,
        ],
        check=False,
        input=png,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={"OMP_NUM_THREADS": "1", "PYTHONNOUSERSITE": "1"},
        timeout=60,
    )
    try:
        result = json.loads(completed.stdout.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError):
        raise PortalCaptureError("local OCR response is invalid") from None
    if completed.returncode != 0 or not isinstance(result, dict):
        raise PortalCaptureError("local OCR failed")
    count = result.get("text_count")
    ambiguous = result.get("ambiguous")
    sender = result.get("unverified_display_sender")
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or not 0 <= count <= 16
        or not isinstance(ambiguous, bool)
        or (
            sender is not None
            and (
                not isinstance(sender, str)
                or re.fullmatch(r"uds_[0-9a-f]{64}", sender) is None
            )
        )
    ):
        raise PortalCaptureError("local OCR response is unsafe")
    return {
        "text_count": count,
        "ambiguous": ambiguous,
        "unverified_display_sender": sender,
    }


def _row_layout(
    rgba: bytes | memoryview,
    *,
    frame_width: int,
    frame_height: int,
    stride: int,
    row: tuple[int, int, int, int],
) -> _RowLayout:
    """Hash a conservative non-text horizontal anchor for one canary row."""
    x, y, width, height = row
    if (
        stride < frame_width * 4
        or len(rgba) < stride * frame_height
        or x + width > frame_width
        or y + height > frame_height
    ):
        raise PortalCaptureError("row does not fit the frame")

    buckets = min(64, width)
    x_step = max(1, width // 256)
    y_step = max(1, height // 32)
    colors: Counter[tuple[int, int, int]] = Counter()
    for sample_y in range(y, y + height, y_step):
        row_offset = sample_y * stride
        for sample_x in range(x, x + width, x_step):
            offset = row_offset + sample_x * 4
            colors[(rgba[offset] // 8, rgba[offset + 1] // 8, rgba[offset + 2] // 8)] += 1
    if not colors:
        raise PortalCaptureError("row has no pixels")
    background_bin, _count = colors.most_common(1)[0]
    background = tuple(value * 8 + 4 for value in background_bin)

    active: list[bool] = []
    for bucket in range(buckets):
        left = x + bucket * width // buckets
        right = x + (bucket + 1) * width // buckets
        bucket_x_step = max(1, (right - left) // 4)
        changed = 0
        sampled = 0
        for sample_y in range(y, y + height, y_step):
            row_offset = sample_y * stride
            for sample_x in range(left, max(left + 1, right), bucket_x_step):
                offset = row_offset + sample_x * 4
                difference = sum(
                    abs(int(rgba[offset + channel]) - background[channel])
                    for channel in range(3)
                )
                changed += difference >= 18
                sampled += 1
        active.append(sampled > 0 and changed / sampled >= 0.30)

    # Bubble corners may leave one inactive bucket. Join only a single hole;
    # larger gaps continue to separate avatars and controls from the bubble.
    for index in range(1, len(active) - 1):
        if not active[index] and active[index - 1] and active[index + 1]:
            active[index] = True
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, present in enumerate((*active, False)):
        if present and start is None:
            start = index
        elif not present and start is not None:
            if index - start >= 2:
                runs.append((start, index - 1))
            start = None
    if not runs:
        raise PortalCaptureError("no stable row region")
    ranked = sorted(runs, key=lambda run: run[1] - run[0] + 1, reverse=True)
    largest = ranked[0]
    largest_width = largest[1] - largest[0] + 1
    if len(ranked) > 1:
        second_width = ranked[1][1] - ranked[1][0] + 1
        if second_width * 4 >= largest_width * 3:
            raise PortalCaptureError("row region is ambiguous")
    left_margin = largest[0]
    right_margin = buckets - 1 - largest[1]
    if abs(left_margin - right_margin) < max(2, buckets // 16):
        raise PortalCaptureError("row region has no stable edge")
    anonymous_edge = "edge_a" if left_margin < right_margin else "edge_b"
    edge_distance_bucket = min(left_margin, right_margin) * 16 // buckets
    canonical = f"visual-row-anchor-v1|{anonymous_edge}|{edge_distance_bucket}"
    bubble_left = x + largest[0] * width // buckets
    bubble_right = x + (largest[1] + 1) * width // buckets

    vertical_buckets = min(32, height)
    vertical_active: list[bool] = []
    vertical_x_step = max(1, (bubble_right - bubble_left) // 32)
    for bucket in range(vertical_buckets):
        top = y + bucket * height // vertical_buckets
        bottom = y + (bucket + 1) * height // vertical_buckets
        bucket_y_step = max(1, (bottom - top) // 2)
        changed = 0
        sampled = 0
        for sample_y in range(top, max(top + 1, bottom), bucket_y_step):
            row_offset = sample_y * stride
            for sample_x in range(bubble_left, bubble_right, vertical_x_step):
                offset = row_offset + sample_x * 4
                difference = sum(
                    abs(int(rgba[offset + channel]) - background[channel])
                    for channel in range(3)
                )
                changed += difference >= 18
                sampled += 1
        vertical_active.append(sampled > 0 and changed / sampled >= 0.30)
    vertical_runs: list[tuple[int, int]] = []
    vertical_start: int | None = None
    for index, present in enumerate((*vertical_active, False)):
        if present and vertical_start is None:
            vertical_start = index
        elif not present and vertical_start is not None:
            if index - vertical_start >= 2:
                vertical_runs.append((vertical_start, index - 1))
            vertical_start = None
    if not vertical_runs:
        raise PortalCaptureError("row region has no vertical body")
    vertical = max(vertical_runs, key=lambda run: run[1] - run[0] + 1)
    bubble_top = y + vertical[0] * height // vertical_buckets
    bubble_bottom = y + (vertical[1] + 1) * height // vertical_buckets
    return _RowLayout(
        fingerprint=hashlib.sha256(canonical.encode("ascii")).hexdigest(),
        bubble_box=(
            bubble_left,
            bubble_top,
            bubble_right - bubble_left,
            bubble_bottom - bubble_top,
        ),
    )


def _row_layout_fingerprint(
    rgba: bytes | memoryview,
    *,
    frame_width: int,
    frame_height: int,
    stride: int,
    row: tuple[int, int, int, int],
) -> str:
    return _row_layout(
        rgba,
        frame_width=frame_width,
        frame_height=frame_height,
        stride=stride,
        row=row,
    ).fingerprint


@dataclass(frozen=True, slots=True)
class _PortalSession:
    handle: str
    node_id: int
    pipewire_fd: int


class _PortalClient:
    def __init__(self, *, timeout_seconds: int) -> None:
        self._gio, self._glib, _gst = _load_gi()
        self._timeout_ms = timeout_seconds * 1000
        self._connection = self._gio.bus_get_sync(self._gio.BusType.SESSION, None)
        unique_name = self._connection.get_unique_name()
        if not unique_name:
            raise PortalProtocolError("session bus has no unique name")
        self._unique_name = str(unique_name)

    def _request(self, method: str, parameters: Any, token: str) -> dict[str, Any]:
        expected_path = _request_path(self._unique_name, token)
        response: dict[str, Any] = {}
        loop = self._glib.MainLoop()
        timed_out = False

        def receive(
            _connection: Any,
            _sender: str,
            object_path: str,
            _interface: str,
            _signal: str,
            parameters_value: Any,
            _user_data: Any,
        ) -> None:
            if object_path != expected_path or response:
                return
            code, values = parameters_value.unpack()
            response["code"] = int(code)
            response["values"] = values
            loop.quit()

        def expire() -> bool:
            nonlocal timed_out
            timed_out = True
            loop.quit()
            return False

        subscription = self._connection.signal_subscribe(
            PORTAL_BUS,
            REQUEST_IFACE,
            "Response",
            expected_path,
            None,
            self._gio.DBusSignalFlags.NONE,
            receive,
            None,
        )
        timeout_source = self._glib.timeout_add(self._timeout_ms, expire)
        try:
            reply = self._connection.call_sync(
                PORTAL_BUS,
                PORTAL_PATH,
                SCREENCAST_IFACE,
                method,
                parameters,
                self._glib.VariantType.new("(o)"),
                self._gio.DBusCallFlags.NONE,
                self._timeout_ms,
                None,
            )
            returned_path = str(reply.unpack()[0])
            if returned_path != expected_path:
                raise PortalProtocolError("unexpected request handle")
            loop.run()
        finally:
            self._connection.signal_unsubscribe(subscription)
            if not timed_out:
                self._glib.source_remove(timeout_source)
        if timed_out or not response:
            raise PortalTimeout("portal response timed out")
        if response["code"] != 0:
            raise PortalDenied("portal request was not approved")
        values = response["values"]
        if not isinstance(values, dict):
            raise PortalProtocolError("invalid portal response")
        return values

    def open(self) -> _PortalSession:
        create_token = f"lb_{secrets.token_hex(12)}"
        session_token = f"lb_{secrets.token_hex(12)}"
        created = self._request(
            "CreateSession",
            self._glib.Variant(
                "(a{sv})",
                (
                    {
                        "handle_token": self._glib.Variant("s", create_token),
                        "session_handle_token": self._glib.Variant("s", session_token),
                    },
                ),
            ),
            create_token,
        )
        session_handle = _unpack(created.get("session_handle"))
        if not isinstance(session_handle, str) or not session_handle.startswith(
            "/org/freedesktop/portal/desktop/session/"
        ):
            raise PortalProtocolError("invalid session handle")
        try:
            select_token = f"lb_{secrets.token_hex(12)}"
            self._request(
                "SelectSources",
                self._glib.Variant(
                    "(oa{sv})",
                    (
                        session_handle,
                        {
                            "handle_token": self._glib.Variant("s", select_token),
                            "types": self._glib.Variant("u", 2),
                            "multiple": self._glib.Variant("b", False),
                            "cursor_mode": self._glib.Variant("u", 1),
                            "persist_mode": self._glib.Variant("u", 0),
                        },
                    ),
                ),
                select_token,
            )

            start_token = f"lb_{secrets.token_hex(12)}"
            started = self._request(
                "Start",
                self._glib.Variant(
                    "(osa{sv})",
                    (
                        session_handle,
                        "",
                        {"handle_token": self._glib.Variant("s", start_token)},
                    ),
                ),
                start_token,
            )
            node_id = _single_stream(started)
            reply, fd_list = self._connection.call_with_unix_fd_list_sync(
                PORTAL_BUS,
                PORTAL_PATH,
                SCREENCAST_IFACE,
                "OpenPipeWireRemote",
                self._glib.Variant("(oa{sv})", (session_handle, {})),
                self._glib.VariantType.new("(h)"),
                self._gio.DBusCallFlags.NONE,
                self._timeout_ms,
                None,
                None,
            )
            fd_index = reply.unpack()[0]
            pipewire_fd = fd_list.get(fd_index)
            if pipewire_fd < 0:
                raise PortalProtocolError("invalid PipeWire descriptor")
            return _PortalSession(session_handle, node_id, pipewire_fd)
        except Exception:
            with suppress(Exception):
                self.close(session_handle)
            raise

    def close(self, session_handle: str) -> None:
        self._connection.call_sync(
            PORTAL_BUS,
            session_handle,
            SESSION_IFACE,
            "Close",
            None,
            None,
            self._gio.DBusCallFlags.NONE,
            self._timeout_ms,
            None,
        )


def _consume_frames(
    session: _PortalSession,
    *,
    frame_count: int,
    timeout_seconds: int,
    rows: dict[str, tuple[int, int, int, int]],
    ocr_python: str | None,
) -> dict[str, Any]:
    _gio, _glib, gst = _load_gi()
    gst.init(None)
    pipeline = gst.parse_launch(
        "pipewiresrc name=portal_source do-timestamp=true ! "
        "videoconvert ! video/x-raw,format=RGBA ! "
        "appsink name=frame_sink sync=false drop=true max-buffers=1"
    )
    source = pipeline.get_by_name("portal_source")
    sink = pipeline.get_by_name("frame_sink")
    if source is None or sink is None:
        raise PortalCaptureError("capture elements unavailable")
    source.set_property("fd", session.pipewire_fd)
    source.set_property("path", str(session.node_id))
    formats: set[str] = set()
    sizes: set[tuple[int, int]] = set()
    row_fingerprints: dict[str, set[str]] = {label: set() for label in rows}
    ocr_results: dict[str, dict[str, object]] = {}
    session_salt = secrets.token_bytes(32)
    session_ref = f"visual_{secrets.token_hex(12)}"
    seen = 0
    try:
        if pipeline.set_state(gst.State.PLAYING) == gst.StateChangeReturn.FAILURE:
            raise PortalCaptureError("capture pipeline failed to start")
        timeout_ns = timeout_seconds * gst.SECOND
        while seen < frame_count:
            sample = sink.emit("try-pull-sample", timeout_ns)
            if sample is None:
                raise PortalTimeout("frame acquisition timed out")
            caps = sample.get_caps()
            structure = caps.get_structure(0) if caps is not None else None
            if structure is None:
                raise PortalCaptureError("frame caps unavailable")
            width = int(structure.get_value("width") or 0)
            height = int(structure.get_value("height") or 0)
            pixel_format = str(structure.get_value("format") or "")
            if not 1 <= width <= 16_384 or not 1 <= height <= 16_384 or pixel_format != "RGBA":
                raise PortalCaptureError("unsafe frame geometry")
            sizes.add((width, height))
            formats.add(pixel_format)
            if rows:
                buffer = sample.get_buffer()
                mapped, map_info = buffer.map(gst.MapFlags.READ)
                if not mapped:
                    raise PortalCaptureError("frame mapping failed")
                try:
                    stride = buffer.get_size() // height
                    for label, row in rows.items():
                        layout = _row_layout(
                            map_info.data,
                            frame_width=width,
                            frame_height=height,
                            stride=stride,
                            row=row,
                        )
                        row_fingerprints[label].add(layout.fingerprint)
                        if seen == 0 and ocr_python and label in {"peer", "peer_continuation"}:
                            crop = _label_crop_png(
                                map_info.data,
                                frame_width=width,
                                frame_height=height,
                                stride=stride,
                                row=row,
                                layout=layout,
                            )
                            ocr_results[label] = _local_label_ocr(
                                crop,
                                python_executable=ocr_python,
                                session_salt=session_salt,
                                session_ref=session_ref,
                            )
                finally:
                    buffer.unmap(map_info)
            seen += 1
    finally:
        pipeline.set_state(gst.State.NULL)
    if any(len(values) != 1 for values in row_fingerprints.values()):
        raise PortalCaptureError("row layout changed during capture")
    result: dict[str, Any] = {
        "frames_seen": seen,
        "frame_sizes": [list(value) for value in sorted(sizes)],
        "pixel_formats": sorted(formats),
    }
    if row_fingerprints:
        result["row_layout_fingerprints"] = {
            label: next(iter(values)) for label, values in sorted(row_fingerprints.items())
        }
    if ocr_python:
        peer = ocr_results.get("peer", {})
        continuation = ocr_results.get("peer_continuation", {})
        peer_sender = peer.get("unverified_display_sender")
        segment_proven = (
            peer.get("text_count") == 1
            and peer.get("ambiguous") is False
            and isinstance(peer_sender, str)
        )
        continuation_proven = (
            continuation.get("text_count") == 0
            and continuation.get("ambiguous") is False
            and continuation.get("unverified_display_sender") is None
        )
        result.update(
            {
                "segment_label_anchor_proven": segment_proven,
                "continuation_binding_proven": continuation_proven,
                "unverified_display_sender": peer_sender if segment_proven else None,
            }
        )
    return result


def portal_probe(
    *,
    frame_count: int,
    timeout_seconds: int,
    rows: dict[str, tuple[int, int, int, int]] | None = None,
    ocr_python: str | None = None,
) -> dict[str, Any]:
    if not 1 <= frame_count <= 30 or not 5 <= timeout_seconds <= 120:
        raise ValueError("probe bounds are outside the safe range")
    client = _PortalClient(timeout_seconds=timeout_seconds)
    session: _PortalSession | None = None
    try:
        session = client.open()
        frame_facts = _consume_frames(
            session,
            frame_count=frame_count,
            timeout_seconds=timeout_seconds,
            rows=rows or {},
            ocr_python=ocr_python,
        )
    finally:
        if session is not None:
            try:
                client.close(session.handle)
            finally:
                os.close(session.pipewire_fd)
    return {
        "schema_version": 1,
        "capture_source": "xdg-desktop-portal",
        "source_kind": "window",
        "cursor_included": False,
        "pixels_persisted": False,
        "selected_stream_count": 1,
        **frame_facts,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Consent-bound Portal ScreenCast probe")
    parser.add_argument("--frames", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("--row", action="append", default=[])
    parser.add_argument("--ocr-python")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        row_values = dict(_parse_row(value) for value in arguments.row)
        if len(row_values) != len(arguments.row):
            raise ValueError("row labels must be unique")
        report = portal_probe(
            frame_count=arguments.frames,
            timeout_seconds=arguments.timeout_seconds,
            rows=row_values,
            ocr_python=arguments.ocr_python,
        )
    except Exception as exc:
        print(json.dumps({"error": _safe_error_code(exc)}, sort_keys=True))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
