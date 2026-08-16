from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from lemonbot.domain import InboundEvent, ModelCapabilities
from lemonbot.memory import (
    ContextBuilder,
    MemoryContextService,
    MemoryDerivationService,
    MemoryKind,
    MemoryRecord,
    Provenance,
    SQLiteMemoryStore,
)
from lemonbot.orchestration import EventPipeline, FakeModelBackend, PipelineConfig, PipelineStatus
from lemonbot.policy import DeterministicPolicy
from lemonbot.storage import CoreRepository, Database


class TightModel(FakeModelBackend):
    def __init__(self, responses: Sequence[str], *, context_tokens: int) -> None:
        super().__init__(responses)
        self._context_tokens = context_tokens

    def count_tokens(self, messages: Sequence[object]) -> int:
        return sum(len(getattr(message, "content", "") or "") + 4 for message in messages)

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(tools=True, json_output=True, context_tokens=self._context_tokens)


async def _repository(path: Path) -> tuple[Database, CoreRepository]:
    database = Database.from_path(path)
    await database.initialise()
    return database, CoreRepository(database)


async def test_pipeline_retrieves_only_current_scope_and_enforces_context_cap(
    tmp_path: Path,
) -> None:
    database, repository = await _repository(tmp_path / "scoped.db")
    memory = SQLiteMemoryStore(tmp_path / "scoped.db")
    model = TightModel(["收到"], context_tokens=700)
    try:
        await memory.add(
            MemoryRecord(
                channel="fake",
                chat_id="chat-a",
                kind=MemoryKind.FACT,
                text="用户喜欢无糖柠檬茶。",
                provenance=Provenance(
                    source_message_ids=("source-a",),
                    model="deepseek-v4-flash",
                    prompt_version="test/v1",
                    confidence=0.9,
                ),
            )
        )
        await memory.add(
            MemoryRecord(
                channel="fake",
                chat_id="chat-a",
                kind=MemoryKind.COMMITMENT,
                text="承诺：周五提醒用户提交项目报告。",
                provenance=Provenance(
                    source_message_ids=("promise-a",),
                    model="deepseek-v4-flash",
                    prompt_version="test/v1",
                    confidence=0.9,
                ),
            )
        )
        await memory.add(
            MemoryRecord(
                channel="fake",
                chat_id="chat-b",
                kind=MemoryKind.COMMITMENT,
                text="柠檬茶 SECRET-CROSS-CHAT-CANARY",
                provenance=Provenance(
                    source_message_ids=("source-b",),
                    model="deepseek-v4-flash",
                    prompt_version="test/v1",
                    confidence=0.9,
                ),
            )
        )
        await repository.set_allowlisted("fake", "chat-a")
        for index in range(5):
            await repository.record_inbound(
                InboundEvent(
                    channel="fake",
                    event_id=f"old-{index}",
                    chat_id="chat-a",
                    sender_id="user-a",
                    text="旧对话" + "很长" * 70,
                    metadata={"attachment_ids": ["OLD-ATTACHMENT-CANARY"]},
                )
            )
            claimed = await repository.claim_next_inbox("fake")
            assert claimed is not None
            await repository.complete_inbox(claimed.id)

        pipeline = EventPipeline(
            repository,
            DeterministicPolicy(repository),
            model,
            memory_context=MemoryContextService(memory, ContextBuilder(model)),
            config=PipelineConfig(
                system_prompt="Lemonbot system",
                model_max_tokens=40,
                max_context_tokens=700,
            ),
        )
        await pipeline.ingest(
            InboundEvent(
                channel="fake",
                event_id="current-event",
                chat_id="chat-a",
                sender_id="user-a",
                text="我最近又想喝柠檬茶了",
                metadata={"attachment_ids": ["CURRENT-ATTACHMENT"]},
            )
        )
        result = await pipeline.process_once("fake")
        assert result.status is PipelineStatus.COMPLETED
        request = model.requests[0]
        rendered = "\n".join(message.content or "" for message in request.messages)
        assert "用户喜欢无糖柠檬茶" in rendered
        assert "周五提醒用户提交项目报告" in rendered
        assert "CURRENT-ATTACHMENT" in rendered
        assert "SECRET-CROSS-CHAT-CANARY" not in rendered
        assert "OLD-ATTACHMENT-CANARY" not in rendered
        assert model.count_tokens(request.messages) + request.max_tokens <= 700
    finally:
        await database.close()


async def test_pipeline_derives_source_bound_memory_with_one_remaining_flash_call(
    tmp_path: Path,
) -> None:
    database, repository = await _repository(tmp_path / "derive.db")
    memory = SQLiteMemoryStore(tmp_path / "derive.db")
    event_id = "memory-event"
    assistant_id = uuid5(NAMESPACE_URL, f"lemonbot:fake:{event_id}")
    derived_json = (
        '{"memories":[{"kind":"preference","text":"用户偏好无糖柠檬茶",'
        f'"source_message_ids":["{event_id}","{assistant_id}"],'
        '"confidence":0.93,"importance":0.8}],"summary":null}'
    )
    model = FakeModelBackend(["好的，我会记住。", derived_json])
    try:
        await repository.set_allowlisted("fake", "chat-a")
        pipeline = EventPipeline(
            repository,
            DeterministicPolicy(repository),
            model,
            memory_context=MemoryContextService(memory, ContextBuilder(model)),
            memory_derivation=MemoryDerivationService(store=memory, backend=model),
            config=PipelineConfig(system_prompt="system", max_model_turns=8),
        )
        await pipeline.ingest(
            InboundEvent(
                channel="fake",
                event_id=event_id,
                chat_id="chat-a",
                sender_id="user-a",
                text="我喜欢无糖柠檬茶",
            )
        )
        assert (await pipeline.process_once("fake")).status is PipelineStatus.COMPLETED

        hits = await memory.search(
            channel="fake",
            chat_id="chat-a",
            query="无糖柠檬茶",
            kinds=(MemoryKind.PREFERENCE,),
        )
        assert len(hits) == 1
        record = hits[0].record
        assert record.provenance.source_message_ids == (event_id, str(assistant_id))
        assert record.provenance.source_event_ids == (event_id,)
        assert record.provenance.model == "fake"
        assert record.provenance.prompt_version == "memory-derive/v1"
        assert record.provenance.confidence == 0.93
        assert len(model.requests) == 2
        assert model.requests[1].deep is False
        assert model.requests[1].response_format == "json"
    finally:
        await database.close()


async def test_memory_derivation_never_exceeds_per_event_model_turn_limit(
    tmp_path: Path,
) -> None:
    database, repository = await _repository(tmp_path / "turn-limit.db")
    memory = SQLiteMemoryStore(tmp_path / "turn-limit.db")
    model = FakeModelBackend(["only reply"])
    try:
        await repository.set_allowlisted("fake", "chat-a")
        pipeline = EventPipeline(
            repository,
            DeterministicPolicy(repository),
            model,
            memory_context=MemoryContextService(memory, ContextBuilder(model)),
            memory_derivation=MemoryDerivationService(store=memory, backend=model),
            config=PipelineConfig(
                system_prompt="system",
                max_model_turns=1,
                memory_summary_turn_threshold=2,
            ),
        )
        await pipeline.ingest(
            InboundEvent(
                channel="fake",
                event_id="one-call-event",
                chat_id="chat-a",
                sender_id="user-a",
                text="hello",
            )
        )
        assert (await pipeline.process_once("fake")).status is PipelineStatus.COMPLETED
        assert len(model.requests) == 1
        assert await memory.search(channel="fake", chat_id="chat-a", query="") == ()
    finally:
        await database.close()
