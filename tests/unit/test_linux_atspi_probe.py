from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lemonbot import cli
from lemonbot.cli import app
from lemonbot.connectors import AtspiEnrollment
from lemonbot.connectors.linux_atspi_probe import _canary_matches, _inspect_app, probe


class _Action:
    def get_n_actions(self) -> int:
        return 1


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


def test_enroll_requires_two_consistent_reports_and_writes_no_visible_text(
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
    assert result.exit_code == 0, result.output
    bundle = AtspiEnrollment.model_validate_json(output.read_bytes())
    refs = {target.target_ref for target in bundle.targets}
    assert len(refs) == 2
    assert any(ref.startswith("private_") for ref in refs)
    assert any(ref.startswith("group_") for ref in refs)
    encoded = output.read_text(encoding="utf-8")
    assert "contact" not in encoded
    assert "message" not in encoded


def test_enroll_rejects_structural_drift_without_creating_bundle(tmp_path: Path) -> None:
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
