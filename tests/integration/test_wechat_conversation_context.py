from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from lemonbot.connectors.wechat_agent import reference
from lemonbot.domain import InboundEvent
from lemonbot.orchestration import EventPipeline, FakeConnector, FakeModelBackend, PipelineStatus
from lemonbot.policy import DeterministicPolicy, PolicyConfig
from lemonbot.storage import CoreRepository, Database


async def test_scoped_context_survives_restart_and_excludes_unknown_answers(tmp_path: Path) -> None:
    path = tmp_path / "conversation.db"
    db = Database.from_path(path)
    await db.initialise()
    repo = CoreRepository(db)
    for chat in ("group", "private"):
        await repo.set_allowlisted("fake", chat)
    model = FakeModelBackend(["delivered group answer", "UNKNOWN answer", "private answer"])
    pipe = EventPipeline(
        repo, DeterministicPolicy(repo, config=PolicyConfig(quiet_enabled=False)), model
    )
    connector = FakeConnector()

    def event(identifier: str, chat: str, sender: str, text: str) -> InboundEvent:
        return InboundEvent(
            channel="fake",
            event_id=identifier,
            chat_id=chat,
            sender_id=sender,
            text=text,
            metadata={
                "wechat_mode": "conversation",
                "context_scope": reference(b"k" * 32, "context", chat, sender),
            },
        )

    try:
        pipe.event_eligible = lambda _event: False
        await pipe.ingest(event("blocked", "group", "alice", "queued before target pause"))
        assert (await pipe.process_once()).status is PipelineStatus.SKIPPED
        assert model.requests == []
        pipe.event_eligible = None
        for identifier, chat, sender, text, unknown in [
            ("1", "group", "alice", "group secret word", False),
            ("2", "group", "bob", "other member word", True),
            ("3", "private", "alice", "private secret word", False),
        ]:
            await pipe.ingest(event(identifier, chat, sender, text))
            assert (await pipe.process_once()).status is PipelineStatus.COMPLETED
            connector.raise_after_accept = unknown
            status = (await pipe.dispatch_once(connector, channel="fake")).status
            assert status is (PipelineStatus.UNKNOWN if unknown else PipelineStatus.ACKNOWLEDGED)
        assert "group secret word" not in str(model.requests[-1].messages)
    finally:
        await db.close()

    db = Database.from_path(path)
    repo = CoreRepository(db)
    model = FakeModelBackend()
    pipe = EventPipeline(
        repo, DeterministicPolicy(repo, config=PolicyConfig(quiet_enabled=False)), model
    )
    pipe.policy.clock = lambda: datetime.now(UTC) + timedelta(minutes=11)
    try:
        for identifier, chat, sender in [
            ("4", "group", "alice"),
            ("5", "group", "bob"),
            ("6", "private", "alice"),
        ]:
            await pipe.ingest(event(identifier, chat, sender, "what was my word?"))
            assert (await pipe.process_once()).status is PipelineStatus.COMPLETED
        group_a, group_b, private = (str(r.messages) for r in model.requests)
        assert "group secret word" in group_a and "delivered group answer" in group_a
        assert "private secret word" not in group_a and "other member word" not in group_a
        assert "other member word" in group_b and "UNKNOWN answer" not in group_b
        assert "private secret word" in private and "private answer" in private
        assert "group secret word" not in private
        assert all(not r.tools for r in model.requests)
    finally:
        await db.close()
