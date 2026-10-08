from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from lemonbot import cli
from lemonbot.cli import app
from lemonbot.research import portal_capture
from lemonbot.research.portal_capture import (
    PortalCaptureError,
    PortalProtocolError,
    _label_crop_png,
    _local_label_ocr,
    _parse_row,
    _request_path,
    _row_layout,
    _row_layout_fingerprint,
    _safe_error_code,
    _single_stream,
)


def test_portal_request_path_is_bound_to_dbus_sender_and_random_token() -> None:
    assert _request_path(":1.42", "lb_test") == (
        "/org/freedesktop/portal/desktop/request/1_42/lb_test"
    )
    with pytest.raises(PortalProtocolError):
        _request_path(":1.42", "../unsafe")


def test_portal_accepts_exactly_one_pipewire_stream() -> None:
    assert _single_stream({"streams": [(42, {"source_type": 2})]}) == 42
    with pytest.raises(PortalProtocolError):
        _single_stream({"streams": [(1, {}), (2, {})]})
    with pytest.raises(PortalProtocolError):
        _single_stream({"streams": [(True, {})]})


def test_portal_capture_details_map_only_to_fixed_safe_codes() -> None:
    assert _safe_error_code(PortalCaptureError("row does not fit the frame")) == (
        "PortalRowOutOfFrame"
    )
    assert _safe_error_code(PortalCaptureError("private screen detail")) == (
        "PortalCaptureError"
    )


def _rgba_frame(width: int, height: int, color: tuple[int, int, int]) -> bytearray:
    return bytearray((*color, 255)) * (width * height)


def _fill(
    frame: bytearray,
    frame_width: int,
    box: tuple[int, int, int, int],
    color: tuple[int, int, int],
) -> None:
    x, y, width, height = box
    for pixel_y in range(y, y + height):
        for pixel_x in range(x, x + width):
            offset = (pixel_y * frame_width + pixel_x) * 4
            frame[offset : offset + 4] = bytes((*color, 255))


def test_row_layout_uses_canary_calibrated_edge_not_message_width_or_color() -> None:
    width, height = 640, 180
    frame = _rgba_frame(width, height, (245, 245, 245))
    _fill(frame, width, (70, 20, 230, 45), (255, 255, 255))
    _fill(frame, width, (410, 105, 160, 45), (100, 205, 80))

    peer = _row_layout_fingerprint(
        frame,
        frame_width=width,
        frame_height=height,
        stride=width * 4,
        row=(0, 10, width, 65),
    )
    self_value = _row_layout_fingerprint(
        frame,
        frame_width=width,
        frame_height=height,
        stride=width * 4,
        row=(0, 95, width, 65),
    )
    assert peer != self_value

    resized = _rgba_frame(width, height, (245, 245, 245))
    _fill(resized, width, (70, 20, 150, 45), (235, 225, 210))
    assert (
        _row_layout_fingerprint(
            resized,
            frame_width=width,
            frame_height=height,
            stride=width * 4,
            row=(0, 10, width, 65),
        )
        == peer
    )


def test_row_layout_fails_closed_on_centered_or_out_of_frame_region() -> None:
    width, height = 320, 80
    frame = _rgba_frame(width, height, (245, 245, 245))
    _fill(frame, width, (100, 10, 120, 50), (100, 205, 80))
    with pytest.raises(PortalCaptureError):
        _row_layout_fingerprint(
            frame,
            frame_width=width,
            frame_height=height,
            stride=width * 4,
            row=(0, 0, width, height),
        )
    with pytest.raises(PortalCaptureError):
        _row_layout_fingerprint(
            frame,
            frame_width=width,
            frame_height=height,
            stride=width * 4,
            row=(0, 40, width, height),
        )


def test_label_crop_is_only_the_region_above_the_detected_bubble() -> None:
    width, height = 320, 100
    frame = _rgba_frame(width, height, (245, 245, 245))
    _fill(frame, width, (40, 30, 180, 50), (255, 255, 255))
    layout = _row_layout(
        frame,
        frame_width=width,
        frame_height=height,
        stride=width * 4,
        row=(0, 10, width, 80),
    )

    crop = _label_crop_png(
        frame,
        frame_width=width,
        frame_height=height,
        stride=width * 4,
        row=(0, 10, width, 80),
        layout=layout,
    )

    assert crop is not None and crop.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(crop) < len(frame)


