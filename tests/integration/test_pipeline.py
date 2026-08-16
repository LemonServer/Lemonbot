from __future__ import annotations

import asyncio
from datetime import timedelta

from lemonbot.domain import EventKind, InboundEvent, OutboxState
from lemonbot.models.gateway import ModelTransportError
from lemonbot.orchestration import (
    EventPipeline,
    FakeConnector,
    FakeModelBackend,
    PipelineConfig,
    PipelineStatus,
)
from lemonbot.policy import DeterministicPolicy
from lemonbot.storage import CoreRepository, Database


def run(coroutine):
    return asyncio.run(coroutine)


async def make_pipeline(tmp_path, *, response: str = "你好, 我是 AI 助手。"):
    database = Database.from_path(tmp_path / "test.db")
    await database.initialise()
    repository = CoreRepository(database)
    policy = DeterministicPolicy(repository)
    model = FakeModelBackend([response])
    pipeline = EventPipeline(repository, policy, model)
    connector = FakeConnector(channel="fake")
    return database, repository, model, pipeline, connector


def test_event_to_acknowledged_delivery_is_durable_and_deduplicated(tmp_path) -> None:
    async def scenario() -> None:
        database, repository, model, pipeline, connector = await make_pipeline(tmp_path)
        try:
            await repository.set_allowlisted("fake", "chat-1")
            event = InboundEvent(
                channel="fake",
                event_id="event-1",
                chat_id="chat-1",
                sender_id="user-1",
                text="你好",
            )
            assert (await pipeline.ingest(event)).status is PipelineStatus.QUEUED
            assert (await pipeline.ingest(event)).status is PipelineStatus.SKIPPED

            processed = await pipeline.process_once()
            assert processed.status is PipelineStatus.COMPLETED
            assert len(model.requests) == 1

            delivered = await pipeline.dispatch_once(connector, channel="fake")
            assert delivered.status is PipelineStatus.ACKNOWLEDGED
            assert len(connector.delivered) == 1
            message = connector.delivered[0]
            assert message.reply_to_event_id == "event-1"
            assert await repository.outbox_state(message.message_id) is OutboxState.ACKNOWLEDGED

            assert (await pipeline.process_once()).status is PipelineStatus.IDLE
            assert (
                await pipeline.dispatch_once(connector, channel="fake")
            ).status is PipelineStatus.IDLE
        finally:
            await database.close()

    run(scenario())


def test_fixed_welcome_uses_policy_and_outbox_without_calling_model(tmp_path) -> None:
    async def scenario() -> None:
        database = Database.from_path(tmp_path / "welcome.db")
        await database.initialise()
        repository = CoreRepository(database)
        await repository.set_allowlisted("fake", "chat-1")
        model = FakeModelBackend(["must not be used"])
        pipeline = EventPipeline(
            repository,
            DeterministicPolicy(repository),
            model,
            config=PipelineConfig(welcome_text="你好，我是 Lemonbot AI 助手。"),
        )
        connector = FakeConnector(channel="fake")
        try:
            await pipeline.ingest(
                InboundEvent(
                    channel="fake",
                    event_id="enter-1",
                    chat_id="chat-1",
                    sender_id="user-1",
                    kind=EventKind.ENTER_CHAT,
                )
            )
            assert (await pipeline.process_once("fake")).status is PipelineStatus.COMPLETED
            assert model.requests == []
            assert (
                await pipeline.dispatch_once(connector, channel="fake")
            ).status is PipelineStatus.ACKNOWLEDGED
            assert connector.delivered[0].metadata["welcome"] is True
        finally:
            await database.close()

    run(scenario())


def test_ambiguous_delivery_is_quarantined_and_never_blindly_retried(tmp_path) -> None:
    async def scenario() -> None:
        database, repository, _, pipeline, connector = await make_pipeline(tmp_path)
        try:
            await repository.set_allowlisted("fake", "chat-1")
            await pipeline.ingest(
                InboundEvent(
                    channel="fake",
                    event_id="event-ambiguous",
                    chat_id="chat-1",
                    sender_id="user-1",
                    text="测试未知发送状态",
                )
            )
            assert (await pipeline.process_once()).status is PipelineStatus.COMPLETED
            connector.raise_after_accept = True

            result = await pipeline.dispatch_once(connector, channel="fake")
            assert result.status is PipelineStatus.UNKNOWN
            assert len(connector.delivered) == 1
            message_id = connector.delivered[0].message_id
            assert await repository.outbox_state(message_id) is OutboxState.UNKNOWN

            second = await pipeline.dispatch_once(connector, channel="fake")
            assert second.status is PipelineStatus.IDLE
            assert len(connector.delivered) == 1
        finally:
            await database.close()

    run(scenario())


