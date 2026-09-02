from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lemonbot import cli
from lemonbot.cli import app
from lemonbot.connectors.linux_atspi_probe import (
    _alt_s_keyboard_event,
    _canary_matches,
    _inspect_app,
    _testing_action_surface,
    _testing_draft_state,
    _testing_focus_only,
    _testing_input_focus_only,
    _testing_send_canary,
    _testing_submit_confirmed_draft,
    probe,
)


class _Action:
    def get_n_actions(self) -> int:
        return 1


class _Rectangle:
    x = 100
    y = 200
    width = 800
    height = 60


class _Component:
    def get_extents(self, coordinate_type: int) -> _Rectangle:
        assert coordinate_type == 1
        return _Rectangle()


class _SurfaceAction:
    def get_n_actions(self) -> int:
        return 1

    def get_action_name(self, index: int) -> str:
        assert index == 0
        return "click"


class _SurfaceStates:
    def contains(self, state: int) -> bool:
        return state in {25, 30}


class _SurfaceNode:
    def __init__(
        self,
        name: str,
        *children: _SurfaceNode,
        role: str = "panel",
        interfaces: tuple[str, ...] = (),
        forbid_name: bool = False,
    ) -> None:
        self._name = name
        self._children = children
        self._role = role
        self._interfaces = interfaces
        self._forbid_name = forbid_name
        self._parent: _SurfaceNode | None = None
        for child in children:
            child._parent = self

    def get_interfaces(self) -> list[str]:
        return [f"Atspi.{value}" for value in self._interfaces]

    def get_role_name(self) -> str:
        return self._role

    def get_name(self) -> str:
        if self._forbid_name:
            raise AssertionError("protected visible text must not be requested")
        return self._name

    def get_action_iface(self) -> _SurfaceAction | None:
        return _SurfaceAction() if "Action" in self._interfaces else None

    def get_child_count(self) -> int:
        return len(self._children)

    def get_child_at_index(self, index: int) -> _SurfaceNode:
        return self._children[index]

    def get_component_iface(self) -> _Component:
        return _Component()

    def get_state_set(self) -> _SurfaceStates:
        return _SurfaceStates()

    def get_parent(self) -> _SurfaceNode | None:
        return self._parent


class _DraftText:
    def __init__(self, state: dict[str, object]) -> None:
        self._state = state

    def get_character_count(self) -> int:
        return 1

    def get_text(self, start: int, end: int) -> str:
        return "\u2029"[start:end]


class _DraftEditable:
    def __init__(self, state: dict[str, object]) -> None:
        self._state = state

    def set_text_contents(self, value: str) -> bool:
        self._state["draft"] = value
        return True


class _DraftNode(_SurfaceNode):
    def __init__(self, state: dict[str, object]) -> None:
        super().__init__(
            "protected draft",
            role="text",
            interfaces=("EditableText",),
            forbid_name=True,
        )
        self._state = state

    def get_name(self) -> str:
        return str(self._state["draft"])

    def get_text_iface(self) -> _DraftText:
        return _DraftText(self._state)

    def get_editable_text_iface(self) -> _DraftEditable:
        return _DraftEditable(self._state)

    def get_component_iface(self) -> _InputComponent:
        return _InputComponent(self._state)

    def get_state_set(self) -> _InputStates:
        return _InputStates(self._state)


class _InputComponent(_Component):
    def __init__(self, state: dict[str, object]) -> None:
        self._state = state

    def grab_focus(self) -> bool:
        self._state["input_focused"] = True
        return True


class _InputStates(_SurfaceStates):
    def __init__(self, state: dict[str, object]) -> None:
        self._state = state

    def contains(self, state: int) -> bool:
        return super().contains(state) or (
            state == 12 and self._state.get("input_focused") is True
        )


class _CommitAction(_SurfaceAction):
    def __init__(self, state: dict[str, object]) -> None:
        self._state = state

    def do_action(self, index: int) -> bool:
        assert index == 0
        assert str(self._state["draft"]).startswith("LB26_SEND_")
        self._state["draft"] = ""
        self._state["invoked"] = True
        return True


