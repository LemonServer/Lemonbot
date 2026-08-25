from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from lemonbot.admin.control import StatusView
from lemonbot.approvals import ApprovalRepository, ApprovalService
from lemonbot.domain import (
    ApprovalState,
    InboundEvent,
    ModelResponse,
    ToolCall,
    ToolContext,
    ToolManifest,
    ToolResult,
)
from lemonbot.orchestration import EventPipeline, FakeModelBackend, PipelineConfig
from lemonbot.policy import DeterministicPolicy
from lemonbot.runtime import RepositoryControl
from lemonbot.storage import CoreRepository, Database
from lemonbot.tools.vault import FileVault, VaultCreateTool, VaultRoot


class _AmbiguousTool:
    def __init__(self) -> None:
        self.calls = 0

    def manifest(self) -> ToolManifest:
        return ToolManifest(
            name="test.ambiguous_write",
            description="Test-only ambiguous side effect.",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            action_kind="write_file",
            side_effect=True,
            risk_level="high",
            idempotent=False,
        )

    async def invoke(self, context: ToolContext, arguments: dict[str, object]) -> ToolResult:
        del context, arguments
        self.calls += 1
        await asyncio.sleep(0)
        raise ConnectionError("simulated loss after commit boundary")


async def test_approved_file_create_is_bound_revalidated_and_executed_once(tmp_path) -> None:
    database = Database.from_path(tmp_path / "approval-runtime.db")
    await database.initialise()
    repository = CoreRepository(database)
    approvals = ApprovalService(ApprovalRepository(database), profile="lab")
    policy = DeterministicPolicy(repository)
    target_root = tmp_path / "approved-output"
    create_tool = VaultCreateTool(FileVault([VaultRoot("out", target_root, writable=True)]))
    tools = {create_tool.manifest().name: create_tool}
    side_effect_lock = asyncio.Lock()
    model = FakeModelBackend(
        [
            ModelResponse(
                model="fake",
                tool_calls=(
                    ToolCall(
                        call_id="create-1",
                        name="vault.create_text",
                        arguments={
                            "root": "out",
                            "path": "note.txt",
                            "content": "approved content",
                        },
                    ),
                ),
            ),
            "我已提交本机管理员审批；文件尚未创建。",
        ]
    )
    pipeline = EventPipeline(
        repository,
        policy,
        model,
        tools=tools,
        approval_service=approvals,
        side_effect_lock=side_effect_lock,
        config=PipelineConfig(profile="lab"),
    )
    control = RepositoryControl(
        repository,
        profile="lab",
        connector_name="fake",
        started_at=datetime.now(UTC),
        emergency_event=asyncio.Event(),
        approvals=approvals,
        tools=tools,
        policy=policy,
        granted_tool_scopes=frozenset(),
        side_effect_lock=side_effect_lock,
    )
    try:
        await repository.set_allowlisted("fake", "chat-1")
        await pipeline.ingest(
            InboundEvent(
                channel="fake",
                event_id="event-approval",
                chat_id="chat-1",
                sender_id="user-1",
                text="请把这段话保存到新文件",
            )
        )
        await pipeline.process_once("fake")

        pending = await approvals.pending()
        assert len(pending) == 1
        assert not (target_root / "note.txt").exists()
        views = await control.approvals()
        assert len(views) == 1
        assert "approved content" not in views[0].summary
        assert "note.txt" not in views[0].summary

        assert await control.decide_approval(str(pending[0].approval_id), "approve_once")
        assert (target_root / "note.txt").read_text(encoding="utf-8") == "approved content"
        status = await approvals.status(pending[0].approval_id)
        assert status is not None and status.state is ApprovalState.APPROVED
        assert not await control.decide_approval(str(pending[0].approval_id), "approve_once")
        assert not (target_root / "note.v1.txt").exists()
        status_view = await control.status()
        assert isinstance(status_view, StatusView)
        assert status_view.pending_approvals == 0
    finally:
        await database.close()


async def test_approval_execution_exception_becomes_unknown_and_is_never_retried(
    tmp_path,
) -> None:
    database = Database.from_path(tmp_path / "approval-unknown.db")
    await database.initialise()
    repository = CoreRepository(database)
    approvals = ApprovalService(ApprovalRepository(database), profile="lab")
    policy = DeterministicPolicy(repository)
    tool = _AmbiguousTool()
    tools = {tool.manifest().name: tool}
    control = RepositoryControl(
        repository,
        profile="lab",
        connector_name="fake",
        started_at=datetime.now(UTC),
        emergency_event=asyncio.Event(),
        approvals=approvals,
        tools=tools,
        policy=policy,
        granted_tool_scopes=frozenset(),
        side_effect_lock=asyncio.Lock(),
    )
    try:
        await repository.set_allowlisted("fake", "chat-1")
        await repository.record_inbound(
            InboundEvent(
                channel="fake",
                event_id="event-unknown",
                chat_id="chat-1",
                sender_id="user-1",
                text="test",
            )
        )
        approval = await approvals.request(
            channel="fake",
            chat_id="chat-1",
            event_id="event-unknown",
            tool_name=tool.manifest().name,
            action_kind="write_file",
            arguments={},
        )

        assert await control.decide_approval(str(approval.approval_id), "approve_once")
        status = await approvals.status(approval.approval_id)
        assert status is not None and status.state is ApprovalState.UNKNOWN
        assert tool.calls == 1
        assert not await control.decide_approval(str(approval.approval_id), "approve_once")
        assert tool.calls == 1
    finally:
        await database.close()
