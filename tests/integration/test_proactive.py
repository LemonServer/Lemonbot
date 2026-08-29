from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from lemonbot.connectors import FakeConnector
from lemonbot.domain import InboundEvent
from lemonbot.orchestration import EventPipeline, FakeModelBackend, PipelineStatus
from lemonbot.policy import DeterministicPolicy, PolicyConfig, RateLimitProfile
from lemonbot.proactive import JobSource, JobStatus, ProactiveJob, ProactiveJobStore
from lemonbot.proactive.runner import ProactiveRunner
from lemonbot.storage import CoreRepository, Database


async def test_causal_proactive_job_queues_and_dispatches_once(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "jobs.db")
    await database.initialise()
    repository = CoreRepository(database)
    store = ProactiveJobStore(tmp_path / "jobs.db")
    await store.initialize()
    await repository.set_allowlisted("fake", "chat-1")
    await repository.record_inbound(
        InboundEvent(
            channel="fake",
            event_id="event-commitment-1",
            chat_id="chat-1",
            sender_id="user-1",
            text="请稍后提醒我",
        )
    )
    enabled_limits = RateLimitProfile(
        reply_per_10_minutes=3,
        reply_per_hour=10,
        reply_per_day=30,
        global_per_day=50,
        proactive_cooldown_hours=12,
        proactive_per_day=2,
        proactive_global_per_day=10,
        proactive_enabled=True,
    )
    policy = DeterministicPolicy(
        repository,
        config=PolicyConfig(fallback=enabled_limits),
        clock=lambda: datetime(2026, 8, 16, 4, 0, tzinfo=UTC),
    )
    model = FakeModelBackend(["我是 Lemonbot AI，提醒你查看约定事项。"])
    runner = ProactiveRunner(store, repository, policy, model)
    job = ProactiveJob(
        source=JobSource.STORED_COMMITMENT,
        channel="fake",
        chat_id="chat-1",
        reason_event_id="event-commitment-1",
        prompt="提醒对方查看约定事项",
        due_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    await store.add(job)
    try:
        assert await runner.run_once()
        listed = await store.list()
        assert listed[0].status is JobStatus.COMPLETED
        connector = FakeConnector(channel="fake")
        pipeline = EventPipeline(repository, policy, model)
        result = await pipeline.dispatch_once(connector, channel="fake")
        assert result.status is PipelineStatus.ACKNOWLEDGED
        sent = connector.delivered_messages[0]
        assert sent.metadata["reason_event_id"] == "event-commitment-1"
        assert sent.reply_to_event_id is None
        assert (
            await pipeline.dispatch_once(connector, channel="fake")
        ).status is PipelineStatus.IDLE
    finally:
        await database.close()


async def test_job_store_recovers_fresh_running_job_on_restart(tmp_path: Path) -> None:
    store = ProactiveJobStore(tmp_path / "jobs.db")
    await store.initialize()
    job = ProactiveJob(
        source=JobSource.ADMIN_SCHEDULE,
        channel="wecom",
        chat_id="chat-1",
        reason_event_id="admin-event-1",
        prompt="test",
        due_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    await store.add(job)
    claimed = await store.claim_due()
    assert claimed is not None and claimed.status is JobStatus.RUNNING

    # Runtime ownership is exclusive, so even a just-claimed job belongs to
    # the dead process and must be recovered immediately on startup.
    recovered = ProactiveJobStore(tmp_path / "jobs.db")
    await recovered.initialize()
    assert (await recovered.claim_due()) is not None


async def test_job_store_never_retries_after_model_io_may_have_started(
    tmp_path: Path,
) -> None:
    path = tmp_path / "jobs.db"
    store = ProactiveJobStore(path)
    await store.initialize()
    job = ProactiveJob(
        source=JobSource.ADMIN_SCHEDULE,
        channel="wecom",
        chat_id="chat-1",
        reason_event_id="admin-event-paid",
        prompt="test",
        due_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    await store.add(job)
    claimed = await store.claim_due()
    assert claimed is not None
    assert await store.mark_model_started(claimed.job_id)

    recovered = ProactiveJobStore(path)
    await recovered.initialize()
    listed = await recovered.list()
    assert listed[0].status is JobStatus.DEAD
    assert listed[0].last_error is not None
    assert "model provider I/O" in listed[0].last_error
    assert await recovered.claim_due() is None


async def test_runner_rejects_a_job_without_a_conversation_bound_reason(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "jobs.db")
    await database.initialise()
    repository = CoreRepository(database)
    store = ProactiveJobStore(tmp_path / "jobs.db")
    await store.initialize()
    await repository.set_allowlisted("wecom", "chat-1")
    runner = ProactiveRunner(
        store,
        repository,
        DeterministicPolicy(repository),
        FakeModelBackend(["must not be called"]),
    )
    job = ProactiveJob(
        source=JobSource.ADMIN_SCHEDULE,
        channel="wecom",
        chat_id="chat-1",
        reason_event_id="fabricated-event",
        prompt="test",
        due_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    await store.add(job)
    try:
        assert await runner.run_once()
        listed = await store.list()
        assert listed[0].status is JobStatus.DEAD
    finally:
        await database.close()
