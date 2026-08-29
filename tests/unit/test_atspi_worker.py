from __future__ import annotations

import hashlib
import inspect
import io

import pytest
from pydantic import ValidationError

from lemonbot.connectors.atspi_protocol import AtspiTargetSpec
from lemonbot.connectors.atspi_worker import (
    ERROR,
    HEALTH,
    INIT,
    READY,
    SHUTDOWN,
    SNAPSHOT,
    AtspiWorkerService,
    _structure_fingerprint,
)
from lemonbot.connectors.linux_atspi_probe import semantic_probe


class _Text:
    def __init__(self, value: str) -> None:
        self._value = value

    def get_text(self, _start: int, _end: int) -> str:
        return self._value


class _Node:
    def __init__(
        self,
        role: str,
        text: str = "",
        *children: _Node,
        attributes: dict[str, str] | None = None,
    ) -> None:
        self.role = role
        self.text = text
        self.children = children
        self.attributes = attributes or {}

    def get_child_count(self) -> int:
        return len(self.children)

    def get_child_at_index(self, index: int) -> _Node:
        return self.children[index]

    def get_role_name(self) -> str:
        return self.role

    def get_interfaces(self) -> list[str]:
        return ["Atspi.Text"] if self.text else []

    def get_text_iface(self) -> _Text | None:
        return _Text(self.text) if self.text else None

    def get_name(self) -> str:
        return self.text

    def get_attributes(self) -> dict[str, str]:
        return self.attributes


def _target(
    *,
    header: _Node,
    self_item: _Node,
    inbound_item: _Node,
    group: bool = False,
) -> AtspiTargetSpec:
    return AtspiTargetSpec(
        target_ref="group_test" if group else "private_test",
        chat_kind="group" if group else "private",
        header_selector=(0,),
        header_fingerprint=hashlib.sha256(header.text.encode()).hexdigest(),
        transcript_selector=(1,),
        self_item_signature=_structure_fingerprint(self_item),
        inbound_item_signature=_structure_fingerprint(inbound_item),
        self_body_relative_path=(0,),
        inbound_body_relative_path=(0,),
        sender_relative_path=(1,) if group else None,
        sender_attribute_key="accessible-id" if group else None,
    )


def test_worker_reads_only_enrolled_item_shapes_and_classifies_direction() -> None:
    header = _Node("heading", "private title")
    self_item = _Node("self-row", "", _Node("text", "mine"))
    inbound_item = _Node("peer-row", "", _Node("text", "hello"))
    popup = _Node("dialog", "", _Node("text", "untrusted popup"))
    app = _Node("application", "", header, _Node("list", "", self_item, popup, inbound_item))
    service = AtspiWorkerService(io.BytesIO(), io.BytesIO())

    snapshot = service._target_snapshot((app,), _target(
        header=header, self_item=self_item, inbound_item=inbound_item
    ))

    assert snapshot is not None
    assert [(item.direction, item.text) for item in snapshot.items] == [
        ("self", "mine"),
        ("inbound", "hello"),
    ]
    assert "untrusted popup" not in {item.text for item in snapshot.items}


def test_group_sender_is_salted_and_missing_identity_remains_unproven() -> None:
    header = _Node("heading", "test group")
    self_item = _Node("self-row", "", _Node("text", "mine"))
    sender = _Node("image", "", attributes={"accessible-id": "stable-peer"})
    inbound_item = _Node("peer-row", "", _Node("text", "hello"), sender)
    app = _Node("application", "", header, _Node("list", "", inbound_item))
    service = AtspiWorkerService(io.BytesIO(), io.BytesIO())
    service._account_fingerprint = "a" * 64

    snapshot = service._target_snapshot((app,), _target(
        header=header, self_item=self_item, inbound_item=inbound_item, group=True
    ))

    assert snapshot is not None
    assert snapshot.items[0].sender_ref == hashlib.sha256(
        f"{'a' * 64}\0stable-peer".encode()
    ).hexdigest()
    assert "stable-peer" not in snapshot.model_dump_json()


def test_atspi_protocol_rejects_extra_fields_and_display_sender_identity() -> None:
    with pytest.raises(ValidationError):
        AtspiTargetSpec.model_validate(
            {
                "target_ref": "group_test",
                "chat_kind": "group",
                "header_selector": [0],
                "header_fingerprint": "a" * 64,
                "transcript_selector": [1],
                "self_item_signature": "b" * 64,
                "inbound_item_signature": "c" * 64,
                "self_body_relative_path": [0],
                "inbound_body_relative_path": [0],
                "sender_relative_path": [1],
                "sender_attribute_key": "name",
                "action": "click",
            }
        )


def test_worker_protocol_defines_no_action_message_type() -> None:
    message_types = {INIT, READY, SNAPSHOT, HEALTH, ERROR, SHUTDOWN}
    assert not any(word in value for value in message_types for word in ("click", "send", "input"))


def test_atspi_paths_never_start_an_unsafe_background_event_loop() -> None:
    assert "event_main" not in inspect.getsource(AtspiWorkerService.run)
    assert "event_main" not in inspect.getsource(semantic_probe)
