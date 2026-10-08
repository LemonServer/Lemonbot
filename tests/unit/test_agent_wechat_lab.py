from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from lemonbot.domain.models import DeliveryStatus, OutboxState
from lemonbot.research.agent_wechat import (
    CHANNEL,
    AgentWechatConnector,
    AgentWechatHTTP,
    LabPaused,
    enroll,
    reply_for,
    title_candidates,
)
from lemonbot.research.agent_wechat_lab import (
    LabRunner,
    SimulatedWechat,
    demo,
    load_binding,
    prepare_state,
    private_read,
    save_binding,
)
from lemonbot.storage import CoreRepository, Database

KEY = b"k" * 32
COMMAND_A = "LB26_E2E_" + "A" * 24
COMMAND_B = "LB26_E2E_" + "B" * 24


async def test_send_diagnostics_count_once_and_hide_message_and_identifiers() -> None:
    mock = SimulatedWechat()
    http = mock.http()
    binding = await enroll(http, KEY)
    diagnostics: list[dict[str, Any]] = []
    connector = AgentWechatConnector(
        http, binding, KEY, run_id="AGW_TEST", diagnostic=diagnostics.append,
        readback_seconds=0,
    )
    try:
        event = connector.event(mock.append(COMMAND_A))
        assert event is not None
        message = reply_for(event)
        receipt = await connector.deliver(message)
        assert receipt.status is DeliveryStatus.ACKNOWLEDGED
        await connector.deliver(message)
        assert mock.posts == http.post_count == 1
        assert [r["stage"] for r in diagnostics] == [
            "before_send", "post_returned", "readback",
        ]
        assert diagnostics[-1]["match_count"] == 1
        assert diagnostics[-1]["records"][0]["is_self"] is True
        serialized = json.dumps(diagnostics)
        assert mock.chat not in serialized and mock.account not in serialized
        assert message.text not in serialized and "synthetic-token" not in serialized
    finally:
        await connector.close()


async def test_timeout_diagnostic_preserves_single_unknown_attempt() -> None:
    mock = SimulatedWechat()
    mock.send_timeout = True
    http = mock.http()
    diagnostics: list[dict[str, Any]] = []
    connector = AgentWechatConnector(
        http, await enroll(http, KEY), KEY, diagnostic=diagnostics.append,
    )
    try:
        event = connector.event(mock.append(COMMAND_A))
        assert event is not None
        message = reply_for(event)
        assert (await connector.deliver(message)).status is DeliveryStatus.UNKNOWN
        assert (await connector.deliver(message)).status is DeliveryStatus.UNKNOWN
        assert diagnostics[-1]["stage"] == "unconfirmed"
        assert diagnostics[-1]["post_count"] == mock.posts == 1
        assert diagnostics[-1]["send_seconds"] >= 0
    finally:
        await connector.close()


async def setup_lab(
    mock: SimulatedWechat, path: Path | None = None,
) -> tuple[AgentWechatConnector, Database, LabRunner]:
    http = mock.http()
    binding = await enroll(http, KEY)
    connector = AgentWechatConnector(http, binding, KEY, readback_seconds=0)
    database = Database.from_path(path) if path is not None else Database.in_memory()
    await database.initialise()
    runner = LabRunner(connector, CoreRepository(database))
    await runner.initialize()
    return connector, database, runner


async def test_demo_covers_persistence_restart_and_unknown(tmp_path: Path) -> None:
    result = await demo(tmp_path)
    assert result["ok"]
    assert result["mode"] == "simulated_http_not_live"
    assert result["post_count"] == 2
    assert result["unknown_not_retried"]
    assert result["model_calls"] == 0


async def test_baseline_self_and_unrelated_text_never_create_replies() -> None:
    mock = SimulatedWechat()
    mock.append(COMMAND_A)
    connector, database, runner = await setup_lab(mock)
    try:
        mock.append(COMMAND_B, is_self=True)
        mock.append("unrelated private text")
        assert (await runner.tick())["new_inbound"] == 0
        assert mock.posts == 0
    finally:
        await connector.close()
        await database.close()


