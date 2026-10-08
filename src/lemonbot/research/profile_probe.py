"""Standalone testing-only profile reader; no UI actions or runtime connection."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import json
import os
import secrets
from pathlib import Path
from typing import Any

_LABEL = 29
_PROTECTED_ROLES = {31, 32}
_ID_LABELS = {"weixin id", "wechat id", "微信号"}


def _showing(node: Any) -> bool:
    states = node.get_state_set()
    return bool(states.contains(25) and states.contains(30))


def read_profile_identifier(
    popup: Any, *, session_salt: bytes, max_nodes: int = 256,
) -> dict[str, object]:
    """Inspect an already-bound popup; caller must independently verify testing.

    The reference is deliberately scoped to one call's salt, not a persistent
    contact identity. Native exceptions are sanitized by the process boundary.
    """
    if len(session_salt) < 32 or not 1 <= max_nodes <= 1000 or not _showing(popup):
        raise ValueError("profile bounds or state invalid")
    stack: list[tuple[Any, tuple[int, ...]]] = [(popup, ())]
    seen: set[Any] = set()
    candidates: list[tuple[Any, Any, tuple[int, ...], str]] = []
    visited = 0
    while stack and visited < max_nodes:
        node, path = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        visited += 1
        if not _showing(node):
            continue
        if int(node.get_role()) in _PROTECTED_ROLES:
            continue
        interfaces = node.get_interfaces()
        if any(str(value).rsplit(".", 1)[-1] == "EditableText" for value in interfaces):
            continue
        if int(node.get_role()) == _LABEL:
            label = str(node.get_name() or "").strip().rstrip(":：").strip().casefold()
            if label in _ID_LABELS:
                parent = node.get_parent()
                if parent.get_child_count() != 2 or node.get_index_in_parent() != 0:
                    raise ValueError("profile field structure ambiguous")
                value_node = parent.get_child_at_index(1)
                if int(value_node.get_role()) != _LABEL or not _showing(value_node):
                    raise ValueError("profile value unavailable")
                value = str(value_node.get_name() or "").strip()
                if not value or len(value) > 128:
                    raise ValueError("profile value invalid")
                candidates.append((node, value_node, (*path[:-1], 1), value))
        count = node.get_child_count()
        if not 0 <= count <= 100 or (count and len(path) >= 32):
            raise ValueError("profile subtree exceeds bounds")
        for index in reversed(range(count)):
            child = node.get_child_at_index(index)
            if child is None:
                raise ValueError("profile subtree incomplete")
            stack.append((child, (*path, index)))
    if stack or len(candidates) != 1:
        raise ValueError("profile identifier missing or ambiguous")
    label_node, value_node, value_path, value = candidates[0]
    if (
        not _showing(popup)
        or not _showing(label_node)
        or not _showing(value_node)
        or str(value_node.get_name() or "").strip() != value
    ):
        raise ValueError("profile changed during read")
    digest = hmac.new(session_salt, value.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "schema_version": 1,
        "identifier_kind": "displayed_weixin_id",
        "identifier_ref": f"profile_{digest}",
        "reference_scope": "probe_run",
        "value_selector": value_path,
        "nodes_visited": visited,
        "actions_performed": 0,
        "automatic_message_binding_proven": False,
        "identity_authorized": False,
    }


def probe(target_pids: set[int]) -> dict[str, object]:
    if not target_pids or any(pid <= 0 for pid in target_pids):
        raise ValueError("invalid process selection")
    if any(os.readlink(f"/proc/{pid}/exe") != "/opt/wechat/wechat" for pid in target_pids):
        raise ValueError("unexpected executable")
    source = Path(__file__).parents[1] / "connectors" / "linux_atspi_probe.py"
    spec = importlib.util.spec_from_file_location("standalone_atspi_probe", source)
    if spec is None or spec.loader is None:
        raise ValueError("probe runtime unavailable")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    atspi = helper._load_atspi()
    atspi.init()
    desktop = atspi.get_desktop(0)
    apps = [
        node for index in range(min(desktop.get_child_count(), 1000))
        if (node := desktop.get_child_at_index(index)) is not None
        and node.get_process_id() in target_pids
    ]
    surface = helper._testing_action_surface(tuple(apps), max_nodes=10_000)
    if len(apps) != 1 or surface.get("passed") is not True:
        raise ValueError("testing surface unavailable")
    app = apps[0]
    # This is the experimentally observed popup arrangement, not a general Qt
    # guarantee. Reject other arrangements instead of opening arbitrary windows.
    if app.get_child_count() != 2:
        raise ValueError("profile popup ambiguous")
    popup = app.get_child_at_index(1)
    report = read_profile_identifier(popup, session_salt=secrets.token_bytes(32))
    final = helper._testing_action_surface(tuple(apps), max_nodes=10_000)
    if (
        final.get("passed") is not True
        or final.get("candidate") != surface.get("candidate")
        or app.get_child_count() != 2
        or app.get_child_at_index(1) != popup
        or not _showing(popup)
    ):
        raise ValueError("testing or popup changed")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, action="append", required=True)
    arguments = parser.parse_args()
    try:
        report = probe(set(arguments.pid))
    except Exception:
        print(json.dumps({"error": "ProfileReadFailed", "actions_performed": 0}))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
