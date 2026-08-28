"""Standalone, read-only AT-SPI surface probe for official Linux WeChat.

This module deliberately depends only on the Python standard library and the
distribution-provided ``python3-gi`` package.  The main Lemonbot virtualenv
launches it with ``/usr/bin/python3 -I`` so the probe never receives model
credentials or imports the Lemonbot runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
from collections import Counter
from collections.abc import Callable, Sequence
from functools import partial
from typing import Any

DEFAULT_MAX_NODES = 10_000
DEFAULT_MAX_DEPTH = 24


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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sanitized read-only Linux WeChat AT-SPI probe")
    parser.add_argument("--pid", action="append", required=True, type=int)
    parser.add_argument("--max-nodes", type=int, default=DEFAULT_MAX_NODES)
    parser.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
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
