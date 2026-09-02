from __future__ import annotations

import asyncio
import getpass
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Literal

import psutil  # type: ignore[import-untyped]
import typer
from pydantic import ValidationError

from lemonbot.backup import BackupError, create_backup, restore_backup
from lemonbot.config import RuntimePaths, load_settings
from lemonbot.config.settings import default_config_path
from lemonbot.data import DataOperationError, delete_conversation, export_profile_data
from lemonbot.doctor import run_checks
from lemonbot.domain import InboundEvent
from lemonbot.orchestration import EventPipeline, FakeModelBackend
from lemonbot.policy import DeterministicPolicy
from lemonbot.runtime_lock import AlreadyRunningError, RuntimeLock
from lemonbot.safety_gates import AT_SPI_DIRECTION_GATE_OPEN
from lemonbot.security.secrets import (
    NamespacedSecretStore,
    SecretStoreError,
    platform_secret_store,
)

app = typer.Typer(no_args_is_help=True, help="Lemonbot Linux-only runtime")
secret_app = typer.Typer(no_args_is_help=True, help="Manage Linux Secret Service entries")
data_app = typer.Typer(no_args_is_help=True, help="Offline administrator data operations")
outbox_app = typer.Typer(no_args_is_help=True, help="Reconcile ambiguous outbound sends")
channel_app = typer.Typer(no_args_is_help=True, help="Inspect Linux chat channels")
app.add_typer(secret_app, name="secret")
app.add_typer(data_app, name="data")
app.add_typer(outbox_app, name="outbox")
app.add_typer(channel_app, name="channel")

ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", help="Path to a non-secret TOML configuration file"),
]
OutputOption = Annotated[Path | None, typer.Option("--output")]
SemanticOutputOption = Annotated[Path | None, typer.Option("--output")]
PrivateReportsOption = Annotated[list[Path], typer.Option("--private-report")]
GroupReportsOption = Annotated[list[Path], typer.Option("--group-report")]
EnrollmentOutputOption = Annotated[Path, typer.Option("--output")]
PrivateRefOption = Annotated[str | None, typer.Option("--private-ref")]
GroupRefOption = Annotated[str | None, typer.Option("--group-ref")]
ConfirmRestartOption = Annotated[bool, typer.Option("--confirm-restart")]
ConfirmLockOption = Annotated[bool, typer.Option("--confirm-lock-cycle")]
ConfirmTestingOption = Annotated[
    bool,
    typer.Option(
        "--confirm-testing",
        help="Confirm the visible current group is the dedicated testing group",
    ),
]
ConfirmTestingSendOption = Annotated[
    bool,
    typer.Option(
        "--confirm-testing-send",
        help="Authorize one generated canary send in the visible testing group",
    ),
]
ConfirmEmptyDraftOption = Annotated[
    bool,
    typer.Option(
        "--confirm-empty-draft",
        help="Confirm the currently visible testing-group input is empty",
    ),
]
LinuxProbePidOption = Annotated[
    list[int] | None,
    typer.Option("--pid", help="Exact WeChat PID; repeatable"),
]
LinuxProbeMaxNodesOption = Annotated[
    int,
    typer.Option("--max-nodes", min=100, max=20_000),
]
PortalFrameCountOption = Annotated[int, typer.Option("--frames", min=1, max=30)]
PortalTimeoutOption = Annotated[int, typer.Option("--timeout-seconds", min=5, max=120)]
TestingSendTimeoutOption = Annotated[int, typer.Option("--timeout-seconds", min=5, max=60)]
VisualSemanticReportOption = Annotated[Path, typer.Option("--semantic-report")]
VisualCalibrationOutputOption = Annotated[Path, typer.Option("--output")]
VisualCalibrationReportsOption = Annotated[list[Path], typer.Option("--report")]

_SECRET_NAMES = {"deepseek_api_key", "zhipu_api_key"}
_SAFE_SEMANTIC_PROBE_ERROR_CODES = frozenset(
    {
        "AttributeError",
        "EOFError",
        "IndexError",
        "KeyError",
        "RuntimeError",
        "SystemError",
        "TypeError",
        "UnicodeError",
        "ValueError",
    }
)


def _write_private_new(destination: Path, payload: bytes) -> None:
    parent = destination.parent
    if not parent.is_dir() or parent.resolve(strict=True) != parent:
        raise ValueError("private output parent must be an existing non-symlink directory")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(destination, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _wechat_pids(explicit: list[int] | None) -> tuple[int, ...]:
    expected = Path("/opt/wechat/wechat")
    try:
        expected = expected.resolve(strict=True)
    except OSError:
        raise ValueError("the official WeChat executable is unavailable") from None
    requested = set(explicit or [])
    target_pids: set[int] = set()
    for process in psutil.process_iter(("pid", "exe")):
        try:
            process_id = int(process.info["pid"])
            executable = Path(str(process.info.get("exe") or ""))
            if (not requested or process_id in requested) and executable.resolve() == expected:
                target_pids.add(process_id)
        except (OSError, psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    if requested and target_pids != requested:
        raise ValueError("an explicit PID does not belong to the official WeChat executable")
    if not target_pids or len(target_pids) > 16 or any(value <= 0 for value in target_pids):
        raise ValueError("WeChat process identity is missing or ambiguous")
    return tuple(sorted(target_pids))


def _probe_command(pids: tuple[int, ...], max_nodes: int) -> tuple[list[str], dict[str, str]]:
    if not sys.platform.startswith("linux"):
        raise ValueError("Linux AT-SPI probes require a Linux graphical session")
    helper = Path(__file__).with_name("connectors") / "linux_atspi_probe.py"
    system_python = Path("/usr/bin/python3")
    if not helper.is_file() or not system_python.is_file():
        raise OSError("AT-SPI probe runtime is unavailable")
    command = [str(system_python), "-I", str(helper)]
    for process_id in pids:
        command.extend(("--pid", str(process_id)))
    command.extend(("--max-nodes", str(max_nodes)))
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "DBUS_SESSION_BUS_ADDRESS",
            "DISPLAY",
            "LANG",
            "LC_ALL",
            "WAYLAND_DISPLAY",
            "XDG_RUNTIME_DIR",
        }
    }
    return command, environment