def test_local_label_ocr_subprocess_returns_only_validated_minimal_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = tmp_path / "python"
    python.write_bytes(b"executable placeholder")
    monkeypatch.setattr(
        portal_capture.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "text_count": 1,
                    "ambiguous": False,
                    "unverified_display_sender": "uds_" + "c" * 64,
                }
            ).encode(),
            stderr=b"raw OCR detail",
        ),
    )

    result = _local_label_ocr(
        b"\x89PNG\r\n\x1a\nminimal",
        python_executable=str(python.resolve()),
        session_salt=b"a" * 32,
        session_ref="visual_test",
    )

    assert result == {
        "text_count": 1,
        "ambiguous": False,
        "unverified_display_sender": "uds_" + "c" * 64,
    }


def test_row_argument_has_only_fixed_canary_labels_and_integer_geometry() -> None:
    assert _parse_row("self:1,2,3,4") == ("self", (1, 2, 3, 4))
    with pytest.raises(ValueError):
        _parse_row("sender-name:1,2,3,4")


def test_missing_label_crop_cannot_prove_absence_of_sender(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("no crop must not start an OCR worker")

    monkeypatch.setattr(portal_capture.subprocess, "run", forbidden)
    assert _local_label_ocr(
        None, python_executable="unused", session_salt=b"a" * 32, session_ref="test",
    ) == {"text_count": 0, "ambiguous": True, "unverified_display_sender": None}


def test_portal_probe_command_inherits_only_graphical_session_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/test/bus")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-cross")

    command, environment = cli._portal_probe_command(2, 60)

    assert command[0:2] == ["/usr/bin/python3", "-I"]
    assert environment["WAYLAND_DISPLAY"] == "wayland-0"
    assert "DEEPSEEK_API_KEY" not in environment


def test_portal_cli_hides_child_error_details(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_portal_probe_command", lambda _frames, _timeout: (["probe"], {}))
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=json.dumps(
                {"error": "PortalProtocolError", "untrusted": "screen detail"}
            ).encode(),
            stderr=b"raw portal failure",
        ),
    )

    result = CliRunner().invoke(app, ["channel", "linux-portal-screen-probe"])

    assert result.exit_code == 1
    assert "PortalProtocolError" in result.output
    assert "screen detail" not in result.output
    assert "raw portal failure" not in result.output