class _CommitNode(_SurfaceNode):
    def __init__(self, state: dict[str, object]) -> None:
        super().__init__(
            "",
            _SurfaceNode("发送", role="label"),
            _SurfaceNode("Send", role="label"),
            role="push button",
            interfaces=("Action",),
        )
        self._state = state

    def get_action_iface(self) -> _CommitAction:
        return _CommitAction(self._state)


class _OpaqueDraftNode(_DraftNode):
    def get_name(self) -> str:
        return "fixed empty-editor placeholder"


class _FocusStates(_SurfaceStates):
    def __init__(self, state: dict[str, object]) -> None:
        self._state = state

    def contains(self, state: int) -> bool:
        return super().contains(state) or (state == 12 and self._state.get("focused") is True)


class _FocusAction(_SurfaceAction):
    def __init__(self, state: dict[str, object], *, proves_focus: bool = True) -> None:
        self._state = state
        self._proves_focus = proves_focus

    def get_action_name(self, index: int) -> str:
        assert index == 0
        return "SetFocus"

    def do_action(self, index: int) -> bool:
        assert index == 0
        self._state["focused"] = self._proves_focus
        return True


class _FocusCommitNode(_CommitNode):
    def __init__(self, state: dict[str, object], *, proves_focus: bool = True) -> None:
        super().__init__(state)
        self._proves_focus = proves_focus

    def get_action_iface(self) -> _FocusAction:
        return _FocusAction(self._state, proves_focus=self._proves_focus)

    def get_state_set(self) -> _FocusStates:
        return _FocusStates(self._state)


class _Node:
    def __init__(
        self, name: str, *children: _Node, pid: int = 1234, role: str = "text"
    ) -> None:
        self._name = name
        self._children = children
        self._pid = pid
        self._role = role
        self._parent: _Node | None = None
        for child in children:
            child._parent = self

    def get_interfaces(self) -> list[str]:
        return ["Atspi.Text", "Atspi.Action"]

    def get_role_name(self) -> str:
        return self._role

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

    def get_parent(self) -> _Node | None:
        return self._parent

    def get_attributes(self) -> list[str]:
        return []

    def get_component_iface(self) -> _Component:
        return _Component()


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


def test_testing_action_surface_is_unique_read_only_and_skips_sensitive_text() -> None:
    transcript = _SurfaceNode(
        "",
        _SurfaceNode("secret body", role="text", forbid_name=True),
        role="list",
        forbid_name=True,
    )
    input_node = _SurfaceNode(
        "unsent draft",
        role="entry",
        interfaces=("EditableText",),
        forbid_name=True,
    )
    send_control = _SurfaceNode(
        "",
        _SurfaceNode("发送", role="label"),
        _SurfaceNode("Send", role="label"),
        role="push button",
        interfaces=("Action",),
    )
    controls = _SurfaceNode("", input_node, send_control)
    app = _SurfaceNode(
        "",
        _SurfaceNode("testing", role="heading"),
        transcript,
        controls,
        role="application",
    )

    report = _testing_action_surface((app,), max_nodes=100)

    assert report["passed"] is True
    assert report["actions_performed"] == 0
    assert report["testing_text_match_count"] == 1
    assert report["send_label_match_count"] == 2
    assert report["editable_candidate_count"] == 1
    assert report["send_action_candidate_count"] == 1
    assert report["candidate"]["send_action_index"] == 0  # type: ignore[index]
    assert report["candidate"]["send_action_kind"] == "activate"  # type: ignore[index]
    assert report["candidate"]["send_activation_proven"] is True  # type: ignore[index]


def test_testing_action_probe_requires_confirmation() -> None:
    result = CliRunner().invoke(app, ["channel", "linux-atspi-testing-action-probe"])

    assert result.exit_code == 2
    assert "testing" in result.output


