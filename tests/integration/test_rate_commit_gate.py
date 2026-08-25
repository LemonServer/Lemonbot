from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lemonbot.domain import InboundEvent, ModelRequest, ModelResponse
from lemonbot.orchestration import EventPipeline, FakeModelBackend, PipelineStatus
from lemonbot.policy import DeterministicPolicy, PolicyConfig, RateLimitProfile
from lemonbot.storage import CoreRepository, Database


class _BarrierModel(FakeModelBackend):
    def __init__(self) -> None:
        super().__init__()
        self._barrier = asyncio.Barrier(2)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        await self._barrier.wait()
        return await super().generate(request)


async def test_shared_commit_gate_prevents_concurrent_global_quota_overshoot(
    tmp_path: Path,
) -> None:
    database = Database.from_path(tmp_path / "quota.db")
    await database.initialise()
    repository = CoreRepository(database)
    for chat_id in ("chat-a", "chat-b"):
        await repository.set_allowlisted("fake", chat_id)
        await repository.record_inbound(
            InboundEvent(
                channel="fake",
                event_id=f"event-{chat_id}",
                chat_id=chat_id,
                sender_id=f"sender-{chat_id}",
                text="hello",
            )
        )
    one = RateLimitProfile(
        reply_per_10_minutes=1,
        reply_per_hour=1,
        reply_per_day=1,
        global_per_day=1,
        proactive_cooldown_hours=1,
        proactive_per_day=1,
        proactive_global_per_day=1,
    )
    policy = DeterministicPolicy(
        repository,
        PolicyConfig(fallback=one),
        clock=lambda: datetime.now(UTC),
    )
    shared_gate = asyncio.Lock()
    model = _BarrierModel()
    first = EventPipeline(repository, policy, model, side_effect_lock=shared_gate)
    second = EventPipeline(repository, policy, model, side_effect_lock=shared_gate)

    try:
        results = await asyncio.wait_for(
            asyncio.gather(first.process_once("fake"), second.process_once("fake")),
            timeout=10,
        )
        assert sorted(result.status for result in results) == [
            PipelineStatus.COMPLETED,
            PipelineStatus.SKIPPED,
        ]
        assert (
            await repository.count_outbound_since(
                datetime.now(UTC) - timedelta(days=1),
                channel="fake",
            )
            == 1
        )
    finally:
        await database.close()