def _portal_probe_command(
    frame_count: int,
    timeout_seconds: int,
    rows: dict[str, tuple[int, int, int, int]] | None = None,
    ocr_python: str | None = None,
) -> tuple[list[str], dict[str, str]]:
    if not sys.platform.startswith("linux"):
        raise ValueError("Portal probes require a Linux graphical session")
    helper = Path(__file__).with_name("research") / "portal_capture.py"
    system_python = Path("/usr/bin/python3")
    if not helper.is_file() or not system_python.is_file():
        raise OSError("Portal probe runtime is unavailable")
    command = [
        str(system_python),
        "-I",
        str(helper),
        "--frames",
        str(frame_count),
        "--timeout-seconds",
        str(timeout_seconds),
    ]
    for label, extents in sorted((rows or {}).items()):
        command.extend(("--row", f"{label}:{','.join(str(value) for value in extents)}"))
    if ocr_python:
        command.extend(("--ocr-python", ocr_python))
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "DBUS_SESSION_BUS_ADDRESS",
            "DISPLAY",
            "LANG",
            "LC_ALL",
            "WAYLAND_DISPLAY",
            "XDG_CURRENT_DESKTOP",
            "XDG_RUNTIME_DIR",
            "XDG_SESSION_DESKTOP",
            "XDG_SESSION_TYPE",
        }
    }
    return command, environment