def test_ambiguous_paid_model_call_is_dead_and_never_blindly_retried(tmp_path) -> None:
    class FailingModel(FakeModelBackend):
        async def generate(self, request):
            self.requests.append(request)
            raise ModelTransportError("provider delivery state is unknown")

    async def scenario() -> None:
        database = Database.from_path(tmp_path / "model-unknown.db")
        await database.initialise()
        repository = CoreRepository(database)
        await repository.set_allowlisted("fake", "chat-1")
        model = FailingModel()
        pipeline = EventPipeline(repository, DeterministicPolicy(repository), model)
        try:
            await pipeline.ingest(
                InboundEvent(
                    channel="fake",
                    event_id="model-unknown",
                    chat_id="chat-1",
                    sender_id="user-1",
                    text="do not retry blindly",
                )
            )
            result = await pipeline.process_once("fake")
            assert result.status is PipelineStatus.DEAD
            assert len(model.requests) == 1
            assert (await pipeline.process_once("fake")).status is PipelineStatus.IDLE
            assert len(model.requests) == 1
        finally:
            await database.close()

    run(scenario())


def test_cross_chat_history_is_not_added_to_model_context(tmp_path) -> None:
    async def focused() -> None:
        database, repository, model, pipeline, _ = await make_pipeline(tmp_path, response="收到")
        try:
            await repository.set_allowlisted("fake", "chat-a")
            await pipeline.ingest(
                InboundEvent(
                    channel="fake",
                    event_id="normal-event",
                    chat_id="chat-a",
                    sender_id="user-a",
                    text="普通问题",
                )
            )
            await repository.record_inbound(
                InboundEvent(
                    channel="fake",
                    event_id="secret-event",
                    chat_id="chat-b",
                    sender_id="user-b",
                    text="SECRET-CANARY",
                )
            )
            await pipeline.process_once(channel="fake")
            rendered = "\n".join(message.content or "" for message in model.requests[0].messages)
            assert "普通问题" in rendered
            assert "SECRET-CANARY" not in rendered
        finally:
            await database.close()

    run(focused())


def test_only_enrolled_admin_sender_can_request_deep_model(tmp_path) -> None:
    async def scenario() -> None:
        database = Database.from_path(tmp_path / "deep.db")
        await database.initialise()
        repository = CoreRepository(database)
        await repository.set_allowlisted("fake", "chat-1")
        model = FakeModelBackend(["ordinary", "admin"])
        pipeline = EventPipeline(
            repository,
            DeterministicPolicy(repository),
            model,
            config=PipelineConfig(deep_sender_ids=frozenset({"admin-stable-id"})),
        )
        try:
            for event_id, sender_id in (
                ("normal-deep", "ordinary-user"),
                ("admin-deep", "admin-stable-id"),
            ):
                await pipeline.ingest(
                    InboundEvent(
                        channel="fake",
                        event_id=event_id,
                        chat_id="chat-1",
                        sender_id=sender_id,
                        text="/deep 请深入分析",
                    )
                )
                assert (await pipeline.process_once("fake")).status is PipelineStatus.COMPLETED
            assert [request.deep for request in model.requests] == [False, True]
        finally:
            await database.close()

    run(scenario())


def test_recovery_marks_dispatching_unknown_but_requeues_safe_states(tmp_path) -> None:
    async def scenario() -> None:
        database, repository, _, pipeline, _ = await make_pipeline(tmp_path)
        try:
            await repository.set_allowlisted("fake", "chat-1")
            await pipeline.ingest(
                InboundEvent(
                    channel="fake",
                    event_id="event-crash",
                    chat_id="chat-1",
                    sender_id="user-1",
                    text="crash test",
                )
            )
            await pipeline.process_once()
            reserved = await repository.reserve_next_outbox("fake")
            assert reserved is not None
            assert await repository.mark_dispatching(reserved.id)

            recovered = await repository.recover_interrupted()
            assert recovered["outbox_unknown"] == 1
            assert await repository.outbox_state(reserved.message.message_id) is OutboxState.UNKNOWN
        finally:
            await database.close()

    run(scenario())


def test_recovery_never_retries_inbox_after_model_io_may_have_started(tmp_path) -> None:
    async def scenario() -> None:
        database, repository, _, _, _ = await make_pipeline(tmp_path)
        try:
            await repository.record_inbound(
                InboundEvent(
                    channel="fake",
                    event_id="event-paid-crash",
                    chat_id="chat-1",
                    sender_id="user-1",
                    text="do not spend twice",
                )
            )
            claimed = await repository.claim_next_inbox("fake")
            assert claimed is not None
            assert await repository.mark_inbox_model_started(claimed.id)

            recovered = await repository.recover_interrupted(
                stale_after=timedelta(0)
            )
            assert recovered["inbox_dead_ambiguous"] == 1
            assert await repository.claim_next_inbox("fake") is None
        finally:
            await database.close()

    run(scenario())