async def test_peer_consecutive_duplicate_rows_and_self_echo() -> None:
    mock = SimulatedWechat()
    mock.append("old baseline")
    connector, database, runner = await setup_lab(mock)
    try:
        a = mock.append(COMMAND_A)
        b = mock.append(COMMAND_B)
        result = await runner.tick()
        assert result["new_inbound"] == 2
        assert result["deliveries"] == ["acknowledged", "acknowledged"]
        event_a, event_b = connector.event(a), connector.event(b)
        assert event_a is not None and event_b is not None
        assert event_a.sender_id == event_b.sender_id
        assert event_a.event_id != event_b.event_id
        assert not await runner.repository.record_inbound(event_a)
        assert (await runner.tick())["new_inbound"] == 0
        assert mock.posts == 2
    finally:
        await connector.close()
        await database.close()


@pytest.mark.parametrize("change,code", [
    ("account", "AccountChanged"),
    ("chat", "TestingBindingChanged"),
    ("title", "TestingNotForeground"),
])
async def test_guard_drift_pauses_before_sending(change: str, code: str) -> None:
    mock = SimulatedWechat()
    connector, database, runner = await setup_lab(mock)
    try:
        row = mock.append(COMMAND_A)
        event = connector.event(row)
        assert event is not None
        await runner.repository.record_inbound(event)
        await runner.process_pending()
        setattr(mock, change, "changed@chatroom" if change == "chat" else "changed")
        with pytest.raises(LabPaused, match=code):
            await runner.dispatch_pending()
        assert mock.posts == 0
        assert not (await connector.health()).healthy
    finally:
        await connector.close()
        await database.close()


@pytest.mark.parametrize("field,value,code", [
    ("sender", None, "SenderMissing"),
    ("isSelf", None, "DirectionMissing"),
    ("isSelf", 0, "DirectionMissing"),
    ("timestamp", "not-a-date", "TimestampMissing"),
    ("timestamp", "2026-10-01T00:00:00", "TimestampMissing"),
    ("localId", True, "MessageIdentityMissing"),
    ("chatId", "wrong@chatroom", "WrongChatMessage"),
])
async def test_bad_identity_fields_pause_without_persisting(
    field: str, value: Any, code: str,
) -> None:
    mock = SimulatedWechat()
    connector, database, runner = await setup_lab(mock)
    try:
        mock.append(COMMAND_A)[field] = value
        with pytest.raises(LabPaused, match=code):
            await runner.tick()
        assert mock.posts == 0
        assert await runner.repository.claim_next_inbox(CHANNEL) is None
    finally:
        await connector.close()
        await database.close()


async def test_page_boundary_polls_until_baseline_anchor() -> None:
    mock = SimulatedWechat()
    mock.append("baseline")
    connector, database, _runner = await setup_lab(mock)
    try:
        for i in range(105):
            mock.append("LB26_E2E_" + f"{i:024X}")
        events = await connector.poll()
        assert len(events) == 105
        assert events[0].text == "LB26_E2E_" + f"{0:024X}"
        assert events[-1].text == "LB26_E2E_" + f"{104:024X}"
        assert not await connector.poll()
    finally:
        await connector.close()
        await database.close()


async def test_anchor_lost_pauses_instead_of_replaying() -> None:
    mock = SimulatedWechat()
    mock.append("baseline")
    connector, database, runner = await setup_lab(mock)
    try:
        mock.rows.clear()
        mock.append(COMMAND_A)
        with pytest.raises(LabPaused, match="AnchorLost"):
            await runner.tick()
        assert mock.posts == 0
    finally:
        await connector.close()
        await database.close()