def _semantic_report(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "group",
                "self_match_count": 1,
                "inbound_match_count": 1,
                "inbound_continuation_match_count": 1,
                "header_proven": True,
                "self_evidence": [
                    {
                        "item_path": [0, 2],
                        "item_window_extents": [0, 100, 640, 120],
                        "preceding_sibling_window_extents": [0, 100, 640, 60],
                    }
                ],
                "inbound_evidence": [
                    {
                        "item_path": [0, 3],
                        "item_window_extents": [0, 100, 640, 180],
                        "preceding_sibling_window_extents": [0, 100, 640, 120],
                    }
                ],
                "inbound_continuation_evidence": [
                    {
                        "item_path": [0, 5],
                        "item_window_extents": [0, 100, 640, 240],
                        "preceding_sibling_window_extents": [0, 100, 640, 210],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _capture_report(frames: int = 3) -> dict[str, object]:
    return {
        "schema_version": 1,
        "capture_source": "xdg-desktop-portal",
        "source_kind": "window",
        "portal_authorized": True,
        "local_processing_only": True,
        "cloud_processing_used": False,
        "cursor_included": False,
        "pixels_persisted": False,
        "selected_stream_count": 1,
        "frames_seen": frames,
        "frame_sizes": [[640, 480]],
        "pixel_formats": ["RGBA"],
    }


_UNSAFE_CAPTURE_CHANGES: list[dict[str, object]] = [
    {"schema_version": True},
    {"capture_source": "private-source"},
    {"source_kind": "monitor"},
    {"portal_authorized": None},
    {"local_processing_only": False},
    {"cloud_processing_used": True},
    {"cursor_included": True},
    {"pixels_persisted": True},
    {"selected_stream_count": 2},
    {"selected_stream_count": True},
    {"frames_seen": 1},
    {"frame_sizes": [[640, 480], [800, 600]]},
    {"frame_sizes": [[True, 480]]},
    {"frame_sizes": [[640, 0]]},
    {"pixel_formats": ["secret-format"]},
]


@pytest.mark.parametrize("changes", [{}, *_UNSAFE_CAPTURE_CHANGES])
def test_group_calibration_writes_only_private_fail_closed_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changes: dict[str, object],
) -> None:
    semantic = (tmp_path / "semantic.json").resolve()
    output = (tmp_path / "visual.json").resolve()
    _semantic_report(semantic)
    captured_rows: dict[str, tuple[int, int, int, int]] = {}

    def command(
        _frames: int,
        _timeout: int,
        rows: dict[str, tuple[int, int, int, int]] | None = None,
        _ocr_python: str | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        captured_rows.update(rows or {})
        return ["probe"], {}

    monkeypatch.setattr(cli, "_portal_probe_command", command)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    **_capture_report(),
                    **changes,
                    "untrusted": "private-screen-content",
                    "row_layout_fingerprints": {
                        "self": "a" * 64,
                        "peer": "b" * 64,
                        "peer_continuation": "b" * 64,
                    },
                    "segment_label_anchor_proven": True,
                    "continuation_binding_proven": True,
                    "unverified_display_sender": "uds_" + "c" * 64,
                }
            ).encode(),
            stderr=b"must not be copied",
        ),
    )

    result = CliRunner().invoke(
        app,
        [
            "channel",
            "linux-portal-group-calibration",
            "--semantic-report",
            str(semantic),
            "--output",
            str(output),
            "--confirm-restart",
            "--confirm-lock-cycle",
        ],
    )

    assert "private-screen-content" not in result.output
    if changes:
        assert result.exit_code == 1, result.output
        assert not output.exists()
        return
    assert result.exit_code == 0, result.output
    assert captured_rows == {
        "self": (0, 160, 640, 60),
        "peer": (0, 220, 640, 60),
        "peer_continuation": (0, 310, 640, 30),
    }
    report = json.loads(output.read_text(encoding="ascii"))
    assert report["passed"] is False
    assert report["capture_facts"] == _capture_report()
    assert report["calibration_sample"]["portal_authorized"] is True
    assert report["calibration_sample"]["segment_label_anchor_proven"] is True
    assert report["calibration_sample"]["continuation_binding_proven"] is True
    assert report["reason_codes"] == ["requires_second_round"]
    assert report["unverified_display_sender"] == "uds_" + "c" * 64
    assert output.stat().st_mode & 0o077 == 0
    assert "item_window_extents" not in output.read_text(encoding="ascii")
    assert "private-screen-content" not in output.read_text(encoding="ascii")


@pytest.mark.parametrize("changes", [{}, *_UNSAFE_CAPTURE_CHANGES])
def test_screen_probe_validates_and_filters_success_report(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, object],
) -> None:
    monkeypatch.setattr(cli, "_portal_probe_command", lambda *_args: (["probe"], {}))
    child_report = {**_capture_report(2), **changes, "untrusted": "private-screen-content"}
    monkeypatch.setattr(
        cli.subprocess, "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(child_report).encode(),
        ),
    )
    result = CliRunner().invoke(app, ["channel", "linux-portal-screen-probe"])
    assert "private-screen-content" not in result.output
    if changes:
        assert result.exit_code == 1
    else:
        assert result.exit_code == 0
        assert json.loads(result.output) == _capture_report(2)


def test_screen_probe_hides_malformed_error_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_portal_probe_command", lambda *_args: (["probe"], {}))
    monkeypatch.setattr(
        cli.subprocess, "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=1, stdout=b'{"error": ["private-screen-content"]}',
        ),
    )
    result = CliRunner().invoke(app, ["channel", "linux-portal-screen-probe"])
    assert result.exit_code == 1
    assert "private-screen-content" not in result.output
    assert "PortalProbeError" in result.output


def test_group_calibration_rejects_semantic_report_without_private_permissions(
    tmp_path: Path,
) -> None:
    semantic = (tmp_path / "semantic.json").resolve()
    output = (tmp_path / "visual.json").resolve()
    _semantic_report(semantic)
    semantic.chmod(0o644)

    result = CliRunner().invoke(
        app,
        [
            "channel",
            "linux-portal-group-calibration",
            "--semantic-report",
            str(semantic),
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 1
    assert not output.exists()


def _visual_report(path: Path, run_ref: str, *, restart: bool, lock_cycle: bool) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "group",
                "passed": False,
                "capture_facts": _capture_report(),
                "calibration_sample": {
                    "schema_version": 1,
                    "run_ref": run_ref,
                    "portal_authorized": True,
                    "capture_source": "xdg-desktop-portal",
                    "local_processing_only": True,
                    "cloud_processing_used": False,
                    "client_restart_observed": restart,
                    "lock_cycle_observed": lock_cycle,
                    "self_layout_fingerprint": "a" * 64,
                    "peer_layout_fingerprint": "b" * 64,
                    "segment_label_anchor_proven": True,
                    "continuation_binding_proven": True,
                    "ambiguous": False,
                },
            }
        ),
        encoding="ascii",
    )
    path.chmod(0o600)