def test_testing_action_probe_filters_child_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "_wechat_pids", lambda _pid: (1234,))
    monkeypatch.setattr(cli, "_probe_command", lambda _pids, _max_nodes: (["probe"], {}))
    child_report = {
        "schema_version": 1,
        "matched_app_count": 1,
        "testing_text_match_count": 1,
        "send_label_match_count": 2,
        "editable_candidate_count": 1,
        "send_action_candidate_count": 1,
        "candidate": {
            "title_selector": [0],
            "title_role": "heading",
            "input_selector": [2, 0],
            "input_role": "entry",
            "send_selector": [2, 1],
            "send_role": "push button",
            "send_action_index": 0,
            "send_action_kind": "focus_only",
            "send_activation_proven": False,
            "input_window_extents": [100, 200, 800, 60],
            "send_window_extents": [100, 200, 800, 60],
            "surface_sha256": "a" * 64,
        },
        "passed": True,
        "actions_performed": 0,
        "untrusted": "secret body",
    }
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(child_report).encode()
        ),
    )

    result = CliRunner().invoke(
        app,
        ["channel", "linux-atspi-testing-action-probe", "--confirm-testing"],
    )

    assert result.exit_code == 0
    assert "secret body" not in result.output
    assert "input_window_extents" not in result.output
    assert '"actions_performed": 0' in result.output


def _testing_send_tree(state: dict[str, object]) -> _SurfaceNode:
    controls = _SurfaceNode("", _DraftNode(state), _CommitNode(state))
    return _SurfaceNode(
        "",
        _SurfaceNode("testing", role="heading"),
        controls,
        role="application",
    )


def _testing_focus_send_tree(
    state: dict[str, object], *, proves_focus: bool = True
) -> _SurfaceNode:
    controls = _SurfaceNode(
        "", _DraftNode(state), _FocusCommitNode(state, proves_focus=proves_focus)
    )
    return _SurfaceNode(
        "",
        _SurfaceNode("testing", role="heading"),
        controls,
        role="application",
    )


def _testing_opaque_send_tree(state: dict[str, object]) -> _SurfaceNode:
    controls = _SurfaceNode("", _OpaqueDraftNode(state), _CommitNode(state))
    return _SurfaceNode(
        "",
        _SurfaceNode("testing", role="heading"),
        controls,
        role="application",
    )


def test_testing_send_canary_commits_once_and_remains_unattributed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, object] = {"draft": "", "invoked": False}

    def matches(
        _apps: tuple[object, ...], tokens: dict[str, str], *, max_nodes: int
    ) -> dict[str, list[dict[str, object]]]:
        assert max_nodes == 100
        assert str(tokens["send"]).startswith("LB26_SEND_")
        return {
            "send": (
                [
                    {
                        "role": "list item",
                        "parent_role": "list",
                        "item_window_extents": (10, 20, 30, 40),
                    }
                ]
                if state["invoked"]
                else []
            )
        }

    monkeypatch.setattr(
        "lemonbot.connectors.linux_atspi_probe._canary_matches",
        matches,
    )

    report = _testing_send_canary((_testing_send_tree(state),), max_nodes=100, timeout_seconds=5)

    assert state == {"draft": "", "invoked": True}
    assert report["actions_performed"] == 1
    assert report["readback_match_count"] == 1
    assert report["outcome"] == "readback_unattributed"
    assert report["direction_proven"] is False
    assert report["acknowledged"] is False
    assert report["retry_allowed"] is False


def test_testing_send_canary_focuses_then_commits_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, object] = {"draft": "", "invoked": False, "focused": False}

    def keyboard_commit() -> bool:
        assert state["focused"] is True
        assert str(state["draft"]).startswith("LB26_SEND_")
        state["draft"] = ""
        state["invoked"] = True
        return True

    monkeypatch.setattr(
        "lemonbot.connectors.linux_atspi_probe._canary_matches",
        lambda *_args, **_kwargs: {
            "send": [
                {
                    "role": "list item",
                    "parent_role": "list",
                    "item_window_extents": (10, 20, 30, 40),
                }
            ]
        },
    )

    report = _testing_send_canary(
        (_testing_focus_send_tree(state),),
        max_nodes=100,
        timeout_seconds=5,
        keyboard_commit=keyboard_commit,
    )

    assert report["commit_mechanism"] == "focus_only"
    assert report["focused_before_commit"] is True
    assert report["keyboard_event_invoked"] is True
    assert report["actions_performed"] == 2
    assert state["invoked"] is True


