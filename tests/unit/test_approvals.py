from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from lemonbot.approvals import ApprovalRepository, ApprovalService
from lemonbot.domain import ApprovalState
from lemonbot.storage import Database
from lemonbot.storage.models import ApprovalRow


async def _service(
    tmp_path: Path,
    now: list[datetime],
) -> tuple[Database, ApprovalService]:
    database = Database.from_path(tmp_path / "approvals.db")
    await database.initialise()
    service = ApprovalService(
        ApprovalRepository(database),
        profile="lab",
        clock=lambda: now[0],
    )
    return database, service


async def test_pending_list_is_bound_but_never_returns_full_arguments(tmp_path: Path) -> None:
    now = [datetime(2026, 8, 16, 4, 0, tzinfo=UTC)]
    database, service = await _service(tmp_path, now)
    secret = "full-parameter-" + "secret-must-not-be-listed"
    try:
        created = await service.request(
            channel="wechat_personal_lab",
            chat_id="stable-chat-1",
            event_id="event-1",
            tool_name="vault.create",
            action_kind="write_file",
            arguments={
                "api_key": secret,
                "path": "notes/new.txt",
                "content": "private conversation content",
            },
        )

        listed = await service.pending()

        assert listed == [created]
        rendered = listed[0].model_dump_json()
        assert secret not in rendered
        assert "private conversation content" not in rendered
        assert "notes/new.txt" not in rendered
        assert "arguments" not in type(listed[0]).model_fields
        assert listed[0].profile == "lab"
        assert listed[0].channel == "wechat_personal_lab"
        assert listed[0].chat_id == "stable-chat-1"
        assert listed[0].event_id == "event-1"
        assert listed[0].tool_name == "vault.create"
        assert listed[0].action_kind == "write_file"
        assert "api_key=<sensitive>" in listed[0].arguments_summary

        async with database.sessions() as session:
            stored = await session.scalar(
                select(ApprovalRow).where(ApprovalRow.approval_id == str(created.approval_id))
            )
            assert stored is not None
            assert stored.arguments_json["api_key"] == secret
    finally:
        await database.close()


async def test_approve_once_is_an_atomic_claim_and_terminal_result_is_immutable(
    tmp_path: Path,
) -> None:
    now = [datetime(2026, 8, 16, 5, 0, tzinfo=UTC)]
    database, service = await _service(tmp_path, now)
    try:
        created = await service.request(
            channel="wecom",
            chat_id="chat-1",
            event_id="event-atomic",
            tool_name="mcp.pinned_writer",
            action_kind="mcp_write",
            arguments={"record_id": 7, "value": "exact"},
        )

        claims = await asyncio.gather(
            service.approve_once(created.approval_id),
            service.approve_once(created.approval_id),
        )
        claimed = [claim for claim in claims if claim is not None]

        assert len(claimed) == 1
        claim = claimed[0]
        assert claim.state is ApprovalState.EXECUTING
        assert claim.arguments == {"record_id": 7, "value": "exact"}
        assert await service.approve_once(created.approval_id) is None

        forged = claim.model_copy(update={"claim_token": uuid4()})
        assert not await service.resolve(
            forged,
            outcome=ApprovalState.APPROVED,
            outcome_code="committed",
        )
        assert await service.resolve(
            claim,
            outcome=ApprovalState.APPROVED,
            outcome_code="committed",
        )
        assert not await service.resolve(
            claim,
            outcome=ApprovalState.UNKNOWN,
            outcome_code="late_override",
        )
        status = await service.status(created.approval_id)
        assert status is not None
        assert status.state is ApprovalState.APPROVED
        assert status.outcome_code == "committed"
    finally:
        await database.close()


async def test_expiry_and_administrator_denial_are_terminal(tmp_path: Path) -> None:
    now = [datetime(2026, 8, 16, 6, 0, tzinfo=UTC)]
    database, service = await _service(tmp_path, now)
    try:
        expiring = await service.request(
            channel="wecom",
            chat_id="chat-1",
            event_id="event-expiring",
            tool_name="vault.create",
            action_kind="write_file",
            arguments={"name": "one.txt"},
            expires_at=now[0] + timedelta(seconds=30),
        )
        denied = await service.request(
            channel="wecom",
            chat_id="chat-1",
            event_id="event-denied",
            tool_name="vault.create",
            action_kind="write_file",
            arguments={"name": "two.txt"},
        )
        assert await service.deny(denied.approval_id)
        assert not await service.deny(denied.approval_id)

        now[0] += timedelta(minutes=1)
        assert await service.pending() == []
        assert await service.approve_once(expiring.approval_id) is None
        expired_status = await service.status(expiring.approval_id)
        denied_status = await service.status(denied.approval_id)
        assert expired_status is not None
        assert expired_status.state is ApprovalState.DENIED
        assert expired_status.outcome_code == "expired"
        assert denied_status is not None
        assert denied_status.state is ApprovalState.DENIED
        assert denied_status.outcome_code == "administrator_denied"
    finally:
        await database.close()


async def test_interrupted_execution_becomes_unknown_and_is_never_retried(
    tmp_path: Path,
) -> None:
    now = [datetime(2026, 8, 16, 7, 0, tzinfo=UTC)]
    database, service = await _service(tmp_path, now)
    arguments = {"target": "external-resource", "value": 3}
    try:
        created = await service.request(
            channel="wecom",
            chat_id="chat-1",
            event_id="event-crash",
            tool_name="mcp.pinned_writer",
            action_kind="mcp_write",
            arguments=arguments,
        )
        assert await service.approve_once(created.approval_id) is not None

        assert await service.recover_interrupted() == 1
        assert await service.recover_interrupted() == 0
        status = await service.status(created.approval_id)
        assert status is not None
        assert status.state is ApprovalState.UNKNOWN
        assert status.outcome_code == "interrupted_execution"
        assert await service.approve_once(created.approval_id) is None

        duplicate = await service.request(
            channel="wecom",
            chat_id="chat-1",
            event_id="event-crash",
            tool_name="mcp.pinned_writer",
            action_kind="mcp_write",
            arguments=arguments,
        )
        assert duplicate.approval_id == created.approval_id
        assert duplicate.state is ApprovalState.UNKNOWN
        assert await service.pending() == []
    finally:
        await database.close()
