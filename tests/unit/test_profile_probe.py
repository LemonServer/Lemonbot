from __future__ import annotations

import json
from typing import Any

import pytest

from lemonbot.research.profile_probe import read_profile_identifier


class Node:
    def __init__(self, name: str = "", *children: Node, role: int = 20,
                 visible: bool = True, editable: bool = False) -> None:
        self.name, self.children, self.role = name, children, role
        self.visible, self.editable = visible, editable
        self.parent: Node | None = None
        for child in children:
            child.parent = self

    def get_state_set(self) -> Node:
        return self

    def contains(self, _state: int) -> bool:
        return self.visible

    def get_role(self) -> int:
        return self.role

    def get_interfaces(self) -> list[str]:
        return ["EditableText"] if self.editable else []

    def get_name(self) -> str:
        assert self.role not in (31, 32) and not self.editable
        return self.name

    def get_child_count(self) -> int:
        return len(self.children)

    def get_child_at_index(self, index: int) -> Node:
        return self.children[index]

    def get_parent(self) -> Node:
        assert self.parent is not None
        return self.parent

    def get_index_in_parent(self) -> int:
        return self.get_parent().children.index(self)


def field(label: str = "Weixin ID:", value: str = "private_identifier") -> Node:
    return Node("", Node(label, role=29), Node(value, role=29))


def test_profile_uses_label_value_pair_and_run_scoped_reference() -> None:
    popup = Node("", field(), Node("private draft", editable=True), Node("chat", role=31))
    report = read_profile_identifier(popup, session_salt=b"a" * 32)
    other = read_profile_identifier(popup, session_salt=b"b" * 32)
    assert report["identifier_ref"] != other["identifier_ref"]
    assert "private_identifier" not in json.dumps(report)
    assert report["actions_performed"] == 0
    assert report["identity_authorized"] is False
    assert report["value_selector"] == (0, 1)


@pytest.mark.parametrize("popup", [
    Node("", field(), field()), Node("", field(value="")),
    Node("", field(), visible=False), Node("", field(label="Nickname")),
    Node("", Node("", Node("Weixin ID", role=29), Node("value", role=29), Node())),
])
def test_profile_refuses_ambiguous_or_missing_field(popup: Node) -> None:
    with pytest.raises(ValueError):
        read_profile_identifier(popup, session_salt=b"a" * 32)


def test_profile_does_not_accept_partially_scanned_tree() -> None:
    with pytest.raises(ValueError):
        read_profile_identifier(Node("", field(), Node()), session_salt=b"a" * 32, max_nodes=3)


def test_profile_rejects_value_change_during_read(monkeypatch: pytest.MonkeyPatch) -> None:
    pair = field()
    values = iter(("before", "before", "after"))
    monkeypatch.setattr(pair.children[1], "get_name", lambda: next(values))
    with pytest.raises(ValueError, match="changed"):
        read_profile_identifier(Node("", pair), session_salt=b"a" * 32)


def test_pointer_accepts_only_window_pointer_permission() -> None:
    from lemonbot.research.portal_pointer import validate_pointer_stream

    assert validate_pointer_stream({
        "devices": 2, "streams": [(42, {"source_type": 2, "size": (640, 480)})],
    }) == (42, (640, 480))
    for changes in ({"devices": 3}, {"clipboard_enabled": True},
                    {"streams": [(42, {"source_type": 1, "size": (640, 480)})]},
                    {"streams": [(42, {"source_type": 2})]}):
        with pytest.raises(ValueError):
            validate_pointer_stream({
                "devices": 2, "streams": [(42, {"source_type": 2, "size": (640, 480)})],
                **changes,
            })


def test_pointer_releases_button_on_unknown_result_and_never_retries() -> None:
    from lemonbot.research.portal_pointer import PointerSession

    session = object.__new__(PointerSession)
    session.handle, session.node_id, session.logical_size = "session", 42, (640, 480)
    session.attempted = False
    events: list[tuple[str, tuple[object, ...]]] = []

    def notify(method: str, _signature: str, params: tuple[object, ...]) -> None:
        events.append((method, params))
        if method == "NotifyPointerButton" and params[-1] == 1:
            raise RuntimeError("unknown")

    session._notify = notify  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        session.click_once(50, 50)
    assert events[-1][1][-1] == 0
    with pytest.raises(ValueError):
        session.click_once(50, 50)
    assert len(events) == 3


@pytest.mark.parametrize("x,y", [(float("nan"), 1), (float("inf"), 1), (-1, 1), (640, 1)])
def test_pointer_rejects_invalid_points_without_events(x: Any, y: Any) -> None:
    from lemonbot.research.portal_pointer import PointerSession

    session = object.__new__(PointerSession)
    session.logical_size, session.attempted = (640, 480), False
    with pytest.raises(ValueError):
        session.click_once(x, y)
    assert not session.attempted