def test_testing_focus_only_proves_delayed_commit_precondition() -> None:
    state: dict[str, object] = {"draft": "", "invoked": False, "focused": False}

    report = _testing_focus_only(
        (_testing_focus_send_tree(state),), max_nodes=100, timeout_seconds=0.05
    )

    assert report["focus_action_returned"] is True
    assert report["focused_before"] is False
    assert report["focused_after"] is True
    assert report["title_still_proven"] is True
    assert report["passed"] is True
    assert report["actions_performed"] == 1
    assert state["draft"] == ""


def test_testing_input_focus_only_proves_input_focus() -> None:
    state: dict[str, object] = {
        "draft": "",
        "invoked": False,
        "input_focused": False,
    }

    report = _testing_input_focus_only(
        (_testing_send_tree(state),), max_nodes=100, timeout_seconds=0.05
    )

    assert report["focus_action_returned"] is True
    assert report["focused_before"] is False
    assert report["focused_after"] is True
    assert report["title_still_proven"] is True
    assert report["passed"] is True
    assert report["actions_performed"] == 1


def test_testing_send_canary_refuses_keyboard_when_focus_is_not_proven() -> None:
    state: dict[str, object] = {"draft": "", "invoked": False, "focused": False}
    keyboard_calls = 0

    def keyboard_commit() -> bool:
        nonlocal keyboard_calls
        keyboard_calls += 1
        return True

    with pytest.raises(RuntimeError, match="focus was not proven"):
        _testing_send_canary(
            (_testing_focus_send_tree(state, proves_focus=False),),
            max_nodes=100,
            timeout_seconds=5,
            keyboard_commit=keyboard_commit,
        )

    assert keyboard_calls == 0
    assert state["draft"] == ""


def test_testing_send_precommit_diagnostic_never_calls_keyboard() -> None:
    state: dict[str, object] = {"draft": "", "invoked": False, "focused": False}
    keyboard_calls = 0

    def keyboard_commit() -> bool:
        nonlocal keyboard_calls
        keyboard_calls += 1
        return True

    report = _testing_send_canary(
        (_testing_focus_send_tree(state),),
        max_nodes=100,
        timeout_seconds=5,
        operator_confirmed_empty=True,
        precommit_only=True,
        keyboard_commit=keyboard_commit,
    )

    assert report["passed"] is True
    assert report["precommit_stage"] == "ready"
    assert report["cleanup_returned"] is True
    assert report["draft_confirmation"] == "exact_atspi"
    assert report["keyboard_events_performed"] == 0
    assert keyboard_calls == 0
    assert state["draft"] == ""


def test_testing_submit_confirmed_draft_commits_once_without_reading_it() -> None:
    state: dict[str, object] = {
        "draft": "inaccessible existing canary",
        "invoked": False,
        "focused": False,
    }
    keyboard_calls = 0

    def keyboard_commit() -> bool:
        nonlocal keyboard_calls
        keyboard_calls += 1
        state["draft"] = ""
        state["invoked"] = True
        return True

    report = _testing_submit_confirmed_draft(
        (_testing_focus_send_tree(state),),
        max_nodes=100,
        expected_canary_sha256="a" * 64,
        keyboard_commit=keyboard_commit,
    )

    assert report["canary_sha256"] == "a" * 64
    assert report["focused_before_commit"] is True
    assert report["keyboard_event_invoked"] is True
    assert report["keyboard_event_returned"] is True
    assert report["retry_allowed"] is False
    assert keyboard_calls == 1
    assert state["invoked"] is True