async def test_read_timeout_with_actual_message_is_unknown_and_not_retried(tmp_path: Path) -> None:
    mock = SimulatedWechat()
    connector, database, runner = await setup_lab(mock, tmp_path / "lab.db")
    row = mock.append(COMMAND_A)
    event = connector.event(row)
    assert event is not None
    message = reply_for(event)
    try:
        mock.send_timeout = True
        with pytest.raises(LabPaused):
            await runner.tick()
        assert mock.posts == 1
        assert await runner.repository.outbox_state(message.message_id) is OutboxState.UNKNOWN
        assert (await connector.deliver(message)).status is DeliveryStatus.UNKNOWN
        assert mock.posts == 1
    finally:
        await connector.close()
        await database.close()
    connector, database, runner = await setup_lab(mock, tmp_path / "lab.db")
    try:
        await runner.tick()
        assert mock.posts == 1
        assert await runner.repository.outbox_state(message.message_id) is OutboxState.UNKNOWN
    finally:
        await connector.close()
        await database.close()


@pytest.mark.parametrize("stage", ["recorded", "created", "reserved", "dispatching", "sent"])
async def test_crash_recovery_preserves_safe_work_and_quarantines_uncertain_send(
    stage: str, tmp_path: Path,
) -> None:
    mock = SimulatedWechat()
    connector, database, runner = await setup_lab(mock, tmp_path / "lab.db")
    event = connector.event(mock.append(COMMAND_A))
    assert event is not None
    message = reply_for(event)
    try:
        await runner.repository.record_inbound(event)
        if stage != "recorded":
            claimed = await runner.repository.claim_next_inbox(CHANNEL)
            assert claimed is not None
            await runner.repository.create_outbox(message)
            # Crash before complete_inbox must also be recoverable.
        if stage in {"reserved", "dispatching", "sent"}:
            item = await runner.repository.reserve_next_outbox(CHANNEL)
            assert item is not None
            if stage in {"dispatching", "sent"}:
                await runner.repository.mark_dispatching(item.id)
                if stage == "sent":
                    receipt = await connector.deliver(item.message)
                    assert receipt.status is DeliveryStatus.ACKNOWLEDGED
                    # Intentionally omit apply_receipt: crash after external side effect.
    finally:
        await connector.close()
        await database.close()
    connector, database, runner = await setup_lab(mock, tmp_path / "lab.db")
    try:
        await runner.tick()
        expected = (
            OutboxState.UNKNOWN if stage in {"dispatching", "sent"} else OutboxState.ACKNOWLEDGED
        )
        assert await runner.repository.outbox_state(message.message_id) is expected
        assert mock.posts == (0 if stage == "dispatching" else 1)
        await runner.tick()
        assert mock.posts == (0 if stage == "dispatching" else 1)
    finally:
        await connector.close()
        await database.close()


async def test_success_without_readback_is_unknown() -> None:
    mock = SimulatedWechat()
    connector, database, _runner = await setup_lab(mock)
    event = connector.event(mock.append(COMMAND_A))
    assert event is not None
    original = mock.handle

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            mock.posts += 1
            return httpx.Response(200, json={"success": True})
        return original(request)

    connector.http.client._transport = httpx.MockTransport(handle)
    try:
        receipt = await connector.deliver(reply_for(event))
        assert receipt.status is DeliveryStatus.UNKNOWN
        assert connector.paused == "ReadbackTimeoutNoRetry"
        assert mock.posts == 1
    finally:
        await connector.close()
        await database.close()


async def test_ambiguous_multiple_readbacks_are_not_confirmed() -> None:
    mock = SimulatedWechat()
    original = mock.handle

    def handle(request: httpx.Request) -> httpx.Response:
        result = original(request)
        if request.method == "POST":
            mock.append(json.loads(request.content)["text"], is_self=True)
        return result

    http = AgentWechatHTTP("synthetic-token", transport=httpx.MockTransport(handle))
    binding = await enroll(http, KEY)
    connector = AgentWechatConnector(http, binding, KEY, readback_seconds=0)
    try:
        event = connector.event(mock.append(COMMAND_A))
        assert event is not None
        assert (await connector.deliver(reply_for(event))).status is DeliveryStatus.UNKNOWN
        assert connector.paused == "AmbiguousReadback"
    finally:
        await connector.close()