def test_two_round_visual_verification_still_grants_no_runtime_capability(
    tmp_path: Path,
) -> None:
    first = (tmp_path / "first.json").resolve()
    second = (tmp_path / "second.json").resolve()
    output = (tmp_path / "verified.json").resolve()
    _visual_report(first, "restart_round", restart=True, lock_cycle=False)
    _visual_report(second, "lock_round", restart=False, lock_cycle=True)

    result = CliRunner().invoke(
        app,
        [
            "channel",
            "linux-portal-group-calibration-verify",
            "--report",
            str(first),
            "--report",
            str(second),
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    verified = json.loads(output.read_text(encoding="ascii"))
    assert verified["passed"] is True
    assert verified["decision"]["identity_authorized"] is False
    assert verified["decision"]["connector_enrollment_allowed"] is False
    assert verified["decision"]["reply_generation_allowed"] is False
    assert output.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("change", ["missing_capture", "one_frame", "resize", "string_boolean"])
def test_visual_verification_rejects_incomplete_or_coerced_evidence(
    tmp_path: Path, change: str,
) -> None:
    first, second, output = (tmp_path / name for name in ("one.json", "two.json", "out.json"))
    _visual_report(first, "restart", restart=True, lock_cycle=False)
    _visual_report(second, "lock", restart=False, lock_cycle=True)
    report = json.loads(second.read_text())
    if change == "missing_capture":
        report.pop("capture_facts")
    elif change == "one_frame":
        report["capture_facts"]["frames_seen"] = 1
    elif change == "resize":
        report["capture_facts"]["frame_sizes"].append([800, 600])
    else:
        report["calibration_sample"]["lock_cycle_observed"] = "true"
    second.write_text(json.dumps(report))

    result = CliRunner().invoke(
        app, ["channel", "linux-portal-group-calibration-verify",
              "--report", str(first), "--report", str(second), "--output", str(output)],
    )
    assert result.exit_code == 1
    assert not output.exists()


@pytest.mark.parametrize("resize", [False, True])
def test_capture_stops_pipeline_on_frame_size_change(
    monkeypatch: pytest.MonkeyPatch, resize: bool,
) -> None:
    states: list[int] = []
    pulled = 0

    def pull_sample(_signal: str, _timeout: int) -> SimpleNamespace:
        nonlocal pulled
        pulled += 1
        values = {"width": 800 if resize and pulled > 1 else 640,
                  "height": 480, "format": "RGBA"}
        structure = SimpleNamespace(get_value=values.get)
        caps = SimpleNamespace(get_structure=lambda _index: structure)
        return SimpleNamespace(get_caps=lambda: caps)

    source = SimpleNamespace(set_property=lambda *_args: None)
    sink = SimpleNamespace(emit=pull_sample)
    pipeline = SimpleNamespace(
        get_by_name=lambda name: source if name == "portal_source" else sink,
        set_state=lambda state: states.append(state),
    )
    gst = SimpleNamespace(
        init=lambda _args: None, parse_launch=lambda _description: pipeline,
        State=SimpleNamespace(PLAYING=1, NULL=0),
        StateChangeReturn=SimpleNamespace(FAILURE=-1), SECOND=1_000_000_000,
    )
    monkeypatch.setattr(portal_capture, "_load_gi", lambda: (None, None, gst))
    session = portal_capture._PortalSession("private-session", 42, 123)
    if resize:
        with pytest.raises(PortalCaptureError) as caught:
            portal_capture._consume_frames(
                session, frame_count=3, timeout_seconds=5, rows={}, ocr_python=None,
            )
        assert _safe_error_code(caught.value) == "PortalFrameChanged"
        assert pulled == 2
    else:
        report = portal_capture._consume_frames(
            session, frame_count=3, timeout_seconds=5, rows={}, ocr_python=None,
        )
        assert report["frames_seen"] == 3
        assert report["frame_sizes"] == [[640, 480]]
    assert states == [1, 0]
