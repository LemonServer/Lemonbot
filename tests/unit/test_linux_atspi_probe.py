from __future__ import annotations

import json

import pytest

from lemonbot.connectors.linux_atspi_probe import _inspect_app, probe


class _Action:
    def get_n_actions(self) -> int:
        return 1


class _Node:
    def __init__(self, name: str, *children: _Node, pid: int = 1234) -> None:
        self._name = name
        self._children = children
        self._pid = pid

    def get_interfaces(self) -> list[str]:
        return ["Atspi.Text", "Atspi.Action"]

    def get_role_name(self) -> str:
        return "text"

    def get_name(self) -> str:
        return self._name

    def get_action_iface(self) -> _Action:
        return _Action()

    def get_child_count(self) -> int:
        return len(self._children)

    def get_child_at_index(self, index: int) -> _Node:
        return self._children[index]

    def get_process_id(self) -> int:
        return self._pid


def test_probe_report_never_emits_or_hashes_visible_text() -> None:
    first = _inspect_app(
        _Node("private contact", _Node("secret message")),
        max_nodes=100,
        max_depth=10,
    )
    second = _inspect_app(
        _Node("different contact", _Node("different message")),
        max_nodes=100,
        max_depth=10,
    )

    encoded = json.dumps(first)
    assert "private contact" not in encoded
    assert "secret message" not in encoded
    assert first["structure_sha256"] == second["structure_sha256"]
    assert first["action_slot_count"] == 2


def test_probe_requires_explicit_positive_target_pids() -> None:
    with pytest.raises(ValueError, match="positive target PIDs"):
        probe(frozenset(), max_nodes=100, max_depth=10)