@pytest.mark.parametrize("popup_role", ["dialog", "popup menu", "popup-menu"])
async def test_title_message_and_chat_list_matches_are_excluded(popup_role: str) -> None:
    label = {
        "role": "label", "name": "testing",
        "bounds": {"x": 300, "y": 20, "width": 50, "height": 20},
    }
    tree = {"tree": {"role": "frame",
                     "bounds": {"x": 0, "y": 0, "width": 1024, "height": 800},
                     "children": [
        {"role": "list", "name": "Chats", "bounds": {"x": 0, "y": 0, "width": 272, "height": 800},
         "children": [label]},
        {"role": "list item", "children": [label]},
    ]}}
    assert not title_candidates(tree)
    tree["tree"]["children"].append(label)
    assert len(title_candidates(tree)) == 1
    tree["tree"]["children"].append({"role": popup_role})
    with pytest.raises(LabPaused, match="PopupPresent"):
        title_candidates(tree)


async def test_duplicate_testing_name_rejected() -> None:
    mock = SimulatedWechat()
    original = mock.handle

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/chats":
            return httpx.Response(200, json=[
                {"name": "testing", "id": "one@chatroom"},
                {"name": "testing", "id": "two@chatroom"},
            ])
        return original(request)

    http = AgentWechatHTTP("synthetic-token", transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(LabPaused, match="TestingNotUnique"):
            await enroll(http, KEY)
    finally:
        await http.close()


async def test_raw_http_errors_are_sanitized() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="private-account private-body secret-token")

    http = AgentWechatHTTP("synthetic-token", transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(LabPaused) as caught:
            await http.request("GET", "/api/status/auth")
        assert str(caught.value) == "APIUnavailableOrInvalid:Read:HTTP500"
    finally:
        await http.close()


async def test_synchronous_send_has_a_bounded_longer_wait_and_no_retry() -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.extensions["timeout"]))
        if request.method == "POST":
            raise httpx.ReadTimeout("private-token private-chat", request=request)
        return httpx.Response(200, json={})

    http = AgentWechatHTTP("synthetic-token", transport=httpx.MockTransport(handle))
    try:
        await http.request("GET", "/api/status/auth")
        with pytest.raises(LabPaused, match=r"^APIUnavailableOrInvalid:Send:Timeout$"):
            await http.request("POST", "/api/messages/send", {"text": "synthetic"})
        assert len(calls) == 2
        assert calls[0][1]["read"] == 10
        assert calls[1][1]["read"] == 25
        assert calls[1][1]["connect"] == 10
    finally:
        await http.close()


async def test_invalid_json_diagnostic_does_not_expose_read_path_or_body() -> None:
    http = AgentWechatHTTP(
        "synthetic-token",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="private-account private-body")
        ),
    )
    try:
        with pytest.raises(LabPaused) as caught:
            await http.request("GET", "/api/messages/private-account?private-parameter")
        assert str(caught.value) == "APIUnavailableOrInvalid:Read:JSON"
    finally:
        await http.close()


def test_private_binding_files_and_symlink_rejection(tmp_path: Path) -> None:
    state = tmp_path / "state"
    assert len(prepare_state(state)) == 32
    assert state.stat().st_mode & 0o777 == 0o700
    assert (state / "comparison-key").stat().st_mode & 0o777 == 0o600
    linked = state / "linked"
    linked.symlink_to(state / "comparison-key")
    with pytest.raises(OSError):
        private_read(linked)


async def test_binding_cannot_be_reassigned_to_another_account(tmp_path: Path) -> None:
    mock = SimulatedWechat()
    http = mock.http()
    try:
        path = tmp_path / "binding.json"
        original = await enroll(http, KEY)
        save_binding(path, original)
        assert load_binding(path) == original
        mock.account = "another-account"
        changed = await enroll(http, KEY)
        with pytest.raises(LabPaused, match="ExistingBindingChanged"):
            save_binding(path, changed)
    finally:
        await http.close()


