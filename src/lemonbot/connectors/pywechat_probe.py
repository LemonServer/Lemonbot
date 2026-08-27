"""Read-only compatibility probe derived from pywechat's WeChat 4.x selectors.

This module deliberately does not import pywechat.  The upstream package also
imports PyAutoGUI, disables its fail-safe, uses the global clipboard, and has
coordinate-driven actions.  Lemonbot only needs its documented UIA structure
to determine whether WeChat exposed an accessibility tree after Narrator was
started before login.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

PYWECHAT_SOURCE = "wuchaooooo/pywechat-windows-ui-auto"
PYWECHAT_COMMIT = "363f9139abd419c1289a27391890c62112589030"
_WINDOW_CLASS = re.compile(r"^Qt\d+QWindowIcon$")
_ALLOWED_PROCESS_NAMES = frozenset({"weixin.exe", "wechat.exe"})


class PyWechatProbeError(RuntimeError):
    """Raised when a read-only compatibility probe cannot be completed."""


@dataclass(frozen=True, slots=True)
class UIANodeFacts:
    """Minimum UIA properties needed to match the audited selector subset."""

    control_type: str = ""
    class_name: str = ""
    automation_id: str = ""
    framework_id: str = ""
    name: str = ""


@dataclass(frozen=True, slots=True)
class PyWechatProbeReport:
    process_count: int
    candidate_window_count: int
    selected_window_handle: int | None
    node_count: int
    max_depth_seen: int
    truncated: bool
    selector_counts: dict[str, int]
    selector_signature: str | None
    accessibility_exposed: bool
    observe_surface_ready: bool
    draft_surface_ready: bool
    reply_surface_visible: bool
    status: str

    def safe_dict(self) -> dict[str, object]:
        """Return a report containing no account, contact, or message text."""

        return {
            "source": PYWECHAT_SOURCE,
            "source_commit": PYWECHAT_COMMIT,
            "process_count": self.process_count,
            "candidate_window_count": self.candidate_window_count,
            "selected_window_handle": self.selected_window_handle,
            "node_count": self.node_count,
            "max_depth_seen": self.max_depth_seen,
            "truncated": self.truncated,
            "selector_counts": dict(sorted(self.selector_counts.items())),
            "selector_signature": self.selector_signature,
            "accessibility_exposed": self.accessibility_exposed,
            "observe_surface_ready": self.observe_surface_ready,
            "draft_surface_ready": self.draft_surface_ready,
            "reply_surface_visible": self.reply_surface_visible,
            "status": self.status,
        }


def _matches(node: UIANodeFacts, selector: str) -> bool:
    # This is the small, audited subset from pyweixin/Uielements.py.  No
    # action-oriented selector or dynamic contact/message name is included.
    if selector == "main_window":
        return node.class_name == "mmui::MainWindow" and node.framework_id == "Qt"
    if selector == "conversation_list":
        return node.control_type == "List" and node.name == "会话" and node.framework_id == "Qt"
    if selector == "message_list":
        return node.control_type == "List" and node.name == "消息"
    if selector == "search_edit":
        return (
            node.control_type == "Edit"
            and node.name == "搜索"
            and node.class_name == "mmui::XValidatorTextEdit"
        )
    if selector == "input_edit":
        return node.control_type == "Edit" and node.automation_id == "chat_input_field"
    if selector == "send_button":
        return node.control_type == "Button" and node.name in {"发送", "发送(S)"}
    if selector == "chat_header":
        return (
            node.control_type == "Text"
            and node.automation_id
            == "content_view.top_content_view.title_h_view.left_v_view."
            "left_content_v_view.left_ui_.big_title_line_h_view.current_chat_name_label"
        )
    if selector == "session_item":
        return node.control_type == "ListItem" and node.class_name == "mmui::ChatSessionCell"
    raise ValueError(f"unknown audited selector: {selector}")


_SELECTOR_NAMES = (
    "main_window",
    "conversation_list",
    "message_list",
    "search_edit",
    "input_edit",
    "send_button",
    "chat_header",
    "session_item",
)


def analyze_pywechat_nodes(
    nodes: Sequence[UIANodeFacts],
    *,
    process_count: int,
    candidate_window_count: int,
    selected_window_handle: int | None,
    max_depth_seen: int,
    truncated: bool,
) -> PyWechatProbeReport:
    """Analyze a captured UIA tree without retaining or returning visible text."""

    counts = {
        selector: sum(_matches(node, selector) for node in nodes) for selector in _SELECTOR_NAMES
    }
    exposed = any(node.class_name.startswith("mmui::") for node in nodes) and len(nodes) > 3
    observe_ready = (
        exposed
        and counts["main_window"] == 1
        and counts["conversation_list"] == 1
        and counts["message_list"] == 1
    )
    draft_ready = observe_ready and counts["input_edit"] == 1 and counts["chat_header"] == 1
    reply_visible = draft_ready and counts["search_edit"] == 1 and counts["send_button"] == 1

    signature_items: list[dict[str, str]] = []
    for selector in _SELECTOR_NAMES:
        for node in nodes:
            if not _matches(node, selector):
                continue
            signature_items.append(
                {
                    "selector": selector,
                    "control_type": node.control_type,
                    "class_name": node.class_name,
                    "automation_id": node.automation_id,
                    "framework_id": node.framework_id,
                }
            )
    signature = None
    if signature_items:
        encoded = json.dumps(
            signature_items,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        signature = hashlib.sha256(encoded).hexdigest()

    if process_count == 0:
        status = "weixin_not_running"
    elif candidate_window_count == 0:
        status = "main_window_not_visible"
    elif not exposed:
        status = "accessibility_tree_not_exposed"
    elif truncated:
        status = "tree_limit_reached"
    elif reply_visible:
        status = "pywechat_action_surface_visible"
    elif observe_ready:
        status = "pywechat_read_surface_visible"
    else:
        status = "pywechat_selectors_incomplete"

    return PyWechatProbeReport(
        process_count=process_count,
        candidate_window_count=candidate_window_count,
        selected_window_handle=selected_window_handle,
        node_count=len(nodes),
        max_depth_seen=max_depth_seen,
        truncated=truncated,
        selector_counts=counts,
        selector_signature=signature,
        accessibility_exposed=exposed,
        observe_surface_ready=observe_ready,
        draft_surface_ready=draft_ready,
        reply_surface_visible=reply_visible,
        status=status,
    )


def _read_property(element: Any, name: str) -> str:
    try:
        value = getattr(element, name)
    except Exception:
        return ""
    return "" if value is None else str(value)


def _walk_element_info(
    root: Any,
    *,
    max_nodes: int,
    max_depth: int,
) -> tuple[list[UIANodeFacts], int, bool]:
    nodes: list[UIANodeFacts] = []
    stack: list[tuple[Any, int]] = [(root, 0)]
    max_seen = 0
    truncated = False
    while stack:
        element, depth = stack.pop()
        max_seen = max(max_seen, depth)
        nodes.append(
            UIANodeFacts(
                control_type=_read_property(element, "control_type"),
                class_name=_read_property(element, "class_name"),
                automation_id=_read_property(element, "automation_id"),
                framework_id=_read_property(element, "framework_id"),
                name=_read_property(element, "name"),
            )
        )
        if len(nodes) >= max_nodes:
            truncated = bool(stack)
            break
        if depth >= max_depth:
            continue
        try:
            children: Iterable[Any] = element.children()
        except Exception:
            children = ()
        child_list = list(children)
        stack.extend((child, depth + 1) for child in reversed(child_list))
    return nodes, max_seen, truncated


def _candidate_windows(process_ids: set[int]) -> list[int]:
    import win32gui  # type: ignore[import-untyped]
    import win32process  # type: ignore[import-untyped]

    candidates: list[int] = []

    def collect(hwnd: int, _state: object) -> bool:
        try:
            _thread_id, process_id = win32process.GetWindowThreadProcessId(hwnd)
            if process_id not in process_ids:
                return True
            class_name = win32gui.GetClassName(hwnd)
            if class_name == "mmui::MainWindow" or _WINDOW_CLASS.fullmatch(class_name):
                candidates.append(hwnd)
        except Exception:
            return True
        return True

    # Match pywechat's own window discovery: Weixin can keep its Qt window on
    # the input desktop even after the user closes it to the tray.
    win32gui.EnumDesktopWindows(0, collect, None)
    return sorted(set(candidates))


def _capture_window_tree(
    desktop: Any,
    handle: int,
    *,
    max_nodes: int,
    max_depth: int,
) -> tuple[int, list[UIANodeFacts], int, bool] | None:
    try:
        root = desktop.window(handle=handle).wrapper_object().element_info
        nodes, depth, truncated = _walk_element_info(
            root,
            max_nodes=max_nodes,
            max_depth=max_depth,
        )
    except Exception:
        # UIA errors may embed visible chat text, so they are intentionally not logged.
        return None
    return handle, nodes, depth, truncated


def probe_pywechat_surface(
    *,
    expected_process_name: str = "Weixin.exe",
    max_nodes: int = 5_000,
    max_depth: int = 20,
) -> PyWechatProbeReport:
    """Probe pywechat's UIA selectors without focusing, clicking, or typing."""

    if os.name != "nt":
        raise PyWechatProbeError("the pywechat compatibility probe requires Windows")
    if expected_process_name.casefold() not in _ALLOWED_PROCESS_NAMES:
        raise ValueError("the probe is restricted to Weixin.exe or WeChat.exe")
    if not 100 <= max_nodes <= 20_000:
        raise ValueError("max_nodes must be between 100 and 20000")
    if not 1 <= max_depth <= 30:
        raise ValueError("max_depth must be between 1 and 30")

    try:
        import psutil  # type: ignore[import-untyped]
        from pywinauto import Desktop  # type: ignore[import-untyped]
    except ImportError as exc:
        raise PyWechatProbeError(
            "pywinauto is not installed; install Lemonbot's windows optional dependencies"
        ) from exc

    process_ids = {
        process.pid
        for process in psutil.process_iter(["name"])
        if (process.info.get("name") or "").casefold() == expected_process_name.casefold()
    }
    handles = _candidate_windows(process_ids) if process_ids else []
    if not handles:
        return analyze_pywechat_nodes(
            (),
            process_count=len(process_ids),
            candidate_window_count=0,
            selected_window_handle=None,
            max_depth_seen=0,
            truncated=False,
        )

    desktop = Desktop(backend="uia")
    captures: list[tuple[int, list[UIANodeFacts], int, bool]] = []
    for handle in handles:
        capture = _capture_window_tree(
            desktop,
            handle,
            max_nodes=max_nodes,
            max_depth=max_depth,
        )
        if capture is not None:
            captures.append(capture)
    if not captures:
        raise PyWechatProbeError("UIA could not read any candidate Weixin window")

    # Choosing the richest read-only tree cannot trigger an external effect.
    # A stable sender will still require Lemonbot's separate exact enrollment.
    handle, nodes, depth, truncated = max(captures, key=lambda item: len(item[1]))
    return analyze_pywechat_nodes(
        nodes,
        process_count=len(process_ids),
        candidate_window_count=len(handles),
        selected_window_handle=handle,
        max_depth_seen=depth,
        truncated=truncated,
    )