def test_testing_submit_confirmed_draft_can_focus_input_for_shortcut() -> None:
    state: dict[str, object] = {
        "draft": "inaccessible existing canary",
        "invoked": False,
        "input_focused": False,
    }

    def keyboard_commit() -> bool:
        assert state["input_focused"] is True
        state["draft"] = ""
        state["invoked"] = True
        return True

    report = _testing_submit_confirmed_draft(
        (_testing_focus_send_tree(state),),
        max_nodes=100,
        expected_canary_sha256="a" * 64,
        keyboard_commit=keyboard_commit,
        focus_input=True,
    )

    assert report["focus_target"] == "input"
    assert report["focus_confirmation"] == "component_return_only"
    assert report["focus_action_returned"] is True
    assert report["keyboard_event_returned"] is True
    assert state["invoked"] is True


def test_alt_s_keyboard_event_always_unlocks_modifier() -> None:
    events: list[tuple[int, str | None, int]] = []

    class KeySynthType:
        LOCKMODIFIERS = 5
        SYM = 3
        UNLOCKMODIFIERS = 6

    class ModifierType:
        ALT = 3

    class FakeAtspi:
        @staticmethod
        def generate_keyboard_event(keyval: int, keystring: str | None, kind: int) -> bool:
            events.append((keyval, keystring, kind))
            return True

    FakeAtspi.KeySynthType = KeySynthType  # type: ignore[attr-defined]
    FakeAtspi.ModifierType = ModifierType  # type: ignore[attr-defined]

    assert _alt_s_keyboard_event(FakeAtspi) is True
    assert events == [(8, "", 5), (ord("s"), None, 3), (8, "", 6)]


def test_testing_send_canary_refuses_nonempty_draft_before_action() -> None:
    state: dict[str, object] = {"draft": "user draft", "invoked": False}

    with pytest.raises(RuntimeError, match="draft"):
        _testing_send_canary((_testing_send_tree(state),), max_nodes=100, timeout_seconds=5)

    assert state == {"draft": "user draft", "invoked": False}


def test_testing_send_canary_accepts_operator_confirmed_empty_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, object] = {"draft": "empty input placeholder", "invoked": False}
    monkeypatch.setattr(
        "lemonbot.connectors.linux_atspi_probe._canary_matches",
        lambda *_args, **_kwargs: {
            "send": [
                {
                    "role": "list item",
                    "parent_role": "list",
                    "item_window_extents": (10, 20, 30, 40),
                }
            ]
        },
    )

    report = _testing_send_canary(
        (_testing_send_tree(state),),
        max_nodes=100,
        timeout_seconds=5,
        operator_confirmed_empty=True,
    )

    assert report["operator_confirmed_empty"] is True
    assert state == {"draft": "", "invoked": True}


def test_testing_send_canary_accepts_successful_opaque_qt_setter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, object] = {"draft": "", "invoked": False}
    monkeypatch.setattr(
        "lemonbot.connectors.linux_atspi_probe._canary_matches",
        lambda *_args, **_kwargs: {
            "send": [
                {
                    "role": "list item",
                    "parent_role": "list",
                    "item_window_extents": (10, 20, 30, 40),
                }
            ]
        },
    )

    report = _testing_send_canary(
        (_testing_opaque_send_tree(state),),
        max_nodes=100,
        timeout_seconds=5,
        operator_confirmed_empty=True,
    )

    assert report["draft_confirmation"] == "operator_plus_setter"
    assert report["outcome"] == "unknown"
    assert report["readback_match_count"] == 1
    assert state["invoked"] is True


def test_testing_send_existing_canary_uses_only_the_confirmed_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, object] = {
        "draft": "LB26_SEND_0123456789abcdef",
        "invoked": False,
    }

    def matches(
        _apps: tuple[object, ...], tokens: dict[str, str], *, max_nodes: int
    ) -> dict[str, list[dict[str, object]]]:
        assert max_nodes == 100
        assert tokens["send"] == "LB26_SEND_0123456789abcdef"
        return {
            "send": [
                {
                    "role": "list item",
                    "parent_role": "list",
                    "item_window_extents": (10, 20, 30, 40),
                }
            ]
        }

    monkeypatch.setattr(
        "lemonbot.connectors.linux_atspi_probe._canary_matches",
        matches,
    )

    report = _testing_send_canary(
        (_testing_send_tree(state),),
        max_nodes=100,
        timeout_seconds=5,
        use_existing_canary=True,
    )

    assert state == {"draft": "", "invoked": True}
    assert report["input_was_empty"] is False
    assert report["used_existing_canary"] is True
    assert report["outcome"] == "readback_unattributed"