async def test_changed_head_during_paging_pauses_without_persisting() -> None:
    mock = SimulatedWechat()
    mock.append("baseline")
    original = mock.handle
    reads = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal reads
        if request.url.path.startswith("/api/messages/") and request.method == "GET":
            reads += 1
            if reads == 3:
                mock.append(COMMAND_B)
        return original(request)

    http = AgentWechatHTTP("synthetic-token", transport=httpx.MockTransport(handle))
    binding = await enroll(http, KEY)
    connector = AgentWechatConnector(http, binding, KEY)
    try:
        await connector.baseline()
        mock.append(COMMAND_A)
        with pytest.raises(LabPaused, match="MessageWindowChanged"):
            await connector.poll()
        assert mock.posts == 0
    finally:
        await connector.close()


async def test_http_redirect_is_not_followed_to_another_host() -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://example.com/private"})

    http = AgentWechatHTTP("synthetic-token", transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(LabPaused, match="APIUnavailableOrInvalid"):
            await http.request("GET", "/api/status/auth")
        assert calls == ["http://127.0.0.1:6174/api/status/auth"]
    finally:
        await http.close()


async def test_foreground_pause_requires_a_new_explicit_run() -> None:
    mock = SimulatedWechat()
    connector, database, runner = await setup_lab(mock)
    try:
        mock.title = "another-chat"
        with pytest.raises(LabPaused, match="TestingNotForeground"):
            await runner.tick()
        mock.title = "testing"
        with pytest.raises(LabPaused, match="TestingNotForeground"):
            await runner.tick()
        assert mock.posts == 0
    finally:
        await connector.close()
        await database.close()


async def test_title_label_below_header_region_cannot_authorize_send() -> None:
    mock = SimulatedWechat()
    original = mock.handle

    def handle(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.url.path == "/api/debug/a11y":
            tree = response.json()
            tree["tree"]["children"][1]["bounds"]["y"] = 300
            return httpx.Response(200, json=tree)
        return response

    http = AgentWechatHTTP("synthetic-token", transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(LabPaused, match="TestingHeaderNotUnique"):
            await enroll(http, KEY)
    finally:
        await http.close()


async def test_process_cancel_after_post_quarantines_outbox(tmp_path: Path) -> None:
    import asyncio

    mock = SimulatedWechat()
    original = mock.handle

    def handle(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.method == "POST":
            raise asyncio.CancelledError
        return response

    http = AgentWechatHTTP("synthetic-token", transport=httpx.MockTransport(handle))
    binding = await enroll(http, KEY)
    connector = AgentWechatConnector(http, binding, KEY)
    database = Database.from_path(tmp_path / "lab.db")
    await database.initialise()
    runner = LabRunner(connector, CoreRepository(database))
    try:
        await runner.initialize()
        event = connector.event(mock.append(COMMAND_A))
        assert event is not None
        with pytest.raises(asyncio.CancelledError):
            await runner.tick()
        assert mock.posts == 1
        state = await runner.repository.outbox_state(reply_for(event).message_id)
        assert state is OutboxState.UNKNOWN
    finally:
        await connector.close()
        await database.close()


async def test_api_rejection_after_post_is_conservatively_unknown() -> None:
    mock = SimulatedWechat()
    original = mock.handle

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            mock.posts += 1
            return httpx.Response(200, json={"success": False, "error": "private upstream error"})
        return original(request)

    http = AgentWechatHTTP("synthetic-token", transport=httpx.MockTransport(handle))
    binding = await enroll(http, KEY)
    connector = AgentWechatConnector(http, binding, KEY, readback_seconds=0)
    try:
        event = connector.event(mock.append(COMMAND_A))
        assert event is not None
        message = reply_for(event)
        assert (await connector.deliver(message)).status is DeliveryStatus.UNKNOWN
        assert (await connector.deliver(message)).status is DeliveryStatus.UNKNOWN
        assert mock.posts == 1
    finally:
        await connector.close()


async def test_peer_copy_of_ack_does_not_confirm_delivery() -> None:
    mock = SimulatedWechat()
    original = mock.handle

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            mock.posts += 1
            mock.append(json.loads(request.content)["text"], is_self=False)
            return httpx.Response(200, json={"success": True})
        return original(request)

    http = AgentWechatHTTP("synthetic-token", transport=httpx.MockTransport(handle))
    binding = await enroll(http, KEY)
    connector = AgentWechatConnector(http, binding, KEY, readback_seconds=0)
    try:
        event = connector.event(mock.append(COMMAND_A))
        assert event is not None
        assert (await connector.deliver(reply_for(event))).status is DeliveryStatus.UNKNOWN
        assert connector.paused == "InvalidSelfReadback"
    finally:
        await connector.close()


async def test_title_geometry_tracks_its_wechat_frame() -> None:
    frame = {"role": "frame", "bounds": {"x": 156, "y": 68, "width": 880, "height": 640},
             "children": [
                 {"role": "list", "name": "Chats",
                  "bounds": {"x": 217, "y": 149, "width": 240, "height": 558}},
                 {"role": "label", "name": "testing",
                  "bounds": {"x": 473, "y": 116, "width": 48, "height": 18}},
             ]}
    tree = {"tree": frame}
    assert len(title_candidates(tree)) == 1
    for node in [frame, *frame["children"]]:
        node["bounds"]["x"] += 40
        node["bounds"]["y"] += 60
    assert len(title_candidates(tree)) == 1
    frame["children"][1]["bounds"]["y"] += 200
    assert not title_candidates(tree)


async def test_title_in_another_frame_cannot_authorize_testing() -> None:
    mock = SimulatedWechat()
    frame = mock.handle(httpx.Request("GET", "http://localhost/api/debug/a11y")).json()["tree"]
    label = frame["children"].pop()
    other = {"role": "frame", "bounds": frame["bounds"], "children": [label]}
    tree = {"tree": {"role": "desktop-frame", "children": [frame, other]}}
    assert not title_candidates(tree)


@pytest.mark.parametrize("bounds", [None, {"x": 0, "y": float("nan"), "width": 1024,
                                          "height": 800}])
async def test_invalid_frame_geometry_pauses(bounds: Any) -> None:
    mock = SimulatedWechat()
    tree = mock.handle(httpx.Request("GET", "http://localhost/api/debug/a11y")).json()
    tree["tree"]["bounds"] = bounds
    with pytest.raises(LabPaused, match="FrameStructureMissing"):
        title_candidates(tree)


@pytest.mark.parametrize("wrapper_title", ["testing", "testing (3)"])
async def test_nested_duplicate_heading_collapses_to_leaf_without_accepting_siblings(
    wrapper_title: str,
) -> None:
    mock = SimulatedWechat()
    tree = mock.handle(httpx.Request("GET", "http://localhost/api/debug/a11y")).json()
    leaf = tree["tree"]["children"][1]
    wrapper = {"role": "label", "name": wrapper_title, "bounds": dict(leaf["bounds"]),
               "children": [leaf]}
    tree["tree"]["children"][1] = wrapper
    candidates = title_candidates(tree)
    assert [path for path, _ in candidates] == [(1, 0)]
    wrapper["children"].append(dict(leaf))
    assert len(title_candidates(tree)) == 2


async def test_uppercase_hidden_heading_cannot_authorize_send() -> None:
    mock = SimulatedWechat()
    tree = mock.handle(httpx.Request("GET", "http://localhost/api/debug/a11y")).json()
    tree["tree"]["children"][1]["states"] = ["HIDDEN"]
    assert not title_candidates(tree)
