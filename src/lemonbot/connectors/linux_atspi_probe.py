"""Standalone, read-only AT-SPI surface probe for official Linux WeChat.

This module deliberately depends only on the Python standard library and the
distribution-provided ``python3-gi`` package.  The main Lemonbot virtualenv
launches it with ``/usr/bin/python3 -I`` so the probe never receives model
credentials or imports the Lemonbot runtime.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import importlib
import json
import re
import secrets
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from functools import partial
from typing import Any

DEFAULT_MAX_NODES = 10_000
DEFAULT_MAX_DEPTH = 24
_MESSAGE_ITEM_ROLES = frozenset({"list item", "list-item", "row"})
_ACTIVATION_ACTION_NAMES = frozenset({"activate", "click", "press"})
_STATE_SHOWING = 25
_STATE_VISIBLE = 30
_STATE_FOCUSED = 12


def _safe(call: Callable[[], Any], default: Any = None) -> Any:
    try:
        return call()
    except Exception:
        return default


def _interfaces(node: Any) -> tuple[str, ...]:
    values = _safe(node.get_interfaces, []) or []
    return tuple(sorted(str(value).rsplit(".", 1)[-1] for value in values))


def _inspect_app(app: Any, *, max_nodes: int, max_depth: int) -> dict[str, object]:
    role_counts: Counter[str] = Counter()
    interface_counts: Counter[str] = Counter()
    known_controls: Counter[str] = Counter()
    structures: list[str] = []
    named_nodes = 0
    action_nodes = 0
    action_slots = 0
    errors = 0
    max_depth_seen = 0
    stack: list[tuple[Any, int, str]] = [(app, 0, "0")]
    visited = 0

    while stack and visited < max_nodes:
        node, depth, path = stack.pop()
        visited += 1
        max_depth_seen = max(max_depth_seen, depth)
        role = str(_safe(node.get_role_name, "unknown") or "unknown")
        role_counts[role] += 1
        node_interfaces = _interfaces(node)
        interface_counts.update(node_interfaces)

        # Names are inspected only for a tiny fixed vocabulary and are never
        # emitted or included in the structure signature.
        name = str(_safe(node.get_name, "") or "")
        if name:
            named_nodes += 1
            lowered = name.casefold().strip()
            if lowered in {"chats", "聊天"}:
                known_controls["chat_list_label"] += 1
            if lowered in {"send(s)", "send", "发送(s)", "发送"}:
                known_controls["send_label"] += 1
            if lowered in {"search", "搜索"}:
                known_controls["search_label"] += 1

        action_iface = _safe(node.get_action_iface)
        if action_iface is not None:
            action_nodes += 1
            action_slots += max(0, min(int(_safe(action_iface.get_n_actions, 0) or 0), 32))

        child_count = int(_safe(node.get_child_count, 0) or 0)
        if child_count < 0 or child_count > max_nodes:
            errors += 1
            child_count = 0
        structures.append(f"{path}|{role}|{','.join(node_interfaces)}|{child_count}")
        if depth >= max_depth:
            continue
        children: list[tuple[Any, int, str]] = []
        for index in range(child_count):
            child = _safe(partial(node.get_child_at_index, index))
            if child is None:
                errors += 1
                continue
            children.append((child, depth + 1, f"{path}.{index}"))
        stack.extend(reversed(children))

    encoded = "\n".join(structures).encode("utf-8")
    return {
        "pid": int(_safe(app.get_process_id, 0) or 0),
        "node_count": visited,
        "truncated": bool(stack),
        "max_depth": max_depth_seen,
        "named_node_count": named_nodes,
        "action_node_count": action_nodes,
        "action_slot_count": action_slots,
        "errors": errors,
        "role_counts": dict(sorted(role_counts.items())),
        "interface_counts": dict(sorted(interface_counts.items())),
        "known_controls": dict(sorted(known_controls.items())),
        "structure_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _load_atspi() -> Any:
    gi = importlib.import_module("gi")
    gi.require_version("Atspi", "2.0")
    return importlib.import_module("gi.repository.Atspi")


def probe(target_pids: frozenset[int], *, max_nodes: int, max_depth: int) -> dict[str, object]:
    if not target_pids or any(pid <= 0 for pid in target_pids):
        raise ValueError("one or more positive target PIDs are required")
    if not 100 <= max_nodes <= 20_000 or not 1 <= max_depth <= 32:
        raise ValueError("probe bounds are outside the safe range")

    atspi = _load_atspi()
    atspi.init()
    desktop = atspi.get_desktop(0)
    app_count = int(_safe(desktop.get_child_count, 0) or 0)
    matches: list[dict[str, object]] = []
    for index in range(max(0, min(app_count, 1_000))):
        app = _safe(partial(desktop.get_child_at_index, index))
        if app is None:
            continue
        pid = int(_safe(app.get_process_id, 0) or 0)
        if pid in target_pids:
            matches.append(_inspect_app(app, max_nodes=max_nodes, max_depth=max_depth))
    return {
        "schema_version": 1,
        "target_count": len(target_pids),
        "match_count": len(matches),
        "matches": matches,
    }


def _visible_text(node: Any) -> str:
    return str(_safe(node.get_name, "") or "").strip()


def _node_signature(node: Any, *, max_nodes: int = 256) -> str:
    structures: list[str] = []
    stack: list[tuple[Any, str]] = [(node, "0")]
    while stack and len(structures) < max_nodes:
        current, path = stack.pop()
        role = str(_safe(current.get_role_name, "unknown") or "unknown")
        count = int(_safe(current.get_child_count, 0) or 0)
        if count < 0 or count > max_nodes:
            count = 0
        structures.append(f"{path}|{role}|{','.join(_interfaces(current))}|{count}")
        children: list[tuple[Any, str]] = []
        for index in range(count):
            child = _safe(partial(current.get_child_at_index, index))
            if child is not None:
                children.append((child, f"{path}.{index}"))
        stack.extend(reversed(children))
    return hashlib.sha256("\n".join(structures).encode()).hexdigest()


def _parent(node: Any) -> Any | None:
    return _safe(node.get_parent)


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


def _attribute_keys(node: Any) -> tuple[str, ...]:
    return tuple(sorted(_attributes(node)))


def _window_extents(node: Any) -> tuple[int, int, int, int] | None:
    getter = getattr(node, "get_component_iface", None)
    if not callable(getter):
        return None
    component = _safe(getter)
    if component is None:
        return None
    extents_getter = getattr(component, "get_extents", None)
    if not callable(extents_getter):
        return None
    # Atspi.CoordType.WINDOW is 1. Keeping the standalone helper free of an
    # additional GI import also keeps fake-tree tests deterministic.
    rectangle = _safe(partial(extents_getter, 1))
    if rectangle is None:
        return None
    values = tuple(int(getattr(rectangle, key, -1)) for key in ("x", "y", "width", "height"))
    x, y, width, height = values
    if not 0 <= x <= 32_768 or not 0 <= y <= 32_768:
        return None
    if not 1 <= width <= 32_768 or not 1 <= height <= 32_768:
        return None
    return x, y, width, height


def _is_showing(node: Any) -> bool:
    getter = getattr(node, "get_state_set", None)
    if not callable(getter):
        return False
    states = _safe(getter)
    contains = getattr(states, "contains", None)
    if not callable(contains):
        return False
    # Atspi.StateType.SHOWING/VISIBLE are stable protocol enum values. Keeping
    # the standalone helper free of a second GI import simplifies isolation.
    return bool(
        _safe(partial(contains, _STATE_SHOWING), False)
        and _safe(partial(contains, _STATE_VISIBLE), False)
    )


def _single_action(node: Any) -> tuple[int, str] | None:
    getter = getattr(node, "get_action_iface", None)
    if not callable(getter):
        return None
    action = _safe(getter)
    if action is None:
        return None
    count_getter = getattr(action, "get_n_actions", None)
    if not callable(count_getter):
        return None
    count = min(max(int(_safe(count_getter, 0) or 0), 0), 16)
    if count != 1:
        return None
    name_getter = getattr(action, "get_action_name", None)
    if not callable(name_getter):
        return None
    raw_name = str(_safe(partial(name_getter, 0), "") or "")
    normalized = re.sub(r"[^a-z]", "", raw_name.casefold())
    if normalized in _ACTIVATION_ACTION_NAMES:
        return 0, "activate"
    if normalized == "setfocus":
        return 0, "focus_only"
    return 0, "unknown"


def _has_state(node: Any, state: int) -> bool:
    getter = getattr(node, "get_state_set", None)
    states = _safe(getter) if callable(getter) else None
    contains = getattr(states, "contains", None)
    return bool(_safe(partial(contains, state), False)) if callable(contains) else False


def _wait_for_state(node: Any, state: int, *, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _has_state(node, state):
            return True
        time.sleep(min(0.05, max(0.005, deadline - time.monotonic())))
    return _has_state(node, state)


def _path_distance(first: tuple[int, ...], second: tuple[int, ...]) -> int:
    common = _common_prefix_size(first, second)
    return len(first) + len(second) - 2 * common


def _testing_action_surface(apps: tuple[Any, ...], *, max_nodes: int) -> dict[str, object]:
    editable: list[tuple[int, tuple[int, ...], Any]] = []
    action_nodes: list[tuple[int, tuple[int, ...], Any, int, str]] = []
    send_labels: list[tuple[int, tuple[int, ...]]] = []
    titles: list[tuple[int, tuple[int, ...], str]] = []
    for app_index, app in enumerate(apps):
        stack: list[tuple[Any, tuple[int, ...], bool]] = [(app, (), False)]
        visited = 0
        while stack and visited < max_nodes:
            node, path, inside_list = stack.pop()
            visited += 1
            interfaces = _interfaces(node)
            role = str(_safe(node.get_role_name, "unknown") or "unknown")
            role_folded = role.casefold()
            protected_text = inside_list or role_folded in {
                "list",
                "list item",
                "list-item",
                "row",
            }
            showing = _is_showing(node)
            if "EditableText" in interfaces and showing:
                editable.append((app_index, path, node))
                # Never request an input control's accessible name: on some
                # clients it contains the current unsent draft.
                visible = ""
            elif protected_text:
                # Sidebar and transcript list subtrees can contain contacts,
                # sender labels and message bodies.  They are out of scope.
                visible = ""
            else:
                visible = _visible_text(node)
            if visible == "testing" and showing:
                titles.append((app_index, path, role))
            if showing and visible.casefold() in {"send", "send(s)", "发送", "发送(s)"}:
                send_labels.append((app_index, path))
            action = _single_action(node)
            if action is not None and showing:
                action_index, action_kind = action
                action_nodes.append((app_index, path, node, action_index, action_kind))
            count = min(max(int(_safe(node.get_child_count, 0) or 0), 0), max_nodes)
            for index in reversed(range(count)):
                child = _safe(partial(node.get_child_at_index, index))
                if child is not None:
                    stack.append((child, (*path, index), protected_text))

    sends: list[tuple[int, tuple[int, ...], Any, int, str]] = []
    for label_app, label_path in send_labels:
        ranked_actions = sorted(
            (
                _path_distance(label_path, action_path),
                action_path,
                action_node,
                action_index,
                action_kind,
            )
            for action_app, action_path, action_node, action_index, action_kind in action_nodes
            if action_app == label_app
            if _path_distance(label_path, action_path) <= 2
        )
        if not ranked_actions:
            continue
        best_distance = ranked_actions[0][0]
        best_actions = [value for value in ranked_actions if value[0] == best_distance]
        if len(best_actions) == 1:
            _distance, action_path, action_node, action_index, action_kind = best_actions[0]
            sends.append((label_app, action_path, action_node, action_index, action_kind))
    sends = list(
        {
            (send_app, send_path, action_index, action_kind): (
                send_app,
                send_path,
                send_node,
                action_index,
                action_kind,
            )
            for send_app, send_path, send_node, action_index, action_kind in sends
        }.values()
    )

    pairs = sorted(
        (
            _path_distance(input_path, send_path),
            app_index,
            input_path,
            send_path,
            input_node,
            send_node,
            action_index,
            action_kind,
        )
        for app_index, input_path, input_node in editable
        for send_app, send_path, send_node, action_index, action_kind in sends
        if send_app == app_index
    )
    pair_diagnostics = [
        {
            "tree_distance": distance,
            "input_selector": input_path,
            "input_role": str(_safe(input_node.get_role_name, "unknown") or "unknown"),
            "input_window_extents": _window_extents(input_node),
            "send_selector": send_path,
            "send_role": str(_safe(send_node.get_role_name, "unknown") or "unknown"),
            "send_window_extents": _window_extents(send_node),
        }
        for (
            distance,
            _app_index,
            input_path,
            send_path,
            input_node,
            send_node,
            _action_index,
            _action_kind,
        ) in pairs[:8]
    ]
    pair_best_distance = pairs[0][0] if pairs else None
    best = [pair for pair in pairs if pair[0] == pair_best_distance]
    candidate: dict[str, object] | None = None
    if len(best) == 1:
        (
            _distance,
            app_index,
            input_path,
            send_path,
            input_node,
            send_node,
            action_index,
            action_kind,
        ) = best[0]
        ranked_titles = sorted(
            (
                _common_prefix_size(path, input_path),
                path,
                role,
            )
            for title_app, path, role in titles
            if title_app == app_index
            if not path[: len(input_path)] == input_path
        )
        title_best = ranked_titles[-1:] if ranked_titles else []
        if title_best and sum(
            1 for score, *_rest in ranked_titles if score == title_best[0][0]
        ) == 1:
            _score, title_path, title_role = title_best[0]
            shape = {
                "title_selector": title_path,
                "title_role": title_role,
                "input_selector": input_path,
                "input_role": str(_safe(input_node.get_role_name, "unknown") or "unknown"),
                "send_selector": send_path,
                "send_role": str(_safe(send_node.get_role_name, "unknown") or "unknown"),
                "send_action_index": action_index,
                "send_action_kind": action_kind,
                "send_activation_proven": action_kind == "activate",
            }
            candidate = {
                **shape,
                "input_window_extents": _window_extents(input_node),
                "send_window_extents": _window_extents(send_node),
                "surface_sha256": hashlib.sha256(
                    json.dumps(shape, sort_keys=True).encode("ascii")
                ).hexdigest(),
            }
    return {
        "schema_version": 1,
        "matched_app_count": len(apps),
        "testing_text_match_count": len(titles),
        "send_label_match_count": len(send_labels),
        "editable_candidate_count": len(editable),
        "send_action_candidate_count": len(sends),
        "pair_diagnostics": pair_diagnostics,
        "candidate": candidate,
        "passed": candidate is not None,
        "actions_performed": 0,
    }


def testing_action_surface_probe(
    target_pids: frozenset[int], *, max_nodes: int
) -> dict[str, object]:
    if not target_pids or any(pid <= 0 for pid in target_pids):
        raise ValueError("one or more positive target PIDs are required")
    if not 100 <= max_nodes <= 20_000:
        raise ValueError("probe bounds are outside the safe range")
    atspi = _load_atspi()
    atspi.init()
    desktop = atspi.get_desktop(0)
    count = min(max(int(_safe(desktop.get_child_count, 0) or 0), 0), 1_000)
    apps = tuple(
        app
        for index in range(count)
        if (app := _safe(partial(desktop.get_child_at_index, index))) is not None
        and int(_safe(app.get_process_id, 0) or 0) in target_pids
    )
    if not apps:
        raise RuntimeError("wechat application not found")
    return _testing_action_surface(apps, max_nodes=max_nodes)


def _testing_focus_only(
    apps: tuple[Any, ...], *, max_nodes: int, timeout_seconds: float = 2.0
) -> dict[str, object]:
    surface = _testing_action_surface(apps, max_nodes=max_nodes)
    candidate = surface.get("candidate")
    if surface.get("passed") is not True or not isinstance(candidate, dict) or len(apps) != 1:
        raise RuntimeError("testing action surface is ambiguous")
    title_node = _node_at(apps[0], _selector(candidate.get("title_selector")))
    send_node = _node_at(apps[0], _selector(candidate.get("send_selector")))
    action_index = candidate.get("send_action_index")
    if (
        title_node is None
        or send_node is None
        or action_index != 0
        or candidate.get("send_action_kind") != "focus_only"
        or not _is_showing(title_node)
        or not _is_showing(send_node)
        or _visible_text(title_node) != "testing"
    ):
        raise RuntimeError("testing focus precondition is unsafe")
    action_getter = getattr(send_node, "get_action_iface", None)
    action = _safe(action_getter) if callable(action_getter) else None
    action_method = getattr(action, "do_action", None)
    if not callable(action_method):
        raise RuntimeError("testing focus action is unavailable")
    focused_before = _has_state(send_node, _STATE_FOCUSED)
    action_returned = bool(_safe(partial(action_method, action_index), False))
    focused_after = _wait_for_state(
        send_node, _STATE_FOCUSED, timeout_seconds=timeout_seconds
    )
    title_still_proven = _is_showing(title_node) and _visible_text(title_node) == "testing"
    return {
        "schema_version": 1,
        "surface_sha256": candidate.get("surface_sha256"),
        "focus_action_returned": action_returned,
        "focused_before": focused_before,
        "focused_after": focused_after,
        "title_still_proven": title_still_proven,
        "passed": action_returned and focused_after and title_still_proven,
        "actions_performed": 1,
    }


def testing_focus_only_probe(
    target_pids: frozenset[int], *, max_nodes: int
) -> dict[str, object]:
    if not target_pids or any(pid <= 0 for pid in target_pids):
        raise ValueError("one or more positive target PIDs are required")
    if not 100 <= max_nodes <= 20_000:
        raise ValueError("probe bounds are outside the safe range")
    atspi = _load_atspi()
    atspi.init()
    desktop = atspi.get_desktop(0)
    count = min(max(int(_safe(desktop.get_child_count, 0) or 0), 0), 1_000)
    apps = tuple(
        app
        for index in range(count)
        if (app := _safe(partial(desktop.get_child_at_index, index))) is not None
        and int(_safe(app.get_process_id, 0) or 0) in target_pids
    )
    return _testing_focus_only(apps, max_nodes=max_nodes)


def _testing_input_focus_only(
    apps: tuple[Any, ...], *, max_nodes: int, timeout_seconds: float = 2.0
) -> dict[str, object]:
    surface = _testing_action_surface(apps, max_nodes=max_nodes)
    candidate = surface.get("candidate")
    if surface.get("passed") is not True or not isinstance(candidate, dict) or len(apps) != 1:
        raise RuntimeError("testing action surface is ambiguous")
    title_node = _node_at(apps[0], _selector(candidate.get("title_selector")))
    input_node = _node_at(apps[0], _selector(candidate.get("input_selector")))
    if (
        title_node is None
        or input_node is None
        or not _is_showing(title_node)
        or not _is_showing(input_node)
        or _visible_text(title_node) != "testing"
    ):
        raise RuntimeError("testing input focus precondition is unsafe")
    component_getter = getattr(input_node, "get_component_iface", None)
    component = _safe(component_getter) if callable(component_getter) else None
    grab_focus = getattr(component, "grab_focus", None)
    if not callable(grab_focus):
        raise RuntimeError("testing input focus action is unavailable")
    focused_before = _has_state(input_node, _STATE_FOCUSED)
    action_returned = bool(_safe(grab_focus, False))
    input_path = _selector(candidate.get("input_selector"))
    deadline = time.monotonic() + timeout_seconds
    focused_nodes: list[tuple[tuple[int, ...], str, tuple[str, ...]]] = []
    while time.monotonic() < deadline:
        focused_nodes = []
        stack: list[tuple[Any, tuple[int, ...]]] = [(apps[0], ())]
        visited = 0
        while stack and visited < max_nodes:
            node, path = stack.pop()
            visited += 1
            if _has_state(node, _STATE_FOCUSED):
                focused_nodes.append(
                    (
                        path,
                        str(_safe(node.get_role_name, "unknown") or "unknown"),
                        _interfaces(node),
                    )
                )
            count = min(max(int(_safe(node.get_child_count, 0) or 0), 0), max_nodes)
            for index in reversed(range(count)):
                child = _safe(partial(node.get_child_at_index, index))
                if child is not None:
                    stack.append((child, (*path, index)))
        if any(
            path[: len(input_path)] == input_path for path, _role, _interfaces_ in focused_nodes
        ):
            break
        time.sleep(min(0.05, max(0.005, deadline - time.monotonic())))
    input_focused_nodes = [
        item for item in focused_nodes if item[0][: len(input_path)] == input_path
    ]
    focused_after = len(input_focused_nodes) == 1
    title_still_proven = _is_showing(title_node) and _visible_text(title_node) == "testing"
    return {
        "schema_version": 1,
        "surface_sha256": candidate.get("surface_sha256"),
        "focus_action_returned": action_returned,
        "focused_before": focused_before,
        "focused_after": focused_after,
        "focused_node_count": len(focused_nodes),
        "input_focused_node_count": len(input_focused_nodes),
        "input_focused_nodes": [
            {"selector": path, "role": role, "interfaces": interfaces}
            for path, role, interfaces in input_focused_nodes[:2]
        ],
        "title_still_proven": title_still_proven,
        "passed": action_returned and focused_after and title_still_proven,
        "actions_performed": 1,
    }


def testing_input_focus_only_probe(
    target_pids: frozenset[int], *, max_nodes: int
) -> dict[str, object]:
    if not target_pids or any(pid <= 0 for pid in target_pids):
        raise ValueError("one or more positive target PIDs are required")
    if not 100 <= max_nodes <= 20_000:
        raise ValueError("probe bounds are outside the safe range")
    atspi = _load_atspi()
    atspi.init()
    desktop = atspi.get_desktop(0)
    count = min(max(int(_safe(desktop.get_child_count, 0) or 0), 0), 1_000)
    apps = tuple(
        app
        for index in range(count)
        if (app := _safe(partial(desktop.get_child_at_index, index))) is not None
        and int(_safe(app.get_process_id, 0) or 0) in target_pids
    )
    return _testing_input_focus_only(apps, max_nodes=max_nodes)


def _selector(value: object) -> tuple[int, ...]:
    if not isinstance(value, tuple) or not value:
        raise RuntimeError("surface selector is unavailable")
    if any(not isinstance(part, int) or part < 0 for part in value):
        raise RuntimeError("surface selector is invalid")
    return value


def _bounded_draft_name(node: Any, *, maximum: int = 128) -> str | None:
    """Read a draft only for exact local classification; callers must not emit it."""
    getter = getattr(node, "get_name", None)
    if not callable(getter):
        return None
    value = _safe(getter)
    if not isinstance(value, str) or len(value) > maximum:
        return None
    return value


def _bounded_text_value(node: Any, *, maximum: int = 128) -> str | None:
    getter = getattr(node, "get_text_iface", None)
    text = _safe(getter) if callable(getter) else None
    count_getter = getattr(text, "get_character_count", None)
    text_getter = getattr(text, "get_text", None)
    if not callable(count_getter) or not callable(text_getter):
        return None
    count = _safe(count_getter, -1)
    if not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= maximum:
        return None
    return str(_safe(partial(text_getter, 0, count), "") or "")


def _without_terminal_separator(value: str) -> str:
    # Qt may expose one or more paragraph separators around an editable value.
    # Do not strip spaces or other printable content: commit checks stay exact.
    return value.strip("\r\n\u2028\u2029")


def _known_draft_equals(node: Any, expected: str) -> bool:
    values = (_bounded_draft_name(node), _bounded_text_value(node))
    return any(
        value is not None and _without_terminal_separator(value) == expected
        for value in values
    )


def _generated_canary_draft(node: Any) -> str | None:
    for raw in (_bounded_draft_name(node), _bounded_text_value(node)):
        if raw is not None:
            value = _without_terminal_separator(raw)
            if re.fullmatch(r"LB26_SEND_[0-9a-f]{16}", value):
                return value
    return None


def _draft_is_empty(node: Any) -> bool:
    value = _bounded_draft_name(node)
    return value is not None and _without_terminal_separator(value) == ""


def _clear_known_canary(node: Any, editable: Any, canary: str) -> None:
    setter = getattr(editable, "set_text_contents", None)
    if callable(setter) and _known_draft_equals(node, canary):
        _safe(partial(setter, ""), False)


def _testing_send_canary(
    apps: tuple[Any, ...],
    *,
    max_nodes: int,
    timeout_seconds: int,
    use_existing_canary: bool = False,
    operator_confirmed_empty: bool = False,
    precommit_only: bool = False,
    keyboard_commit: Callable[[], bool] | None = None,
) -> dict[str, object]:
    surface = _testing_action_surface(apps, max_nodes=max_nodes)
    candidate = surface.get("candidate")
    if surface.get("passed") is not True or not isinstance(candidate, dict):
        raise RuntimeError("testing action surface is ambiguous")
    app_index = 0
    title_path = _selector(candidate.get("title_selector"))
    input_path = _selector(candidate.get("input_selector"))
    send_path = _selector(candidate.get("send_selector"))
    action_index = candidate.get("send_action_index")
    action_kind = candidate.get("send_action_kind")
    surface_hash = candidate.get("surface_sha256")
    if (
        not isinstance(action_index, int)
        or isinstance(action_index, bool)
        or action_index != 0
        or action_kind not in {"activate", "focus_only"}
        or not isinstance(surface_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", surface_hash) is None
    ):
        raise RuntimeError("testing action surface is invalid")
    if len(apps) != 1:
        raise RuntimeError("testing app is ambiguous")
    app = apps[app_index]
    title_node = _node_at(app, title_path)
    input_node = _node_at(app, input_path)
    send_node = _node_at(app, send_path)
    if (
        title_node is None
        or input_node is None
        or send_node is None
        or not _is_showing(title_node)
        or not _is_showing(input_node)
        or not _is_showing(send_node)
        or _visible_text(title_node) != "testing"
    ):
        raise RuntimeError("testing pre-commit state is unsafe")
    editable_getter = getattr(input_node, "get_editable_text_iface", None)
    if not callable(editable_getter):
        raise RuntimeError("testing input is not editable")
    editable = _safe(editable_getter)
    setter = getattr(editable, "set_text_contents", None)
    if not callable(setter):
        raise RuntimeError("testing input setter is unavailable")
    existing_canary = _generated_canary_draft(input_node)
    if use_existing_canary:
        if existing_canary is None:
            raise RuntimeError("testing draft is not a generated canary")
        canary = existing_canary
        created_here = False
    else:
        if not _draft_is_empty(input_node) and not operator_confirmed_empty:
            raise RuntimeError("testing pre-commit draft is not empty")
        canary = f"LB26_SEND_{secrets.token_hex(8)}"
        created_here = True

    action_getter = getattr(send_node, "get_action_iface", None)
    action = _safe(action_getter) if callable(action_getter) else None
    action_method = getattr(action, "do_action", None)
    if not callable(action_method):
        raise RuntimeError("testing send action is unavailable")

    focus_action_invoked = False
    focus_action_returned = False
    focused_before_commit = False
    keyboard_event_invoked = False
    keyboard_event_returned = False
    setter_returned = use_existing_canary
    draft_confirmation = "exact_atspi" if use_existing_canary else "unavailable"
    precommit_stage = "write_confirmation"
    try:
        if not use_existing_canary:
            pre_write_marker = (
                _bounded_draft_name(input_node),
                _bounded_text_value(input_node),
            )
            setter_returned = bool(_safe(partial(setter, canary), False))
            exact_draft = _known_draft_equals(input_node, canary)
            marker_changed = pre_write_marker != (
                _bounded_draft_name(input_node),
                _bounded_text_value(input_node),
            )
            if exact_draft:
                draft_confirmation = "exact_atspi"
            elif operator_confirmed_empty and setter_returned:
                draft_confirmation = "operator_plus_setter"
            if (
                not setter_returned
                or (not marker_changed and not operator_confirmed_empty)
                or draft_confirmation == "unavailable"
            ):
                raise RuntimeError("testing canary draft was not confirmed")
        precommit_stage = "surface_revalidation"
        commit_surface = _testing_action_surface(apps, max_nodes=max_nodes)
        commit_candidate = commit_surface.get("candidate")
        if (
            commit_surface.get("passed") is not True
            or not isinstance(commit_candidate, dict)
            or commit_candidate.get("surface_sha256") != surface_hash
            or commit_candidate.get("send_action_kind") != action_kind
            or _visible_text(title_node) != "testing"
            or not _is_showing(title_node)
            or (
                draft_confirmation == "exact_atspi"
                and not _known_draft_equals(input_node, canary)
            )
        ):
            raise RuntimeError("testing surface changed before commit")
        if action_kind == "focus_only":
            if keyboard_commit is None:
                raise RuntimeError("testing keyboard commit is unavailable")
            precommit_stage = "focus"
            focus_action_invoked = True
            focus_action_returned = bool(_safe(partial(action_method, action_index), False))
            focused_before_commit = focus_action_returned and _wait_for_state(
                send_node, _STATE_FOCUSED, timeout_seconds=2.0
            )
            focused_surface = _testing_action_surface(apps, max_nodes=max_nodes)
            focused_candidate = focused_surface.get("candidate")
            if (
                not focused_before_commit
                or not isinstance(focused_candidate, dict)
                or focused_candidate.get("surface_sha256") != surface_hash
                or _visible_text(title_node) != "testing"
                or not _is_showing(title_node)
            ):
                raise RuntimeError("testing send control focus was not proven")
    except BaseException:
        cleanup_returned = False
        if created_here and setter_returned and _visible_text(title_node) == "testing":
            cleanup_returned = bool(_safe(partial(setter, ""), False))
        if precommit_only:
            return {
                "schema_version": 1,
                "surface_sha256": surface_hash,
                "canary_sha256": hashlib.sha256(canary.encode("ascii")).hexdigest(),
                "precommit_stage": precommit_stage,
                "passed": False,
                "cleanup_returned": cleanup_returned,
                "keyboard_events_performed": 0,
                "actions_performed": int(focus_action_invoked),
            }
        raise

    if precommit_only:
        cleanup_returned = bool(_safe(partial(setter, ""), False)) if created_here else False
        return {
            "schema_version": 1,
            "surface_sha256": surface_hash,
            "canary_sha256": hashlib.sha256(canary.encode("ascii")).hexdigest(),
            "precommit_stage": "ready",
            "passed": True,
            "draft_confirmation": draft_confirmation,
            "cleanup_returned": cleanup_returned,
            "keyboard_events_performed": 0,
            "actions_performed": int(focus_action_invoked),
        }

    if action_kind == "focus_only":
        # From this point on the external outcome may be unknown. Never retry.
        keyboard_event_invoked = True
        keyboard_event_returned = bool(_safe(keyboard_commit, False))  # type: ignore[arg-type]
        commit_returned = keyboard_event_returned
        actions_performed = 2
    else:
        # From this point on the external outcome may be unknown. Never retry.
        commit_returned = bool(_safe(partial(action_method, action_index), False))
        actions_performed = 1
    deadline = time.monotonic() + timeout_seconds
    transcript_matches: list[dict[str, object]] = []
    while time.monotonic() < deadline:
        observed = _canary_matches(apps, {"send": canary}, max_nodes=max_nodes)["send"]
        transcript_matches = [
            match
            for match in observed
            if str(match.get("role", "")).casefold() in _MESSAGE_ITEM_ROLES
            and str(match.get("parent_role", "")).casefold() == "list"
        ]
        if len(transcript_matches) == 1 and _draft_is_empty(input_node):
            break
        time.sleep(min(0.5, max(0.01, deadline - time.monotonic())))
    readback_count = len(transcript_matches)
    draft_empty = _draft_is_empty(input_node)
    readback_observed = readback_count == 1 and draft_empty
    evidence = transcript_matches[0] if readback_observed else {}
    return {
        "schema_version": 1,
        "surface_sha256": surface_hash,
        "canary_sha256": hashlib.sha256(canary.encode("ascii")).hexdigest(),
        "input_was_empty": not use_existing_canary,
        "operator_confirmed_empty": operator_confirmed_empty,
        "draft_confirmation": draft_confirmation,
        "used_existing_canary": use_existing_canary,
        "commit_mechanism": action_kind,
        "send_action_invoked": True,
        "send_action_returned": commit_returned,
        "focus_action_invoked": focus_action_invoked,
        "focus_action_returned": focus_action_returned,
        "focused_before_commit": focused_before_commit,
        "keyboard_event_invoked": keyboard_event_invoked,
        "keyboard_event_returned": keyboard_event_returned,
        "readback_match_count": readback_count,
        "readback_item_window_extents": evidence.get("item_window_extents"),
        "draft_empty_after": draft_empty,
        "direction_proven": False,
        "acknowledged": False,
        "outcome": "readback_unattributed" if readback_observed else "unknown",
        "actions_performed": actions_performed,
        "retry_allowed": False,
    }


def _testing_draft_state(
    apps: tuple[Any, ...], *, max_nodes: int
) -> dict[str, object]:
    surface = _testing_action_surface(apps, max_nodes=max_nodes)
    candidate = surface.get("candidate")
    if surface.get("passed") is not True or not isinstance(candidate, dict) or len(apps) != 1:
        raise RuntimeError("testing action surface is ambiguous")
    input_node = _node_at(apps[0], _selector(candidate.get("input_selector")))
    if input_node is None or not _is_showing(input_node):
        raise RuntimeError("testing input is unavailable")
    canary = _generated_canary_draft(input_node)
    if _draft_is_empty(input_node):
        state = "empty"
    elif canary is not None:
        state = "generated_canary"
    else:
        # Qt exposes the same non-empty accessible placeholder for a visually
        # empty editor. It is neither proof of content nor proof of emptiness.
        state = "unclassified"
    transcript_canaries: list[dict[str, object]] = []
    for app_index, app in enumerate(apps):
        stack: list[tuple[Any, tuple[int, ...]]] = [(app, ())]
        visited = 0
        while stack and visited < max_nodes:
            node, path = stack.pop()
            visited += 1
            value = _without_terminal_separator(_visible_text(node))
            parent = _parent(node)
            if (
                re.fullmatch(r"LB26_SEND_[0-9a-f]{16}", value)
                and str(_safe(node.get_role_name, "unknown") or "unknown").casefold()
                in _MESSAGE_ITEM_ROLES
                and str(
                    _safe(parent.get_role_name, "unknown") if parent is not None else "none"
                ).casefold()
                == "list"
            ):
                transcript_canaries.append(
                    {
                        "app_index": app_index,
                        "item_selector": path,
                        "canary_sha256": hashlib.sha256(value.encode("ascii")).hexdigest(),
                        "item_window_extents": _window_extents(node),
                    }
                )
            count = min(max(int(_safe(node.get_child_count, 0) or 0), 0), max_nodes)
            for index in reversed(range(count)):
                child = _safe(partial(node.get_child_at_index, index))
                if child is not None:
                    stack.append((child, (*path, index)))
    return {
        "schema_version": 1,
        "surface_sha256": candidate.get("surface_sha256"),
        "draft_state": state,
        "draft_canary_sha256": (
            hashlib.sha256(canary.encode("ascii")).hexdigest() if canary else None
        ),
        "transcript_canary_match_count": len(transcript_canaries),
        "transcript_canaries": transcript_canaries[-4:],
        "actions_performed": 0,
    }


def testing_send_canary_probe(
    target_pids: frozenset[int],
    *,
    max_nodes: int,
    timeout_seconds: int,
    use_existing_canary: bool = False,
    operator_confirmed_empty: bool = False,
    precommit_only: bool = False,
) -> dict[str, object]:
    if not target_pids or any(pid <= 0 for pid in target_pids):
        raise ValueError("one or more positive target PIDs are required")
    if not 100 <= max_nodes <= 20_000 or not 5 <= timeout_seconds <= 60:
        raise ValueError("probe bounds are outside the safe range")
    atspi = _load_atspi()
    atspi.init()
    desktop = atspi.get_desktop(0)
    count = min(max(int(_safe(desktop.get_child_count, 0) or 0), 0), 1_000)
    apps = tuple(
        app
        for index in range(count)
        if (app := _safe(partial(desktop.get_child_at_index, index))) is not None
        and int(_safe(app.get_process_id, 0) or 0) in target_pids
    )
    if len(apps) != 1:
        raise RuntimeError("wechat application is missing or ambiguous")
    return _testing_send_canary(
        apps,
        max_nodes=max_nodes,
        timeout_seconds=timeout_seconds,
        use_existing_canary=use_existing_canary,
        operator_confirmed_empty=operator_confirmed_empty,
        precommit_only=precommit_only,
        keyboard_commit=lambda: bool(
            atspi.generate_keyboard_event(0xFF0D, None, atspi.KeySynthType.SYM)
        ),
    )


def _testing_submit_confirmed_draft(
    apps: tuple[Any, ...],
    *,
    max_nodes: int,
    expected_canary_sha256: str,
    keyboard_commit: Callable[[], bool],
    focus_input: bool = False,
) -> dict[str, object]:
    if re.fullmatch(r"[0-9a-f]{64}", expected_canary_sha256) is None:
        raise ValueError("expected canary hash is invalid")
    surface = _testing_action_surface(apps, max_nodes=max_nodes)
    candidate = surface.get("candidate")
    if surface.get("passed") is not True or not isinstance(candidate, dict) or len(apps) != 1:
        raise RuntimeError("testing action surface is ambiguous")
    title_node = _node_at(apps[0], _selector(candidate.get("title_selector")))
    input_node = _node_at(apps[0], _selector(candidate.get("input_selector")))
    send_node = _node_at(apps[0], _selector(candidate.get("send_selector")))
    action_index = candidate.get("send_action_index")
    surface_hash = candidate.get("surface_sha256")
    if (
        title_node is None
        or input_node is None
        or send_node is None
        or action_index != 0
        or candidate.get("send_action_kind") != "focus_only"
        or not isinstance(surface_hash, str)
        or _visible_text(title_node) != "testing"
        or not _is_showing(title_node)
        or not _is_showing(send_node)
    ):
        raise RuntimeError("testing confirmed-draft precondition is unsafe")
    if focus_input:
        component_getter = getattr(input_node, "get_component_iface", None)
        component = _safe(component_getter) if callable(component_getter) else None
        focus_method = getattr(component, "grab_focus", None)
        if not callable(focus_method):
            raise RuntimeError("testing input focus action is unavailable")
        focus_returned = bool(_safe(focus_method, False))
        focus_ready = focus_returned
        focus_confirmation = "component_return_only"
    else:
        action_getter = getattr(send_node, "get_action_iface", None)
        action = _safe(action_getter) if callable(action_getter) else None
        focus_method = getattr(action, "do_action", None)
        if not callable(focus_method):
            raise RuntimeError("testing focus action is unavailable")
        focus_returned = bool(_safe(partial(focus_method, action_index), False))
        focus_ready = focus_returned and _wait_for_state(
            send_node, _STATE_FOCUSED, timeout_seconds=2.0
        )
        focus_confirmation = "atspi_focused_state"
    commit_surface = _testing_action_surface(apps, max_nodes=max_nodes)
    commit_candidate = commit_surface.get("candidate")
    if (
        not focus_ready
        or not isinstance(commit_candidate, dict)
        or commit_candidate.get("surface_sha256") != surface_hash
        or _visible_text(title_node) != "testing"
        or not _is_showing(title_node)
    ):
        raise RuntimeError("testing confirmed-draft focus was not proven")
    # External outcome may be unknown from here. This event is never retried.
    keyboard_returned = bool(_safe(keyboard_commit, False))
    return {
        "schema_version": 1,
        "surface_sha256": surface_hash,
        "canary_sha256": expected_canary_sha256,
        "focus_target": "input" if focus_input else "send_button",
        "focus_confirmation": focus_confirmation,
        "focus_action_returned": focus_returned,
        "focused_before_commit": focus_ready,
        "keyboard_event_invoked": True,
        "keyboard_event_returned": keyboard_returned,
        "acknowledged": False,
        "outcome": "unknown",
        "actions_performed": 2,
        "retry_allowed": False,
    }


def _alt_s_keyboard_event(atspi: Any) -> bool:
    alt_mask = 1 << int(atspi.ModifierType.ALT)
    alt_locked = False
    key_returned = False
    alt_unlocked = False
    try:
        alt_locked = bool(
            atspi.generate_keyboard_event(
                alt_mask, "", atspi.KeySynthType.LOCKMODIFIERS
            )
        )
        if alt_locked:
            key_returned = bool(
                atspi.generate_keyboard_event(ord("s"), None, atspi.KeySynthType.SYM)
            )
    finally:
        alt_unlocked = bool(
            _safe(
                partial(
                    atspi.generate_keyboard_event,
                    alt_mask,
                    "",
                    atspi.KeySynthType.UNLOCKMODIFIERS,
                ),
                False,
            )
        )
    return alt_locked and key_returned and alt_unlocked


def testing_submit_confirmed_draft_probe(
    target_pids: frozenset[int],
    *,
    max_nodes: int,
    expected_canary_sha256: str,
    use_space_key: bool = False,
    use_alt_s: bool = False,
    focus_input: bool = False,
) -> dict[str, object]:
    if not target_pids or any(pid <= 0 for pid in target_pids):
        raise ValueError("one or more positive target PIDs are required")
    if not 100 <= max_nodes <= 20_000:
        raise ValueError("probe bounds are outside the safe range")
    atspi = _load_atspi()
    atspi.init()
    desktop = atspi.get_desktop(0)
    count = min(max(int(_safe(desktop.get_child_count, 0) or 0), 0), 1_000)
    apps = tuple(
        app
        for index in range(count)
        if (app := _safe(partial(desktop.get_child_at_index, index))) is not None
        and int(_safe(app.get_process_id, 0) or 0) in target_pids
    )
    if use_space_key and use_alt_s:
        raise ValueError("only one confirmed-draft key may be selected")

    def keyboard_commit() -> bool:
        if use_alt_s:
            return _alt_s_keyboard_event(atspi)
        return bool(
            atspi.generate_keyboard_event(
                0x20 if use_space_key else 0xFF0D,
                None,
                atspi.KeySynthType.SYM,
            )
        )

    report = _testing_submit_confirmed_draft(
        apps,
        max_nodes=max_nodes,
        expected_canary_sha256=expected_canary_sha256,
        keyboard_commit=keyboard_commit,
        focus_input=focus_input,
    )
    report["commit_key"] = (
        "alt_s" if use_alt_s else "space" if use_space_key else "return"
    )
    return report


def testing_draft_state_probe(
    target_pids: frozenset[int], *, max_nodes: int
) -> dict[str, object]:
    if not target_pids or any(pid <= 0 for pid in target_pids):
        raise ValueError("one or more positive target PIDs are required")
    if not 100 <= max_nodes <= 20_000:
        raise ValueError("probe bounds are outside the safe range")
    atspi = _load_atspi()
    atspi.init()
    desktop = atspi.get_desktop(0)
    count = min(max(int(_safe(desktop.get_child_count, 0) or 0), 0), 1_000)
    apps = tuple(
        app
        for index in range(count)
        if (app := _safe(partial(desktop.get_child_at_index, index))) is not None
        and int(_safe(app.get_process_id, 0) or 0) in target_pids
    )
    if len(apps) != 1:
        raise RuntimeError("wechat application is missing or ambiguous")
    return _testing_draft_state(apps, max_nodes=max_nodes)


def _match_path(match: dict[str, object], key: str) -> tuple[int, ...]:
    value = match.get(key)
    if not isinstance(value, tuple) or any(not isinstance(part, int) for part in value):
        return ()
    return value


def _message_item(node: Any) -> tuple[Any, int]:
    """Return the nearest message-row ancestor and the number of hops to it."""
    current = node
    for hops in range(9):
        role = str(_safe(current.get_role_name, "unknown") or "unknown").casefold()
        if role in _MESSAGE_ITEM_ROLES:
            return current, hops
        parent = _parent(current)
        if parent is None:
            break
        current = parent
    return node, 0


def _preceding_sibling_extents(
    item: Any, item_path: tuple[int, ...]
) -> tuple[int, int, int, int] | None:
    if not item_path or item_path[-1] <= 0:
        return None
    parent = _parent(item)
    if parent is None:
        return None
    getter = getattr(parent, "get_child_at_index", None)
    if not callable(getter):
        return None
    previous = _safe(partial(getter, item_path[-1] - 1))
    return None if previous is None else _window_extents(previous)


def _canary_matches(
    apps: tuple[Any, ...], tokens: dict[str, str], *, max_nodes: int
) -> dict[str, list[dict[str, object]]]:
    matches: dict[str, list[dict[str, object]]] = {key: [] for key in tokens}
    reverse = {value: key for key, value in tokens.items()}
    for app_index, app in enumerate(apps):
        stack: list[tuple[Any, str]] = [(app, "0")]
        visited = 0
        while stack and visited < max_nodes:
            node, path = stack.pop()
            visited += 1
            label = reverse.get(_visible_text(node))
            if label is not None:
                message_item, item_hops = _message_item(node)
                node_path = _path_parts(path)
                item_path = node_path[:-item_hops] if item_hops else node_path
                body_relative_path = node_path[-item_hops:] if item_hops else ()
                parent = _parent(node)
                matches[label].append(
                    {
                        "app_index": app_index,
                        "path": path,
                        "item_path": item_path,
                        "body_relative_path": body_relative_path,
                        "role": str(_safe(node.get_role_name, "unknown") or "unknown"),
                        "interfaces": _interfaces(node),
                        "parent_role": str(
                            _safe(parent.get_role_name, "unknown") if parent is not None else "none"
                        ),
                        "item_signature": _node_signature(message_item),
                        "attribute_keys": _attribute_keys(message_item),
                        "item_window_extents": _window_extents(message_item),
                        "preceding_sibling_window_extents": _preceding_sibling_extents(
                            message_item, item_path
                        ),
                    }
                )
            count = int(_safe(node.get_child_count, 0) or 0)
            if count < 0 or count > max_nodes:
                continue
            children: list[tuple[Any, str]] = []
            for index in range(count):
                child = _safe(partial(node.get_child_at_index, index))
                if child is not None:
                    children.append((child, f"{path}.{index}"))
            stack.extend(reversed(children))
    return matches


def _path_parts(path: str) -> tuple[int, ...]:
    return tuple(int(part) for part in path.split(".")[1:])


def _exact_text_paths(
    apps: tuple[Any, ...], expected: str, *, max_nodes: int
) -> tuple[tuple[int, tuple[int, ...]], ...]:
    results: list[tuple[int, tuple[int, ...]]] = []
    for app_index, app in enumerate(apps):
        stack: list[tuple[Any, tuple[int, ...]]] = [(app, ())]
        visited = 0
        while stack and visited < max_nodes:
            node, path = stack.pop()
            visited += 1
            if _visible_text(node) == expected:
                results.append((app_index, path))
            count = int(_safe(node.get_child_count, 0) or 0)
            if count < 0 or count > max_nodes:
                continue
            children: list[tuple[Any, tuple[int, ...]]] = []
            for index in range(count):
                child = _safe(partial(node.get_child_at_index, index))
                if child is not None:
                    children.append((child, (*path, index)))
            stack.extend(reversed(children))
    return tuple(results)


def _node_at(root: Any, path: tuple[int, ...]) -> Any | None:
    node = root
    for index in path:
        node = _safe(partial(node.get_child_at_index, index))
        if node is None:
            return None
    return node


def _common_prefix_size(first: tuple[int, ...], second: tuple[int, ...]) -> int:
    size = 0
    for left, right in zip(first, second, strict=False):
        if left != right:
            break
        size += 1
    return size


def _sender_identity_candidate(
    item: Any, body_path: tuple[int, ...]
) -> tuple[tuple[int, ...] | None, str | None, str | None]:
    preferred = {"accessible-id", "automation-id", "id", "object-id"}
    candidates: list[tuple[tuple[int, ...], str, str]] = []
    stack: list[tuple[Any, tuple[int, ...]]] = [(item, ())]
    while stack and len(candidates) < 16:
        node, path = stack.pop()
        if path != body_path:
            for key, value in _attributes(node).items():
                if key.casefold() in preferred and value:
                    candidates.append((path, key, value))
        count = min(max(int(_safe(node.get_child_count, 0) or 0), 0), 64)
        for index in reversed(range(count)):
            child = _safe(partial(node.get_child_at_index, index))
            if child is not None:
                stack.append((child, (*path, index)))
    unique = sorted(set(candidates))
    return unique[0] if len(unique) == 1 else (None, None, None)


def semantic_probe(
    target_pids: frozenset[int],
    *,
    kind: str,
    duration_seconds: int,
    max_nodes: int,
) -> dict[str, object]:
    if kind not in {"private", "group"}:
        raise ValueError("kind must be private or group")
    if not 30 <= duration_seconds <= 600:
        raise ValueError("semantic probe duration must be between 30 and 600 seconds")
    atspi = _load_atspi()
    atspi.init()
    desktop = atspi.get_desktop(0)
    count = min(max(int(_safe(desktop.get_child_count, 0) or 0), 0), 1_000)
    apps = tuple(
        app
        for index in range(count)
        if (app := _safe(partial(desktop.get_child_at_index, index))) is not None
        and int(_safe(app.get_process_id, 0) or 0) in target_pids
    )
    if not apps:
        raise RuntimeError("wechat application not found")
    tokens = {
        "self": f"LB26_SELF_{secrets.token_hex(8)}",
        "inbound": f"LB26_PEER_{secrets.token_hex(8)}",
    }
    if kind == "group":
        tokens["inbound_continuation"] = f"LB26_PEER_CONT_{secrets.token_hex(8)}"
    account_phrase = getpass.getpass("Account enrollment phrase (hidden): ").strip()
    chat_title = getpass.getpass("Current chat title (hidden): ").strip()
    if not account_phrase or not chat_title:
        raise ValueError("account phrase and chat title are required")
    print(
        json.dumps(
            {
                "instruction": (
                    "send the self canary from this WeChat account and the inbound "
                    "canary from the peer; for a group, the same peer must send the "
                    "continuation canary immediately after the inbound canary"
                ),
                "kind": kind,
                "self_canary": tokens["self"],
                "inbound_canary": tokens["inbound"],
                "inbound_continuation_canary": tokens.get("inbound_continuation"),
                "expires_in_seconds": duration_seconds,
            },
            sort_keys=True,
        ),
        file=sys.stderr,
        flush=True,
    )
    # AT-SPI's Python event bindings have caused native crashes in the Linux
    # client despite bounded, explicit deregistration.  Enrollment is rare
    # and has a fixed duration, so a bounded read-only polling loop is safer
    # than registering callbacks in the accessibility process.
    deadline = time.monotonic() + duration_seconds
    latest: dict[str, list[dict[str, object]]] = {key: [] for key in tokens}
    while time.monotonic() < deadline:
        latest = _canary_matches(apps, tokens, max_nodes=max_nodes)
        if all(len(latest[key]) == 1 for key in tokens):
            break
        time.sleep(min(1.0, max(0.01, deadline - time.monotonic())))
    self_signature = latest["self"][0]["item_signature"] if len(latest["self"]) == 1 else None
    inbound_signature = (
        latest["inbound"][0]["item_signature"] if len(latest["inbound"]) == 1 else None
    )
    sender_keys = (
        latest["inbound"][0]["attribute_keys"]
        if kind == "group" and len(latest["inbound"]) == 1
        else ()
    )
    candidate: dict[str, object] | None = None
    header_proven = False
    sender_path: tuple[int, ...] | None = None
    sender_key: str | None = None
    sender_probe_fingerprint: str | None = None
    account_fingerprint = hashlib.sha256(account_phrase.encode()).hexdigest()
    if len(latest["self"]) == 1 and len(latest["inbound"]) == 1:
        self_item_path = _match_path(latest["self"][0], "item_path")
        inbound_item_path = _match_path(latest["inbound"][0], "item_path")
        self_body_path = _match_path(latest["self"][0], "body_relative_path")
        inbound_body_path = _match_path(latest["inbound"][0], "body_relative_path")
        if self_item_path and inbound_item_path:
            transcript_path = self_item_path[:-1]
            canary_app_index = int(str(latest["self"][0]["app_index"]))
            same_app = canary_app_index == int(
                str(latest["inbound"][0]["app_index"])
            )
            if transcript_path and same_app and transcript_path == inbound_item_path[:-1]:
                headers = _exact_text_paths(apps, chat_title, max_nodes=max_nodes)
                scored = sorted(
                    (
                        _common_prefix_size(path, transcript_path),
                        app_index,
                        path,
                    )
                    for app_index, path in headers
                    if app_index == canary_app_index
                    if not path[: len(transcript_path)] == transcript_path
                )
                best = scored[-1:] if scored else []
                if best and sum(1 for score, *_rest in scored if score == best[0][0]) == 1:
                    _score, app_index, header_path = best[0]
                    header_proven = True
                    if kind == "group":
                        inbound_item = _node_at(apps[app_index], inbound_item_path)
                        if inbound_item is not None:
                            sender_path, sender_key, sender_identity = _sender_identity_candidate(
                                inbound_item, inbound_body_path
                            )
                            if sender_identity:
                                sender_probe_fingerprint = hashlib.sha256(
                                    f"{account_fingerprint}\0{sender_identity}".encode()
                                ).hexdigest()
                    semantic_shape = {
                        "header_selector": header_path,
                        "transcript_selector": transcript_path,
                        "self_item_signature": self_signature,
                        "inbound_item_signature": inbound_signature,
                        "self_body_relative_path": self_body_path,
                        "inbound_body_relative_path": inbound_body_path,
                        "sender_relative_path": sender_path,
                        "sender_attribute_key": sender_key,
                    }
                    candidate = {
                        "chat_kind": kind,
                        "header_selector": header_path,
                        "header_fingerprint": hashlib.sha256(
                            chat_title.encode("utf-8")
                        ).hexdigest(),
                        "transcript_selector": transcript_path,
                        "self_item_signature": self_signature,
                        "inbound_item_signature": inbound_signature,
                        "self_body_relative_path": self_body_path,
                        "inbound_body_relative_path": inbound_body_path,
                        "sender_relative_path": sender_path,
                        "sender_attribute_key": sender_key,
                        "sender_probe_fingerprint": sender_probe_fingerprint,
                        "semantic_shape_sha256": hashlib.sha256(
                            json.dumps(semantic_shape, sort_keys=True).encode()
                        ).hexdigest(),
                    }
    group_sender_proven = bool(kind == "private" or (sender_path and sender_key))
    passed = bool(
        len(latest["self"]) == 1
        and len(latest["inbound"]) == 1
        and self_signature != inbound_signature
        and header_proven
        and group_sender_proven
        and candidate is not None
    )
    return {
        "schema_version": 1,
        "kind": kind,
        "matched_app_count": len(apps),
        "registered_event_scopes": 0,
        "event_counts": {},
        "self_match_count": len(latest["self"]),
        "inbound_match_count": len(latest["inbound"]),
        "inbound_continuation_match_count": len(latest.get("inbound_continuation", [])),
        "self_evidence": latest["self"][:1],
        "inbound_evidence": latest["inbound"][:1],
        "inbound_continuation_evidence": latest.get("inbound_continuation", [])[:1],
        "direction_distinct": bool(
            self_signature and inbound_signature and self_signature != inbound_signature
        ),
        "group_sender_attribute_keys": sender_keys,
        "group_sender_proven": group_sender_proven,
        "header_proven": header_proven,
        "account_fingerprint": account_fingerprint,
        "enrollment_candidate": candidate,
        "passed": passed,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sanitized read-only Linux WeChat AT-SPI probe")
    parser.add_argument("--pid", action="append", required=True, type=int)
    parser.add_argument("--max-nodes", type=int, default=DEFAULT_MAX_NODES)
    parser.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH)
    parser.add_argument("--semantic-kind", choices=("private", "group"))
    parser.add_argument("--duration-seconds", type=int, default=180)
    parser.add_argument("--testing-action-surface", action="store_true")
    parser.add_argument("--testing-focus-only", action="store_true")
    parser.add_argument("--testing-input-focus-only", action="store_true")
    parser.add_argument("--testing-send-canary", action="store_true")
    parser.add_argument("--testing-send-existing-canary", action="store_true")
    parser.add_argument("--operator-confirmed-empty-draft", action="store_true")
    parser.add_argument("--testing-send-precommit-only", action="store_true")
    parser.add_argument("--testing-submit-confirmed-draft", action="store_true")
    parser.add_argument("--expected-canary-sha256")
    parser.add_argument("--confirmed-draft-use-space", action="store_true")
    parser.add_argument("--confirmed-draft-use-alt-s", action="store_true")
    parser.add_argument("--confirmed-draft-focus-input", action="store_true")
    parser.add_argument("--testing-draft-state", action="store_true")
    parser.add_argument("--send-timeout-seconds", type=int, default=20)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.testing_draft_state:
            report = testing_draft_state_probe(
                frozenset(arguments.pid),
                max_nodes=arguments.max_nodes,
            )
        elif arguments.testing_submit_confirmed_draft:
            report = testing_submit_confirmed_draft_probe(
                frozenset(arguments.pid),
                max_nodes=arguments.max_nodes,
                expected_canary_sha256=str(arguments.expected_canary_sha256 or ""),
                use_space_key=arguments.confirmed_draft_use_space,
                use_alt_s=arguments.confirmed_draft_use_alt_s,
                focus_input=arguments.confirmed_draft_focus_input,
            )
        elif arguments.testing_send_existing_canary:
            report = testing_send_canary_probe(
                frozenset(arguments.pid),
                max_nodes=arguments.max_nodes,
                timeout_seconds=arguments.send_timeout_seconds,
                use_existing_canary=True,
            )
        elif arguments.testing_send_canary:
            report = testing_send_canary_probe(
                frozenset(arguments.pid),
                max_nodes=arguments.max_nodes,
                timeout_seconds=arguments.send_timeout_seconds,
                operator_confirmed_empty=arguments.operator_confirmed_empty_draft,
                precommit_only=arguments.testing_send_precommit_only,
            )
        elif arguments.testing_action_surface:
            report = testing_action_surface_probe(
                frozenset(arguments.pid),
                max_nodes=arguments.max_nodes,
            )
        elif arguments.testing_focus_only:
            report = testing_focus_only_probe(
                frozenset(arguments.pid),
                max_nodes=arguments.max_nodes,
            )
        elif arguments.testing_input_focus_only:
            report = testing_input_focus_only_probe(
                frozenset(arguments.pid),
                max_nodes=arguments.max_nodes,
            )
        elif arguments.semantic_kind:
            report = semantic_probe(
                frozenset(arguments.pid),
                kind=arguments.semantic_kind,
                duration_seconds=arguments.duration_seconds,
                max_nodes=arguments.max_nodes,
            )
        else:
            report = probe(
                frozenset(arguments.pid),
                max_nodes=arguments.max_nodes,
                max_depth=arguments.max_depth,
            )
    except Exception as exc:
        # Never print exception text: AT-SPI errors may contain visible UI text.
        print(json.dumps({"error": type(exc).__name__}, sort_keys=True))
        return 1
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