def test_testing_send_existing_canary_rejects_arbitrary_draft() -> None:
    state: dict[str, object] = {"draft": "ordinary message", "invoked": False}

    with pytest.raises(RuntimeError, match="not a generated canary"):
        _testing_send_canary(
            (_testing_send_tree(state),),
            max_nodes=100,
            timeout_seconds=5,
            use_existing_canary=True,
        )

    assert state == {"draft": "ordinary message", "invoked": False}


@pytest.mark.parametrize(
    ("draft", "expected"),
    [
        ("", "empty"),
        ("\u2029", "empty"),
        ("LB26_SEND_0123456789abcdef", "generated_canary"),
        ("LB26_SEND_0123456789abcdef\u2029", "generated_canary"),
        ("\u2029LB26_SEND_0123456789abcdef\u2029\u2029", "generated_canary"),
        ("ordinary message", "unclassified"),
        ("ordinary message\u2029", "unclassified"),
    ],
)
def test_testing_draft_state_never_returns_draft_text(draft: str, expected: str) -> None:
    state: dict[str, object] = {"draft": draft, "invoked": False}

    report = _testing_draft_state((_testing_send_tree(state),), max_nodes=100)

    assert report["draft_state"] == expected
    assert report["actions_performed"] == 0
    assert report["transcript_canary_match_count"] == 0
    if draft:
        assert draft not in json.dumps(report)


def test_testing_draft_state_hashes_generated_transcript_canary() -> None:
    state: dict[str, object] = {"draft": "", "invoked": False}
    transcript = _SurfaceNode(
        "",
        _SurfaceNode("LB26_SEND_0123456789abcdef", role="list item"),
        role="list",
    )
    controls = _SurfaceNode("", _DraftNode(state), _CommitNode(state))
    app_node = _SurfaceNode(
        "",
        _SurfaceNode("testing", role="heading"),
        transcript,
        controls,
        role="application",
    )

    report = _testing_draft_state((app_node,), max_nodes=100)

    encoded = json.dumps(report)
    assert report["transcript_canary_match_count"] == 1
    assert "LB26_SEND_0123456789abcdef" not in encoded
    assert len(report["transcript_canaries"][0]["canary_sha256"]) == 64  # type: ignore[index]