def _sanitized_testing_action_report(report: object) -> dict[str, object]:
    if not isinstance(report, dict):
        raise ValueError("testing action surface report is invalid")
    candidate = report.get("candidate")
    if not isinstance(candidate, dict):
        raise ValueError("testing action surface is missing")

    def selector(key: str) -> list[int]:
        value = candidate.get(key)
        if (
            not isinstance(value, list)
            or not value
            or len(value) > 32
            or any(
                not isinstance(part, int)
                or isinstance(part, bool)
                or not 0 <= part <= 20_000
                for part in value
            )
        ):
            raise ValueError("testing action selector is invalid")
        return value

    def count(key: str, *, maximum: int = 20_000) -> int:
        value = report.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= maximum:
            raise ValueError("testing action count is invalid")
        return value

    def role(key: str) -> str:
        value = candidate.get(key)
        if not isinstance(value, str) or not 1 <= len(value) <= 64 or not value.isascii():
            raise ValueError("testing action role is invalid")
        return value

    action_index = candidate.get("send_action_index")
    action_kind = candidate.get("send_action_kind")
    activation_proven = candidate.get("send_activation_proven")
    surface_hash = candidate.get("surface_sha256")
    if (
        not isinstance(action_index, int)
        or isinstance(action_index, bool)
        or not 0 <= action_index <= 15
        or action_kind not in {"activate", "focus_only", "unknown"}
        or not isinstance(activation_proven, bool)
        or activation_proven is not (action_kind == "activate")
        or not isinstance(surface_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", surface_hash) is None
        or report.get("schema_version") != 1
        or report.get("passed") is not True
        or report.get("actions_performed") != 0
    ):
        raise ValueError("testing action surface report is invalid")
    return {
        "schema_version": 1,
        "matched_app_count": count("matched_app_count", maximum=16),
        "testing_text_match_count": count("testing_text_match_count"),
        "send_label_match_count": count("send_label_match_count"),
        "editable_candidate_count": count("editable_candidate_count"),
        "send_action_candidate_count": count("send_action_candidate_count"),
        "candidate": {
            "title_selector": selector("title_selector"),
            "title_role": role("title_role"),
            "input_selector": selector("input_selector"),
            "input_role": role("input_role"),
            "send_selector": selector("send_selector"),
            "send_role": role("send_role"),
            "send_action_index": action_index,
            "send_action_kind": action_kind,
            "send_activation_proven": activation_proven,
            "surface_sha256": surface_hash,
        },
        "passed": True,
        "actions_performed": 0,
    }


def _sanitized_testing_send_report(report: object) -> dict[str, object]:
    if not isinstance(report, dict):
        raise ValueError("testing send report is invalid")
    surface_hash = report.get("surface_sha256")
    canary_hash = report.get("canary_sha256")
    outcome = report.get("outcome")
    commit_mechanism = report.get("commit_mechanism")
    readback_count = report.get("readback_match_count")
    extents = report.get("readback_item_window_extents")
    fixed_bools = {
        key: report.get(key)
        for key in (
            "input_was_empty",
            "operator_confirmed_empty",
            "used_existing_canary",
            "send_action_invoked",
            "send_action_returned",
            "focus_action_invoked",
            "focus_action_returned",
            "focused_before_commit",
            "keyboard_event_invoked",
            "keyboard_event_returned",
            "draft_empty_after",
            "direction_proven",
            "acknowledged",
            "retry_allowed",
        )
    }
    if (
        report.get("schema_version") != 1
        or not isinstance(surface_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", surface_hash) is None
        or not isinstance(canary_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", canary_hash) is None
        or outcome not in {"readback_unattributed", "unknown"}
        or commit_mechanism not in {"activate", "focus_only"}
        or not isinstance(readback_count, int)
        or isinstance(readback_count, bool)
        or not 0 <= readback_count <= 16
        or any(not isinstance(value, bool) for value in fixed_bools.values())
        or fixed_bools["input_was_empty"] is fixed_bools["used_existing_canary"]
        or fixed_bools["send_action_invoked"] is not True
        or fixed_bools["direction_proven"] is not False
        or fixed_bools["acknowledged"] is not False
        or fixed_bools["retry_allowed"] is not False
        or report.get("actions_performed")
        != (2 if commit_mechanism == "focus_only" else 1)
        or (
            commit_mechanism == "activate"
            and any(
                fixed_bools[key]
                for key in (
                    "focus_action_invoked",
                    "focus_action_returned",
                    "focused_before_commit",
                    "keyboard_event_invoked",
                    "keyboard_event_returned",
                )
            )
        )
        or (
            commit_mechanism == "focus_only"
            and not all(
                fixed_bools[key]
                for key in (
                    "focus_action_invoked",
                    "focus_action_returned",
                    "focused_before_commit",
                    "keyboard_event_invoked",
                )
            )
        )
    ):
        raise ValueError("testing send report is unsafe")
    safe_extents: list[int] | None = None
    if extents is not None:
        if (
            not isinstance(extents, list)
            or len(extents) != 4
            or any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 0 <= value <= 32_768
                for value in extents
            )
        ):
            raise ValueError("testing send readback geometry is invalid")
        safe_extents = extents
    return {
        "schema_version": 1,
        "surface_sha256": surface_hash,
        "canary_sha256": canary_hash,
        "commit_mechanism": commit_mechanism,
        **fixed_bools,
        "readback_match_count": readback_count,
        "readback_item_window_extents": safe_extents,
        "outcome": outcome,
        "actions_performed": report.get("actions_performed"),
    }


def _visual_canary_rows(path: Path) -> dict[str, tuple[int, int, int, int]]:
    absolute = path.expanduser()
    if absolute.is_symlink() or not absolute.is_absolute() or not absolute.is_file():
        raise ValueError("semantic report must be an absolute regular file")
    status = absolute.stat()
    if sys.platform.startswith("linux") and status.st_mode & 0o077:
        raise ValueError("semantic report permissions must be 0600")
    if not 0 <= time.time() - status.st_mtime <= 300:
        raise ValueError("semantic report is stale")
    payload = absolute.read_bytes()
    if len(payload) > 256 * 1024:
        raise ValueError("semantic report is too large")
    report = json.loads(payload)
    if (
        not isinstance(report, dict)
        or report.get("schema_version") != 1
        or report.get("kind") != "group"
        or report.get("self_match_count") != 1
        or report.get("inbound_match_count") != 1
        or report.get("inbound_continuation_match_count") != 1
        or report.get("header_proven") is not True
    ):
        raise ValueError("semantic report lacks current group canaries")
    raw_rows: dict[str, tuple[int, int, int, int]] = {}
    preceding_rows: dict[str, tuple[int, int, int, int]] = {}
    item_paths: dict[str, tuple[int, ...]] = {}
    for label in ("self", "inbound", "inbound_continuation"):
        evidence = report.get(f"{label}_evidence")
        if not isinstance(evidence, list) or len(evidence) != 1:
            raise ValueError("semantic report has ambiguous evidence")
        extents = evidence[0].get("item_window_extents")
        if (
            not isinstance(extents, list)
            or len(extents) != 4
            or any(not isinstance(value, int) or isinstance(value, bool) for value in extents)
        ):
            raise ValueError("semantic report lacks safe row geometry")
        x, y, width, height = extents
        if not 0 <= x <= 32_768 or not 0 <= y <= 32_768:
            raise ValueError("semantic row origin is invalid")
        if not 1 <= width <= 32_768 or not 1 <= height <= 32_768:
            raise ValueError("semantic row size is invalid")
        preceding = evidence[0].get("preceding_sibling_window_extents")
        if (
            not isinstance(preceding, list)
            or len(preceding) != 4
            or any(
                not isinstance(value, int) or isinstance(value, bool) for value in preceding
            )
        ):
            raise ValueError("semantic report lacks preceding row geometry")
        preceding_x, preceding_y, preceding_width, preceding_height = preceding
        if (
            (preceding_x, preceding_y, preceding_width) != (x, y, width)
            or not 1 <= preceding_height < height
        ):
            raise ValueError("semantic row geometry is not safely cumulative")
        output_label = {
            "self": "self",
            "inbound": "peer",
            "inbound_continuation": "peer_continuation",
        }[label]
        raw_rows[output_label] = (x, y, width, height)
        item_path = evidence[0].get("item_path")
        if (
            not isinstance(item_path, list)
            or not item_path
            or len(item_path) > 32
            or any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 0 <= value <= 20_000
                for value in item_path
            )
        ):
            raise ValueError("semantic report lacks a safe item selector")
        item_paths[output_label] = tuple(item_path)
        preceding_rows[output_label] = (
            preceding_x,
            preceding_y,
            preceding_width,
            preceding_height,
        )
    if preceding_rows["peer"] != raw_rows["self"]:
        raise ValueError("peer row does not immediately follow self canary")
    if len({path[:-1] for path in item_paths.values()}) != 1:
        raise ValueError("canary rows do not share one transcript")
    self_index = item_paths["self"][-1]
    peer_index = item_paths["peer"][-1]
    continuation_index = item_paths["peer_continuation"][-1]
    if peer_index != self_index + 1 or not peer_index < continuation_index <= peer_index + 2:
        raise ValueError("canary row ordering is unsafe")
    rows = {
        label: (x, y + previous[3], width, height - previous[3])
        for label, (x, y, width, height) in raw_rows.items()
        for previous in (preceding_rows[label],)
    }
    if len(set(rows.values())) != 3:
        raise ValueError("semantic rows are not distinct")
    ordered = sorted((y, y + height) for _x, y, _width, height in rows.values())
    if any(
        left_end > right_start
        for (_left_start, left_end), (right_start, _right_end) in pairwise(ordered)
    ):
        raise ValueError("semantic rows overlap")
    return rows


def _read_visual_calibration_sample(path: Path):  # type: ignore[no-untyped-def]
    from lemonbot.research.visual_calibration import VisualCalibrationSample

    absolute = path.expanduser()
    if absolute.is_symlink() or not absolute.is_absolute() or not absolute.is_file():
        raise ValueError("visual report must be an absolute regular file")
    if sys.platform.startswith("linux") and absolute.stat().st_mode & 0o077:
        raise ValueError("visual report permissions must be 0600")
    payload = absolute.read_bytes()
    if len(payload) > 64 * 1024:
        raise ValueError("visual report is too large")
    report = json.loads(payload)
    if (
        not isinstance(report, dict)
        or report.get("schema_version") != 1
        or report.get("kind") != "group"
        or not isinstance(report.get("calibration_sample"), dict)
    ):
        raise ValueError("visual report is invalid")
    return VisualCalibrationSample.model_validate(report["calibration_sample"])


@channel_app.command("linux-atspi-probe")
def linux_atspi_probe(
    pid: LinuxProbePidOption = None,
    max_nodes: LinuxProbeMaxNodesOption = 10_000,
) -> None:
    """Run the sanitized structural AT-SPI probe."""
    try:
        command, environment = _probe_command(_wechat_pids(pid), max_nodes)
        completed = subprocess.run(  # noqa: S603
            command,
            check=False,
            capture_output=True,
            env=environment,
            timeout=30,
        )
        report = json.loads(completed.stdout.decode("utf-8"))
        if completed.returncode != 0 or not isinstance(report, dict):
            raise ValueError("probe failed")
    except (OSError, subprocess.SubprocessError, UnicodeError, json.JSONDecodeError, ValueError):
        typer.echo("Linux AT-SPI 只读探测失败（输出已隐藏）。", err=True)
        raise typer.Exit(1) from None
    typer.echo(json.dumps(report, ensure_ascii=True, sort_keys=True))


@channel_app.command("linux-atspi-testing-action-probe")
def linux_atspi_testing_action_probe(
    confirm_testing: ConfirmTestingOption = False,
    pid: LinuxProbePidOption = None,
    max_nodes: LinuxProbeMaxNodesOption = 10_000,
) -> None:
    """Find a unique testing-group input/send surface without acting on it."""
    if not confirm_testing:
        typer.echo("必须明确确认当前可见会话是 testing 群。", err=True)
        raise typer.Exit(2)
    try:
        command, environment = _probe_command(_wechat_pids(pid), max_nodes)
        command.append("--testing-action-surface")
        completed = subprocess.run(  # noqa: S603
            command,
            check=False,
            capture_output=True,
            env=environment,
            timeout=30,
        )
        child_report = json.loads(completed.stdout.decode("utf-8"))
        if completed.returncode != 0:
            raise ValueError("testing action surface probe failed")
        report = _sanitized_testing_action_report(child_report)
    except (OSError, subprocess.SubprocessError, UnicodeError, json.JSONDecodeError, ValueError):
        typer.echo(
            "testing 群动作面只读探测失败；正文、草稿和异常详情均未记录。",
            err=True,
        )
        raise typer.Exit(1) from None
    typer.echo(json.dumps(report, ensure_ascii=True, sort_keys=True))


@channel_app.command("linux-atspi-testing-send-canary")
def linux_atspi_testing_send_canary(
    confirm_testing_send: ConfirmTestingSendOption = False,
    confirm_empty_draft: ConfirmEmptyDraftOption = False,
    pid: LinuxProbePidOption = None,
    max_nodes: LinuxProbeMaxNodesOption = 10_000,
    timeout_seconds: TestingSendTimeoutOption = 20,
) -> None:
    """Send one generated canary to the visible testing group; never retry."""
    if not confirm_testing_send or not confirm_empty_draft:
        typer.echo(
            "必须明确授权发送，并确认当前 testing 群输入框为空。",
            err=True,
        )
        raise typer.Exit(2)
    try:
        command, environment = _probe_command(_wechat_pids(pid), max_nodes)
        command.extend(
            (
                "--testing-send-canary",
                "--operator-confirmed-empty-draft",
                "--send-timeout-seconds",
                str(timeout_seconds),
            )
        )
        completed = subprocess.run(  # noqa: S603
            command,
            check=False,
            capture_output=True,
            env=environment,
            timeout=timeout_seconds + 45,
        )
        child_report = json.loads(completed.stdout.decode("utf-8"))
        if completed.returncode != 0:
            raise ValueError("testing send probe failed")
        report = _sanitized_testing_send_report(child_report)
    except (OSError, subprocess.SubprocessError, UnicodeError, json.JSONDecodeError, ValueError):
        typer.echo(
            "testing canary 发送结果未知；禁止自动重试。"
            "正文、草稿和异常详情均未记录。",
            err=True,
        )
        raise typer.Exit(1) from None
    typer.echo(json.dumps(report, ensure_ascii=True, sort_keys=True))


@channel_app.command("linux-atspi-testing-send-existing-canary")
def linux_atspi_testing_send_existing_canary(
    confirm_testing_send: ConfirmTestingSendOption = False,
    pid: LinuxProbePidOption = None,
    max_nodes: LinuxProbeMaxNodesOption = 10_000,
    timeout_seconds: TestingSendTimeoutOption = 20,
) -> None:
    """Send only an existing generated canary draft; never retry."""
    if not confirm_testing_send:
        typer.echo("必须明确授权发送当前 testing 群中的随机 canary 草稿。", err=True)
        raise typer.Exit(2)
    try:
        command, environment = _probe_command(_wechat_pids(pid), max_nodes)
        command.extend(
            (
                "--testing-send-existing-canary",
                "--send-timeout-seconds",
                str(timeout_seconds),
            )
        )
        completed = subprocess.run(  # noqa: S603
            command,
            check=False,
            capture_output=True,
            env=environment,
            timeout=timeout_seconds + 45,
        )
        child_report = json.loads(completed.stdout.decode("utf-8"))
        if completed.returncode != 0:
            raise ValueError("testing existing-canary send probe failed")
        report = _sanitized_testing_send_report(child_report)
        if report.get("used_existing_canary") is not True:
            raise ValueError("testing send did not use the existing canary")
    except (OSError, subprocess.SubprocessError, UnicodeError, json.JSONDecodeError, ValueError):
        typer.echo(
            "testing 既有 canary 发送结果未知；禁止自动重试。"
            "正文、草稿和异常详情均未记录。",
            err=True,
        )
        raise typer.Exit(1) from None
    typer.echo(json.dumps(report, ensure_ascii=True, sort_keys=True))


@channel_app.command("linux-portal-screen-probe")
def linux_portal_screen_probe(
    frames: PortalFrameCountOption = 2,
    timeout_seconds: PortalTimeoutOption = 60,
) -> None:
    """Request one window through Portal and report only sanitized frame facts."""
    error_code = "PortalProbeError"
    try:
        command, environment = _portal_probe_command(frames, timeout_seconds)
        completed = subprocess.run(  # noqa: S603
            command,
            check=False,
            capture_output=True,
            env=environment,
            timeout=timeout_seconds * 3 + 30,
        )
        report = json.loads(completed.stdout.decode("utf-8"))
        if completed.returncode != 0 or not isinstance(report, dict):
            child_error = report.get("error") if isinstance(report, dict) else None
            if child_error in {
                "PortalCaptureError",
                "PortalDenied",
                "PortalProbeError",
                "PortalProtocolError",
                "PortalTimeout",
                "ValueError",
            }:
                error_code = str(child_error)
            raise ValueError("Portal probe failed")
    except (OSError, subprocess.SubprocessError, UnicodeError, json.JSONDecodeError, ValueError):
        typer.echo(
            f"Portal 只读画面探测失败（安全代码：{error_code}）；"
            "画面和异常详情均未记录。",
            err=True,
        )
        raise typer.Exit(1) from None
    typer.echo(json.dumps(report, ensure_ascii=True, sort_keys=True))


@channel_app.command("linux-portal-group-calibration")
def linux_portal_group_calibration(
    semantic_report: VisualSemanticReportOption,
    output: VisualCalibrationOutputOption,
    frames: PortalFrameCountOption = 3,
    timeout_seconds: PortalTimeoutOption = 60,
    confirm_restart: ConfirmRestartOption = False,
    confirm_lock_cycle: ConfirmLockOption = False,
) -> None:
    """Create one private, fail-closed group direction calibration sample."""
    error_code = "PortalCalibrationError"
    destination = output.expanduser()
    if not destination.is_absolute() or destination.exists():
        typer.echo("--output 必须是尚不存在的绝对路径。", err=True)
        raise typer.Exit(2)
    try:
        if frames < 2:
            raise ValueError("calibration requires multiple frames")
        rows = _visual_canary_rows(semantic_report)
        command, environment = _portal_probe_command(
            frames,
            timeout_seconds,
            rows,
            str(Path(sys.executable).absolute()),
        )
        completed = subprocess.run(  # noqa: S603
            command,
            check=False,
            capture_output=True,
            env=environment,
            timeout=timeout_seconds * 3 + 30,
        )
        report = json.loads(completed.stdout.decode("utf-8"))
        layouts = report.get("row_layout_fingerprints") if isinstance(report, dict) else None
        if completed.returncode != 0 or not isinstance(layouts, dict):
            child_error = report.get("error") if isinstance(report, dict) else None
            if child_error in {
                "PortalCaptureError",
                "PortalDenied",
                "PortalOCRFailure",
                "PortalProbeError",
                "PortalProtocolError",
                "PortalRowChanged",
                "PortalRowOutOfFrame",
                "PortalRowUnresolved",
                "PortalTimeout",
                "ValueError",
            }:
                error_code = str(child_error)
            raise ValueError("Portal row probe failed")
        self_layout = layouts.get("self")
        peer_layout = layouts.get("peer")
        continuation_layout = layouts.get("peer_continuation")
        segment_proven = report.get("segment_label_anchor_proven") is True
        continuation_proven = report.get("continuation_binding_proven") is True
        display_sender = report.get("unverified_display_sender")
        if (
            not isinstance(self_layout, str)
            or not isinstance(peer_layout, str)
            or not isinstance(continuation_layout, str)
            or re.fullmatch(r"[0-9a-f]{64}", self_layout) is None
            or re.fullmatch(r"[0-9a-f]{64}", peer_layout) is None
            or re.fullmatch(r"[0-9a-f]{64}", continuation_layout) is None
            or self_layout == peer_layout
            or continuation_layout != peer_layout
            or (
                display_sender is not None
                and (
                    not isinstance(display_sender, str)
                    or re.fullmatch(r"uds_[0-9a-f]{64}", display_sender) is None
                )
            )
            or (segment_proven and display_sender is None)
        ):
            raise ValueError("Portal row directions are not distinct")
        from lemonbot.research.visual_calibration import VisualCalibrationSample

        sample = VisualCalibrationSample(
            run_ref=f"visual_{secrets.token_hex(12)}",
            portal_authorized=True,
            capture_source="xdg-desktop-portal",
            local_processing_only=True,
            cloud_processing_used=False,
            client_restart_observed=confirm_restart,
            lock_cycle_observed=confirm_lock_cycle,
            self_layout_fingerprint=self_layout,
            peer_layout_fingerprint=peer_layout,
            segment_label_anchor_proven=segment_proven,
            continuation_binding_proven=continuation_proven,
            ambiguous=False,
        )
        reason_codes = ["requires_second_round"]
        if not segment_proven:
            reason_codes.append("segment_label_anchor_unproven")
        if not continuation_proven:
            reason_codes.append("continuation_binding_unproven")
        encoded = json.dumps(
            {
                "schema_version": 1,
                "kind": "group",
                "passed": False,
                "reason_codes": reason_codes,
                "unverified_display_sender": display_sender if segment_proven else None,
                "calibration_sample": sample.model_dump(mode="json"),
            },
            ensure_ascii=True,
            sort_keys=True,
        ).encode("ascii")
        _write_private_new(destination, encoded + b"\n")
    except (
        OSError,
        subprocess.SubprocessError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
    ):
        typer.echo(
            f"Portal 群方向校准失败（安全代码：{error_code}）；"
            "画面、正文、标签和异常详情均未记录。",
            err=True,
        )
        raise typer.Exit(1) from None
    typer.echo(str(destination))


@channel_app.command("linux-portal-group-calibration-verify")
def linux_portal_group_calibration_verify(
    report: VisualCalibrationReportsOption,
    output: VisualCalibrationOutputOption,
) -> None:
    """Verify two private visual reports without granting connector capability."""
    destination = output.expanduser()
    if len(report) != 2:
        typer.echo("需要恰好两份独立视觉校准报告。", err=True)
        raise typer.Exit(2)
    if not destination.is_absolute() or destination.exists():
        typer.echo("--output 必须是尚不存在的绝对路径。", err=True)
        raise typer.Exit(2)
    try:
        samples = tuple(_read_visual_calibration_sample(path) for path in report)
        from lemonbot.research.visual_calibration import evaluate_visual_calibration

        decision = evaluate_visual_calibration(samples)
        encoded = json.dumps(
            {
                "schema_version": 1,
                "kind": "group",
                "passed": decision.calibrated,
                "decision": decision.model_dump(mode="json"),
            },
            ensure_ascii=True,
            sort_keys=True,
        ).encode("ascii")
        _write_private_new(destination, encoded + b"\n")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        typer.echo("视觉校准报告无效；未创建验证结果。", err=True)
        raise typer.Exit(1) from None
    typer.echo(str(destination))


@channel_app.command("linux-atspi-semantic-probe")
def linux_atspi_semantic_probe(
    kind: Literal["private", "group"] = typer.Option(..., "--kind"),
    pid: LinuxProbePidOption = None,
    max_nodes: LinuxProbeMaxNodesOption = 10_000,
    duration_seconds: int = typer.Option(180, min=30, max=600),
    output: SemanticOutputOption = None,
) -> None:
    """Observe two one-time canaries without persisting chat text."""
    error_code = "unknown"
    try:
        command, environment = _probe_command(_wechat_pids(pid), max_nodes)
        command.extend(("--semantic-kind", kind, "--duration-seconds", str(duration_seconds)))
        completed = subprocess.run(  # noqa: S603
            command,
            check=False,
            env=environment,
            stdout=subprocess.PIPE,
            timeout=duration_seconds + 30,
        )
        report = json.loads(completed.stdout.decode("utf-8"))
        if completed.returncode != 0 or not isinstance(report, dict):
            child_error = report.get("error") if isinstance(report, dict) else None
            if isinstance(child_error, str) and child_error in _SAFE_SEMANTIC_PROBE_ERROR_CODES:
                error_code = child_error
            raise ValueError("semantic probe failed")
    except (OSError, subprocess.SubprocessError, UnicodeError, json.JSONDecodeError, ValueError):
        typer.echo(
            f"Linux AT-SPI 语义探测失败（安全代码：{error_code}）；"
            "正文和异常详情均未记录。",
            err=True,
        )
        raise typer.Exit(1) from None
    encoded = json.dumps(report, ensure_ascii=True, sort_keys=True)
    if output is None:
        typer.echo(encoded)
        return
    destination = output.expanduser()
    if not destination.is_absolute() or destination.exists():
        typer.echo("--output 必须是尚不存在的绝对路径。", err=True)
        raise typer.Exit(2)
    try:
        _write_private_new(destination, (encoded + "\n").encode("ascii"))
    except (OSError, ValueError):
        typer.echo("无法安全创建语义报告。", err=True)
        raise typer.Exit(1) from None
    typer.echo(str(destination))


def _read_semantic_report(path: Path, expected_kind: str) -> dict[str, object]:
    absolute = path.expanduser()
    if absolute.is_symlink() or not absolute.is_absolute() or not absolute.is_file():
        raise ValueError("semantic reports must be absolute regular files")
    if sys.platform.startswith("linux") and absolute.stat().st_mode & 0o077:
        raise ValueError("semantic report permissions must be 0600")
    payload = absolute.read_bytes()
    if len(payload) > 256 * 1024:
        raise ValueError("semantic report is too large")
    report = json.loads(payload)
    if (
        not isinstance(report, dict)
        or report.get("schema_version") != 1
        or report.get("kind") != expected_kind
        or report.get("passed") is not True
        or not isinstance(report.get("enrollment_candidate"), dict)
    ):
        raise ValueError("semantic report did not pass the required gate")
    candidate = report["enrollment_candidate"]
    sender_proof = candidate.get("sender_probe_fingerprint")
    if expected_kind == "group" and (
        not isinstance(sender_proof, str)
        or len(sender_proof) != 64
        or any(character not in "0123456789abcdef" for character in sender_proof)
    ):
        raise ValueError("group report lacks a stable sender proof")
    return report


@channel_app.command("linux-atspi-enroll")
def linux_atspi_enroll(
    private_report: PrivateReportsOption,
    group_report: GroupReportsOption,
    output: EnrollmentOutputOption,
    private_ref: PrivateRefOption = None,
    group_ref: GroupRefOption = None,
    confirm_restart: ConfirmRestartOption = False,
    confirm_lock_cycle: ConfirmLockOption = False,
) -> None:
    """Combine two stable reports per chat kind into a private enrollment bundle."""
    if not AT_SPI_DIRECTION_GATE_OPEN:
        typer.echo(
            "AT-SPI 无法证明消息方向；安全门禁关闭，未创建 enrollment。",
            err=True,
        )
        raise typer.Exit(1)
    if (
        len(private_report) != 2
        or len(group_report) != 2
        or not confirm_restart
        or not confirm_lock_cycle
    ):
        typer.echo("需要每种会话两份报告，并确认微信重启及锁屏/解锁测试。", err=True)
        raise typer.Exit(2)
    destination = output.expanduser()
    if not destination.is_absolute() or destination.exists():
        typer.echo("enrollment 输出必须是尚不存在的绝对路径。", err=True)
        raise typer.Exit(2)
    try:
        private = tuple(_read_semantic_report(path, "private") for path in private_report)
        group = tuple(_read_semantic_report(path, "group") for path in group_report)
        reports = (*private, *group)
        accounts = {str(report["account_fingerprint"]) for report in reports}
        if len(accounts) != 1:
            raise ValueError("account fingerprint changed across semantic probes")
        private_candidates = {
            json.dumps(report["enrollment_candidate"], sort_keys=True)
            for report in private
        }
        group_candidates = {
            json.dumps(report["enrollment_candidate"], sort_keys=True)
            for report in group
        }
        if len(private_candidates) != 1 or len(group_candidates) != 1:
            raise ValueError("AT-SPI semantic structure changed across probe runs")
        candidates = [
            json.loads(next(iter(private_candidates))),
            json.loads(next(iter(group_candidates))),
        ]
        target_refs = (
            private_ref or f"private_{secrets.token_hex(12)}",
            group_ref or f"group_{secrets.token_hex(12)}",
        )
        if target_refs[0] == target_refs[1]:
            raise ValueError("target refs must differ")
        for candidate, target_ref in zip(candidates, target_refs, strict=True):
            candidate.pop("semantic_shape_sha256", None)
            candidate.pop("sender_probe_fingerprint", None)
            candidate["target_ref"] = target_ref
        from lemonbot.connectors import AtspiEnrollment

        ui_signature = hashlib.sha256(
            json.dumps(candidates, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        bundle = AtspiEnrollment.model_validate(
            {
                "account_fingerprint": next(iter(accounts)),
                "ui_signature": ui_signature,
                "targets": candidates,
            }
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        typer.echo("语义报告不一致或无效；未创建 enrollment。", err=True)
        raise typer.Exit(1) from None
    encoded = bundle.model_dump_json().encode("utf-8")
    try:
        _write_private_new(destination, encoded)
    except (OSError, ValueError):
        typer.echo("无法安全创建 enrollment。", err=True)
        raise typer.Exit(1) from None
    typer.echo(
        json.dumps(
            {
                "account_fingerprint": bundle.account_fingerprint,
                "ui_signature": bundle.ui_signature,
                "enrollment_bundle_sha256": hashlib.sha256(encoded).hexdigest(),
                "target_refs": [target.target_ref for target in bundle.targets],
                "path": str(destination),
            },
            sort_keys=True,
        )
    )


def _settings(config: Path | None):  # type: ignore[no-untyped-def]
    path = config or default_config_path()
    try:
        return load_settings(path)
    except FileNotFoundError:
        typer.echo(f"配置文件不存在：{path}", err=True)
        typer.echo("请复制 config/lemonbot.example.toml 并修改（不要写入密钥）。", err=True)
        raise typer.Exit(2) from None
    except ValidationError as exc:
        typer.echo("配置无效：", err=True)
        for issue in exc.errors(include_url=False, include_context=False, include_input=False):
            location = ".".join(str(part) for part in issue["loc"])
            typer.echo(f"- {location or '<root>'}: {issue['msg']}", err=True)
        raise typer.Exit(2) from None
    except Exception as exc:
        typer.echo(f"配置无效（{type(exc).__name__}）。", err=True)
        raise typer.Exit(2) from None


@data_app.command("export")
def data_export_command(config: ConfigOption = None, output: OutputOption = None) -> None:
    settings = _settings(config)
    try:
        archive = export_profile_data(RuntimePaths.from_settings(settings), output)
    except (AlreadyRunningError, BackupError, DataOperationError) as exc:
        typer.echo(f"数据导出失败：{exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(str(archive))


@data_app.command("delete-conversation")
def data_delete_conversation(
    channel: str,
    chat_id: str,
    config: ConfigOption = None,
    confirm: bool = typer.Option(False, "--confirm"),
) -> None:
    if not confirm:
        typer.echo("这是不可逆操作；停止 Lemonbot 后添加 --confirm。", err=True)
        raise typer.Exit(2)
    settings = _settings(config)
    if channel == "wechat_personal_lab" and chat_id in settings.wechat_atspi.allow_target_refs:
        typer.echo("目标仍在白名单中；请先从配置和 enrollment 移除。", err=True)
        raise typer.Exit(2)
    try:
        result = delete_conversation(
            RuntimePaths.from_settings(settings), channel=channel, chat_id=chat_id
        )
    except (AlreadyRunningError, DataOperationError) as exc:
        typer.echo(f"会话删除失败：{exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        json.dumps(
            {
                "operation_id": result.operation_id,
                "rows_deleted": result.total_rows,
                "objects_removed": result.objects_removed,
                "object_cleanup_failures": result.object_cleanup_failures,
            },
            sort_keys=True,
        )
    )


@outbox_app.command("unknown")
def outbox_unknown(
    config: ConfigOption = None,
    limit: int = typer.Option(100, "--limit", min=1, max=500),
) -> None:
    settings = _settings(config)
    paths = RuntimePaths.from_settings(settings)
    paths.ensure()

    async def list_items() -> list[dict[str, object]]:
        from lemonbot.storage import CoreRepository, Database
        from lemonbot.storage.migrate import upgrade_database

        await asyncio.to_thread(upgrade_database, paths.database)
        database = Database.from_path(paths.database)
        await database.initialise()
        try:
            return await CoreRepository(database).list_unknown_outbox(limit=limit)
        finally:
            await database.close()

    try:
        with RuntimeLock(paths.lock_file):
            items = asyncio.run(list_items())
    except (AlreadyRunningError, RuntimeError, OSError) as exc:
        typer.echo(f"读取 unknown outbox 失败：{type(exc).__name__}", err=True)
        raise typer.Exit(1) from exc
    for item in items:
        typer.echo(json.dumps(item, sort_keys=True))
    if not items:
        typer.echo("没有 unknown outbox。")


@outbox_app.command("resolve")
def outbox_resolve(
    item_id: int,
    outcome: Literal["acknowledged", "dead"] = typer.Option(..., "--as"),
    note: str = typer.Option(..., "--note"),
    config: ConfigOption = None,
    confirm: bool = typer.Option(False, "--confirm"),
) -> None:
    if not confirm:
        raise typer.Exit(2)
    settings = _settings(config)
    paths = RuntimePaths.from_settings(settings)
    paths.ensure()

    async def reconcile() -> bool:
        from lemonbot.storage import CoreRepository, Database
        from lemonbot.storage.migrate import upgrade_database

        await asyncio.to_thread(upgrade_database, paths.database)
        database = Database.from_path(paths.database)
        await database.initialise()
        try:
            return await CoreRepository(database).reconcile_unknown_outbox(
                item_id, outcome=outcome, operator_note=note
            )
        finally:
            await database.close()

    try:
        with RuntimeLock(paths.lock_file):
            resolved = asyncio.run(reconcile())
    except (AlreadyRunningError, RuntimeError, ValueError, OSError) as exc:
        typer.echo(f"outbox 核对失败：{type(exc).__name__}", err=True)
        raise typer.Exit(1) from exc
    if not resolved:
        raise typer.Exit(1)
    typer.echo(f"outbox {item_id} 已核对为 {outcome}。")


@app.command()
def doctor(config: ConfigOption = None) -> None:
    settings = _settings(config)
    checks = run_checks(settings, RuntimePaths.from_settings(settings))
    for check in checks:
        marker = "OK" if check.ok else ("WARN" if not check.required else "FAIL")
        typer.echo(f"[{marker:4}] {check.name}: {check.detail}")
    if any(not item.ok and item.required for item in checks):
        raise typer.Exit(1)


@app.command()
def run(config: ConfigOption = None) -> None:
    settings = _settings(config)
    from lemonbot.runtime import run_service

    try:
        asyncio.run(run_service(settings))
    except KeyboardInterrupt:
        typer.echo("Lemonbot 已停止。")


@app.command()
def smoke() -> None:
    async def scenario(root: Path) -> str:
        from lemonbot.connectors import FakeConnector
        from lemonbot.storage import CoreRepository, Database

        database = Database.from_path(root / "smoke.db")
        await database.initialise()
        repository = CoreRepository(database)
        await repository.set_allowlisted("fake", "smoke-chat", label="offline smoke")
        connector = FakeConnector(channel="fake")
        pipeline = EventPipeline(
            repository,
            DeterministicPolicy(repository),
            FakeModelBackend(["Lemonbot fake vertical slice is healthy."]),
        )
        try:
            await pipeline.ingest(
                InboundEvent(
                    channel="fake",
                    event_id=f"smoke-{datetime.now(UTC).timestamp()}",
                    chat_id="smoke-chat",
                    sender_id="local-operator",
                    text="health check",
                )
            )
            await pipeline.process_once("fake")
            result = await pipeline.dispatch_once(connector, channel="fake")
            if result.status.value != "acknowledged":
                raise RuntimeError("unexpected smoke result")
            return connector.delivered_messages[0].text
        finally:
            await database.close()

    with tempfile.TemporaryDirectory(prefix="lemonbot-smoke-") as temporary:
        typer.echo(asyncio.run(scenario(Path(temporary))))


@app.command("backup")
def backup_command(config: ConfigOption = None, output: OutputOption = None) -> None:
    settings = _settings(config)
    try:
        result = create_backup(RuntimePaths.from_settings(settings), output)
    except BackupError as exc:
        typer.echo(f"备份失败：{exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(str(result))


@app.command("restore")
def restore_command(
    archive: Path,
    config: ConfigOption = None,
    confirm: bool = typer.Option(False, "--confirm"),
) -> None:
    if not confirm:
        raise typer.Exit(2)
    settings = _settings(config)
    preserved = restore_backup(RuntimePaths.from_settings(settings), archive)
    typer.echo(f"恢复完成；原状态：{preserved}" if preserved else "恢复完成。")


@app.command("install-service")
def install_service(config: ConfigOption = None) -> None:
    """Install and start the per-user Linux systemd service."""
    if not sys.platform.startswith("linux"):
        typer.echo("Lemonbot 服务安装只支持 Linux。", err=True)
        raise typer.Exit(2)
    settings = _settings(config)
    config_path = (config or default_config_path()).expanduser().resolve(strict=True)
    project_root = Path.cwd().resolve()
    requirements = project_root / "deploy" / "atspi-worker-requirements.txt"
    if not (project_root / "pyproject.toml").is_file() or not requirements.is_file():
        typer.echo("请从 Lemonbot 仓库根目录运行 install-service。", err=True)
        raise typer.Exit(2)
    worker_python = Path(settings.wechat_atspi.worker_python_path)
    worker_root = worker_python.parent.parent
    data_root = RuntimePaths.from_settings(settings).root
    try:
        worker_root.relative_to(data_root)
    except ValueError:
        typer.echo("AT-SPI worker venv 必须位于当前 profile 数据目录内。", err=True)
        raise typer.Exit(2) from None
    if worker_python.name != "python" or worker_python.parent.name != "bin":
        typer.echo("worker_python_path 必须指向 venv/bin/python。", err=True)
        raise typer.Exit(2)
    uv = shutil.which("uv")
    if uv is None:
        typer.echo("未找到 uv。", err=True)
        raise typer.Exit(1)
    with tempfile.TemporaryDirectory(prefix="lemonbot-worker-build-") as build_directory:
        build_root = Path(build_directory)
        try:
            subprocess.run(  # noqa: S603
                [uv, "build", "--wheel", "--out-dir", str(build_root), str(project_root)],
                check=True,
            )
            wheels = tuple(build_root.glob("lemonbot-*.whl"))
            if len(wheels) != 1:
                raise RuntimeError("worker wheel build was ambiguous")
            subprocess.run(  # noqa: S603
                [
                    uv,
                    "venv",
                    "--clear",
                    "--python",
                    "/usr/bin/python3",
                    "--system-site-packages",
                    str(worker_root),
                ],
                check=True,
            )
            subprocess.run(  # noqa: S603
                [
                    uv,
                    "pip",
                    "install",
                    "--python",
                    str(worker_python),
                    "--requirement",
                    str(requirements),
                ],
                check=True,
            )
            subprocess.run(  # noqa: S603
                [
                    uv,
                    "pip",
                    "install",
                    "--python",
                    str(worker_python),
                    "--no-deps",
                    str(wheels[0]),
                ],
                check=True,
            )
            subprocess.run(  # noqa: S603
                [
                    str(worker_python),
                    "-I",
                    "-c",
                    (
                        "import gi; gi.require_version('Atspi','2.0'); "
                        "import lemonbot.connectors.atspi_worker"
                    ),
                ],
                check=True,
            )
        except (subprocess.CalledProcessError, RuntimeError) as exc:
            typer.echo(f"AT-SPI worker 安装失败：{type(exc).__name__}", err=True)
            raise typer.Exit(1) from exc
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)
    unit_path = service_dir / "lemonbot.service"
    executable = json.dumps(sys.executable)
    configured = json.dumps(str(config_path))
    unit = f"""[Unit]
Description=Lemonbot Linux-only observe runtime
After=graphical-session.target lemonbot-wechat-accessible.service
Requires=lemonbot-wechat-accessible.service
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart={executable} -m lemonbot run --config {configured}
Restart=on-failure
RestartSec=10
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={json.dumps(str(data_root))}
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
UMask=0077
KillMode=control-group

[Install]
WantedBy=graphical-session.target
"""
    temporary = unit_path.with_suffix(".service.tmp")
    temporary.write_text(unit, encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(unit_path)
    wechat_source = project_root / "deploy" / "systemd" / "lemonbot-wechat-accessible.service"
    wechat_unit = service_dir / "lemonbot-wechat-accessible.service"
    if not wechat_source.is_file():
        typer.echo("缺少微信 accessibility systemd unit。", err=True)
        raise typer.Exit(1)
    wechat_temporary = wechat_unit.with_suffix(".service.tmp")
    wechat_temporary.write_bytes(wechat_source.read_bytes())
    wechat_temporary.chmod(0o600)
    wechat_temporary.replace(wechat_unit)
    try:
        subprocess.run(["/usr/bin/systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(
            ["/usr/bin/systemctl", "--user", "enable", "--now", "lemonbot.service"],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        typer.echo(f"systemd 服务安装失败（exit={exc.returncode}）。", err=True)
        raise typer.Exit(1) from exc
    typer.echo(str(unit_path))


@app.command("emergency-stop")
def emergency_stop(config: ConfigOption = None) -> None:
    settings = _settings(config)
    paths = RuntimePaths.from_settings(settings)
    paths.ensure()
    temporary = paths.emergency_stop_file.with_suffix(".tmp")
    temporary.write_text("stopped\n", encoding="ascii")
    temporary.chmod(0o600)
    temporary.replace(paths.emergency_stop_file)
    if sys.platform.startswith("linux"):
        subprocess.run(
            ["/usr/bin/systemctl", "--user", "stop", "lemonbot.service"], check=False
        )

    async def persist_pause() -> None:
        from lemonbot.storage import CoreRepository, Database
        from lemonbot.storage.migrate import upgrade_database

        await asyncio.to_thread(upgrade_database, paths.database)
        database = Database.from_path(paths.database)
        await database.initialise()
        try:
            await CoreRepository(database).set_paused(paused=True)
        finally:
            await database.close()

    try:
        with RuntimeLock(paths.lock_file):
            asyncio.run(persist_pause())
    except (AlreadyRunningError, RuntimeError, OSError):
        typer.echo("服务尚未完全停止；持久急停已生效，请稍后重试确认状态。", err=True)
        raise typer.Exit(1) from None
    typer.echo("已持久急停；服务重启后仍会拒绝运行。")


@app.command("resume")
def resume(config: ConfigOption = None, confirm: bool = typer.Option(False, "--confirm")) -> None:
    if not confirm:
        typer.echo("恢复会重新建立 transcript baseline；请添加 --confirm。", err=True)
        raise typer.Exit(2)
    settings = _settings(config)
    paths = RuntimePaths.from_settings(settings)
    paths.ensure()

    async def reset_observe_boundary() -> None:
        from lemonbot.storage import CoreRepository, Database
        from lemonbot.storage.migrate import upgrade_database

        await asyncio.to_thread(upgrade_database, paths.database)
        database = Database.from_path(paths.database)
        await database.initialise()
        try:
            repository = CoreRepository(database)
            await repository.clear_runtime_state_prefix("atspi:cursor:")
            await repository.set_paused(paused=False)
        finally:
            await database.close()

    try:
        with RuntimeLock(paths.lock_file):
            asyncio.run(reset_observe_boundary())
            paths.emergency_stop_file.unlink(missing_ok=True)
    except (AlreadyRunningError, RuntimeError, OSError):
        typer.echo("无法安全重置 Observe baseline；急停保持生效。", err=True)
        raise typer.Exit(1) from None
    typer.echo("急停已解除；下次启动会重新建立 baseline，不补抓停机消息。")


@secret_app.command("set")
def secret_set(name: str, profile: str = typer.Option("lab", "--profile")) -> None:
    if name not in _SECRET_NAMES or profile not in {"prod", "lab"}:
        raise typer.Exit(2)
    value = getpass.getpass(f"输入 {profile}/{name}（不会回显）: ")
    confirmation = getpass.getpass("再次输入: ")
    if not value or value != confirmation:
        raise typer.Exit(2)
    try:
        NamespacedSecretStore(platform_secret_store(), profile).set(name, value)
    except SecretStoreError:
        typer.echo("Linux Secret Service 不可用或仍处于锁定状态。", err=True)
        raise typer.Exit(1) from None
    typer.echo("密钥已写入 Linux Secret Service。")


@secret_app.command("status")
def secret_status(profile: str = typer.Option("lab", "--profile")) -> None:
    if profile not in {"prod", "lab"}:
        raise typer.Exit(2)
    try:
        store = NamespacedSecretStore(platform_secret_store(), profile)
        statuses = {name: store.get(name) is not None for name in sorted(_SECRET_NAMES)}
    except SecretStoreError:
        typer.echo("Linux Secret Service 不可用或仍处于锁定状态。", err=True)
        raise typer.Exit(1) from None
    for name, configured in statuses.items():
        typer.echo(f"{name}: {'configured' if configured else 'missing'}")


@secret_app.command("delete")
def secret_delete(
    name: str,
    profile: str = typer.Option("lab", "--profile"),
    confirm: bool = typer.Option(False, "--confirm"),
) -> None:
    if name not in _SECRET_NAMES or profile not in {"prod", "lab"} or not confirm:
        raise typer.Exit(2)
    try:
        deleted = NamespacedSecretStore(platform_secret_store(), profile).delete(name)
    except SecretStoreError:
        typer.echo("Linux Secret Service 不可用或仍处于锁定状态。", err=True)
        raise typer.Exit(1) from None
    typer.echo("已删除。" if deleted else "该凭据不存在。")
