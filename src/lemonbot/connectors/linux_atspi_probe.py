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
import queue
import secrets
import sys
import time
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
                parent = _parent(node)
                item = _parent(parent) if parent is not None else None
                matches[label].append(
                    {
                        "app_index": app_index,
                        "path": path,
                        "role": str(_safe(node.get_role_name, "unknown") or "unknown"),
                        "interfaces": _interfaces(node),
                        "parent_role": str(
                            _safe(parent.get_role_name, "unknown") if parent is not None else "none"
                        ),
                        "item_signature": _node_signature(item or parent or node),
                        "attribute_keys": _attribute_keys(parent or node),
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
    account_phrase = getpass.getpass("Account enrollment phrase (hidden): ").strip()
    chat_title = getpass.getpass("Current chat title (hidden): ").strip()
    if not account_phrase or not chat_title:
        raise ValueError("account phrase and chat title are required")
    print(
        json.dumps(
            {
                "instruction": (
                    "send the self canary from this WeChat account and the inbound "
                    "canary from the peer"
                ),
                "kind": kind,
                "self_canary": tokens["self"],
                "inbound_canary": tokens["inbound"],
                "expires_in_seconds": duration_seconds,
            },
            sort_keys=True,
        ),
        file=sys.stderr,
        flush=True,
    )
    event_counts: Counter[str] = Counter()
    triggers: queue.Queue[None] = queue.Queue(maxsize=1)

    def on_event(event: Any, *_args: object) -> None:
        event_counts[str(_safe(lambda: event.type, "unknown") or "unknown")] += 1
        try:
            triggers.put_nowait(None)
        except queue.Full:
            pass

    listener = atspi.EventListener.new(on_event, None)
    registered_types: list[str] = []
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
                registered_types.append(event_type)
            except Exception:  # noqa: S112 - each event family is capability-probed
                continue
    if not registered_types:
        raise RuntimeError("scoped AT-SPI event registration unavailable")
    try:
        deadline = time.monotonic() + duration_seconds
        latest: dict[str, list[dict[str, object]]] = {"self": [], "inbound": []}
        while time.monotonic() < deadline:
            try:
                triggers.get(timeout=min(1.0, max(0.01, deadline - time.monotonic())))
                time.sleep(0.5)
            except queue.Empty:
                pass
            latest = _canary_matches(apps, tokens, max_nodes=max_nodes)
            if len(latest["self"]) == 1 and len(latest["inbound"]) == 1:
                break
    finally:
        for event_type in sorted(set(registered_types)):
            _safe(partial(listener.deregister, event_type))
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
        self_path = _path_parts(str(latest["self"][0]["path"]))
        inbound_path = _path_parts(str(latest["inbound"][0]["path"]))
        if len(self_path) >= 3 and len(inbound_path) >= 3:
            self_item_path, inbound_item_path = self_path[:-2], inbound_path[:-2]
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
                                inbound_item, inbound_path[-2:]
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
                        "self_body_relative_path": self_path[-2:],
                        "inbound_body_relative_path": inbound_path[-2:],
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
                        "self_body_relative_path": self_path[-2:],
                        "inbound_body_relative_path": inbound_path[-2:],
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
        "registered_event_scopes": len(registered_types),
        "event_counts": dict(sorted(event_counts.items())),
        "self_match_count": len(latest["self"]),
        "inbound_match_count": len(latest["inbound"]),
        "self_evidence": latest["self"][:1],
        "inbound_evidence": latest["inbound"][:1],
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.semantic_kind:
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