def test_testing_send_cli_filters_child_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "_wechat_pids", lambda _pid: (1234,))
    monkeypatch.setattr(cli, "_probe_command", lambda _pids, _max_nodes: (["probe"], {}))
    child_report = {
        "schema_version": 1,
        "surface_sha256": "a" * 64,
        "canary_sha256": "b" * 64,
        "input_was_empty": True,
        "operator_confirmed_empty": True,
        "used_existing_canary": False,
        "commit_mechanism": "activate",
        "send_action_invoked": True,
        "send_action_returned": True,
        "focus_action_invoked": False,
        "focus_action_returned": False,
        "focused_before_commit": False,
        "keyboard_event_invoked": False,
        "keyboard_event_returned": False,
        "readback_match_count": 1,
        "readback_item_window_extents": [10, 20, 30, 40],
        "draft_empty_after": True,
        "direction_proven": False,
        "acknowledged": False,
        "outcome": "readback_unattributed",
        "actions_performed": 1,
        "retry_allowed": False,
        "untrusted": "secret body",
    }
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(child_report).encode()
        ),
    )

    result = CliRunner().invoke(
        app,
        [
            "channel",
            "linux-atspi-testing-send-canary",
            "--confirm-testing-send",
            "--confirm-empty-draft",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "secret body" not in result.output
    assert '"actions_performed": 1' in result.output
    assert '"acknowledged": false' in result.output


def test_canary_match_uses_the_message_row_not_its_list_container() -> None:
    tokens = {"self": "LB26_SELF_" + "test", "inbound": "LB26_PEER_" + "test"}
    self_row = _Node(tokens["self"], role="list item")
    inbound_row = _Node(tokens["inbound"], role="list item")
    app = _Node("", _Node("", self_row, inbound_row, role="list"), role="application")

    matches = _canary_matches(
        (app,),
        tokens,
        max_nodes=100,
    )

    assert matches["self"][0]["parent_role"] == "list"
    assert matches["self"][0]["body_relative_path"] == ()
    assert matches["self"][0]["item_path"] != matches["inbound"][0]["item_path"]
    assert matches["self"][0]["item_window_extents"] == (100, 200, 800, 60)
    assert matches["self"][0]["preceding_sibling_window_extents"] is None
    assert matches["inbound"][0]["preceding_sibling_window_extents"] == (
        100,
        200,
        800,
        60,
    )


def _semantic_report(kind: str) -> dict[str, object]:
    group = kind == "group"
    return {
        "schema_version": 1,
        "kind": kind,
        "passed": True,
        "account_fingerprint": "a" * 64,
        "enrollment_candidate": {
            "chat_kind": kind,
            "header_selector": [0, 1],
            "header_fingerprint": ("b" if group else "c") * 64,
            "transcript_selector": [0, 2],
            "self_item_signature": "d" * 64,
            "inbound_item_signature": "e" * 64,
            "self_body_relative_path": [0],
            "inbound_body_relative_path": [0],
            "sender_relative_path": [1] if group else None,
            "sender_attribute_key": "accessible-id" if group else None,
            "sender_probe_fingerprint": "1" * 64 if group else None,
            "semantic_shape_sha256": "f" * 64,
        },
    }


def test_enroll_is_blocked_while_atspi_direction_is_unproven(
    tmp_path: Path,
) -> None:
    reports: list[Path] = []
    for kind in ("private", "private", "group", "group"):
        path = (tmp_path / f"{kind}-{len(reports)}.json").resolve()
        path.write_text(json.dumps(_semantic_report(kind)), encoding="utf-8")
        path.chmod(0o600)
        reports.append(path)
    output = (tmp_path / "enrollment.json").resolve()
    result = CliRunner().invoke(
        app,
        [
            "channel",
            "linux-atspi-enroll",
            "--private-report",
            str(reports[0]),
            "--private-report",
            str(reports[1]),
            "--group-report",
            str(reports[2]),
            "--group-report",
            str(reports[3]),
            "--output",
            str(output),
            "--confirm-restart",
            "--confirm-lock-cycle",
        ],
    )
    assert result.exit_code == 1
    assert "方向" in result.output
    assert not output.exists()


def test_enroll_gate_precedes_report_parsing(tmp_path: Path) -> None:
    private_a = _semantic_report("private")
    private_b = _semantic_report("private")
    private_b["enrollment_candidate"]["transcript_selector"] = [9]  # type: ignore[index]
    values = (private_a, private_b, _semantic_report("group"), _semantic_report("group"))
    paths = []
    for index, value in enumerate(values):
        path = (tmp_path / f"report-{index}.json").resolve()
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o600)
        paths.append(path)
    output = (tmp_path / "must-not-exist.json").resolve()
    result = CliRunner().invoke(
        app,
        [
            "channel",
            "linux-atspi-enroll",
            "--private-report",
            str(paths[0]),
            "--private-report",
            str(paths[1]),
            "--group-report",
            str(paths[2]),
            "--group-report",
            str(paths[3]),
            "--output",
            str(output),
            "--confirm-restart",
            "--confirm-lock-cycle",
        ],
    )
    assert result.exit_code == 1
    assert not output.exists()


def test_semantic_probe_exposes_only_allowlisted_child_error_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "_wechat_pids", lambda _pid: (1234,))
    monkeypatch.setattr(cli, "_probe_command", lambda _pids, _max_nodes: (["probe"], {}))
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=b'{"error":"RuntimeError","untrusted":"do not show this"}',
        ),
    )

    result = CliRunner().invoke(app, ["channel", "linux-atspi-semantic-probe", "--kind", "private"])

    assert result.exit_code == 1
    assert "安全代码：RuntimeError" in result.output
    assert "do not show this" not in result.output
