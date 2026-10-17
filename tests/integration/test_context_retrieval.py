from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest

from lemonbot.domain.models import DeliveryReceipt, DeliveryStatus, OutboundMessage, OutboxState
from lemonbot.embeddings.artifacts import EmbeddingError
from lemonbot.embeddings.protocol import Ready
from lemonbot.embeddings.text import TextChunk
from lemonbot.memory.unified import retrieval_cli
from lemonbot.memory.unified.archive import SQLiteMessageArchive
from lemonbot.memory.unified.documents import IndexUnit, terms
from lemonbot.memory.unified.index import RetrievalError, SQLiteRetrievalIndex
from lemonbot.memory.unified.models import (
    ConversationScope,
    EmbeddingRequest,
    EmbeddingResult,
    Message,
    MessagePart,
    Observation,
    SourceRef,
)
from lemonbot.memory.unified.retrieval import HybridContextRetriever, RetrievalOptions
from lemonbot.storage import CoreRepository, Database
from lemonbot.storage.migrate import upgrade_database

NOW = datetime(2026, 10, 15, tzinfo=UTC)
SCOPE = ConversationScope(
    channel="synthetic", account_id="account-a", conversation_id="group-a", kind="group"
)


def message(identifier: str, text: str = "hello", **changes: Any) -> Message:
    return Message.model_validate(
        {
            "scope": SCOPE,
            "message_id": identifier,
            "event_id": identifier,
            "direction": "inbound",
            "sender_id": "alice",
            "occurred_at": NOW,
            "parts": (MessagePart(part_id="text", kind="text", text=text),),
            **changes,
        }
    )


def source(identifier: str, **changes: Any) -> SourceRef:
    return SourceRef(scope=SCOPE, message_id=identifier, **changes)


class SyntheticEmbedder:
    def __init__(self, version: str = "synthetic-v1", fail: str = "") -> None:
        self.ready = Ready(model_version=version, backend="tensorrt", precision="fp16")
        self.requests: list[EmbeddingRequest] = []
        self.fail = fail
        self.closed = False

    async def chunks(
        self, text: str, purpose: Literal["query", "document"]
    ) -> tuple[TextChunk, ...]:
        return tuple(
            TextChunk(start=i, end=min(i + 100, len(text)), text=text[i : i + 100], tokens=102)
            for i in range(0, len(text), 100)
        )

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        self.requests.append(request)
        if self.fail == "exit":
            raise EmbeddingError("embedding_worker_unavailable")
        vectors = []
        for text in request.texts:
            dimension = (
                0
                if any(word in text for word in ("车", "动力"))
                else 1
                if any(word in text for word in ("杯", "红色", "图片"))
                else 2
                if "Python" in text
                else 3
            )
            vectors.append(tuple(1.0 if i == dimension else 0.0 for i in range(512)))
        return EmbeddingResult(
            request_id="wrong" if self.fail == "identity" else request.request_id,
            model_version=self.ready.model_version,
            backend="tensorrt",
            precision="fp16",
            vectors=tuple(vectors),
        )

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def components(tmp_path: Path) -> tuple[SQLiteMessageArchive, SQLiteRetrievalIndex]:
    database = tmp_path / "archive.sqlite3"
    upgrade_database(database)
    archive = SQLiteMessageArchive(database)
    return archive, SQLiteRetrievalIndex(tmp_path / "index.sqlite3", database)


def retriever(
    components: tuple[SQLiteMessageArchive, SQLiteRetrievalIndex], **kwargs: Any
) -> HybridContextRetriever:
    return HybridContextRetriever(*components, clock=lambda: NOW + timedelta(hours=1), **kwargs)


def fingerprint(path: Path) -> str:
    with sqlite3.connect(path) as db:
        return hashlib.sha256("\n".join(db.iterdump()).encode()).hexdigest()


async def test_chinese_fts_and_keyword_query_are_scoped_and_do_not_modify_archive(
    components,
) -> None:
    archive, index = components
    await archive.put_message(message("old", "这次活动的暗号是银杏。"))
    before = fingerprint(archive.path)
    result = await retriever(components).retrieve(current=message("current", "活动暗号是什么？"))
    assert result.candidates[0].sources == (source("old", part_id="text"),)
    assert "keyword" in result.candidates[0].reasons
    assert result.candidates[0].occurred_at == NOW
    assert result.candidates[0].author_ids == ("alice",)
    assert result.degraded_reason == "embedding_worker_unavailable"
    assert fingerprint(archive.path) == before
    assert index.path.stat().st_mode & 0o777 == 0o600
    for query in ('" OR * NEAR (', "tokens:private OR partition:wrong", '"银杏"'):
        await index.keywords(SCOPE, query)
    assert "活动" in terms("活动暗号")


async def test_semantic_synonym_recall_does_not_depend_on_keyword_overlap(components) -> None:
    archive, _ = components
    await archive.put_message(message("car", "汽车发动机"))
    await archive.put_message(message("cup", "红色杯子"))
    worker = SyntheticEmbedder()
    result = await retriever(
        components, worker=worker, options=RetrievalOptions(recent_messages=0)
    ).retrieve(
        current=message("current", "动力来源"),
    )
    assert [candidate.sources[0].message_id for candidate in result.candidates] == ["car"]
    assert result.candidates[0].reasons == ("semantic",)
    assert result.degraded_reason is None
    assert worker.requests and all(len(request.texts) <= 16 for request in worker.requests)


@pytest.mark.parametrize(
    "foreign",
    [
        {"account_id": "account-b"},
        {"conversation_id": "group-b"},
        {"conversation_id": "private-b", "kind": "private"},
        {"channel": "other"},
        {"kind": "private"},
    ],
)
async def test_same_ids_content_and_vectors_cannot_cross_scope(components, foreign) -> None:
    archive, index = components
    other = ConversationScope.model_validate({**SCOPE.model_dump(), **foreign})
    await archive.put_message(message("same-id", "汽车发动机", scope=other))
    await retriever(components, worker=SyntheticEmbedder()).refresh(other)
    result = await retriever(components, worker=SyntheticEmbedder()).retrieve(
        current=message("current", "汽车动力"),
    )
    assert result.candidates == ()
    assert not await index.keywords(SCOPE, "汽车")
    assert not await index.vectors(SCOPE, "synthetic-v1")
    assert await index.vectors(other, "synthetic-v1")


async def test_group_members_share_candidates_with_original_authorship(components) -> None:
    archive, _ = components
    await archive.put_message(message("alice-word", "活动暗号银杏", sender_id="alice"))
    result = await retriever(components).retrieve(
        current=message("bob-query", "活动暗号是什么", sender_id="bob"),
    )
    assert result.candidates[0].author_ids == ("alice",)


async def test_missing_quote_never_guesses_closest_image_and_valid_quote_is_first(
    components,
) -> None:
    archive, _ = components

    def image() -> tuple[MessagePart, ...]:
        return (MessagePart(part_id="image", kind="image", unavailable_reason="not_fetched"),)

    await archive.put_message(message("image-a", parts=image()))
    await archive.put_message(
        message("image-b", parts=image(), occurred_at=NOW + timedelta(seconds=1))
    )
    current = message(
        "query",
        "这张图片是什么",
        occurred_at=NOW + timedelta(seconds=2),
        references=(source("image-a", part_id="image"),),
    )
    result = await retriever(components).retrieve(current=current)
    assert [candidate.sources[0].message_id for candidate in result.candidates] == ["image-a"]
    assert result.candidates[0].reasons[0] == "quote"
    missing = source("absent", part_id="image")
    result = await retriever(components).retrieve(
        current=current.model_copy(update={"references": (missing,)})
    )
    assert result.candidates == ()
    assert result.unresolved_references == (missing,)


async def add_image_observation(
    archive: SQLiteMessageArchive, *, expires: datetime | None = None
) -> None:
    await archive.put_message(
        message(
            "image",
            parts=(
                MessagePart(
                    part_id="image",
                    kind="image",
                    unavailable_reason="not_fetched",
                ),
            ),
        )
    )
    await archive.put_observation(
        Observation(
            observation_id="ocr-1",
            source=source("image", part_id="image"),
            kind="image_description",
            status="available",
            text="红色杯子放在桌子上",
            extractor_version="synthetic/v1",
            created_at=NOW,
            expires_at=expires,
        )
    )


async def test_unrelated_topic_and_new_image_do_not_default_to_historical_image(components) -> None:
    archive, _ = components
    await add_image_observation(archive)
    worker = SyntheticEmbedder()
    service = retriever(components, worker=worker)
    unrelated = await service.retrieve(current=message("python-query", "Python 列表如何排序？"))
    assert unrelated.candidates == ()
    new_image = await service.retrieve(
        current=message(
            "new-image",
            "请看图片",
            parts=(
                MessagePart(part_id="text", kind="text", text="请看图片"),
                MessagePart(
                    part_id="image", kind="image", unavailable_reason="current_not_fetched"
                ),
            ),
        )
    )
    assert new_image.candidates == ()
    followup = await service.retrieve(current=message("followup", "图片里的杯子是什么颜色？"))
    assert any(candidate.observation_ids == ("ocr-1",) for candidate in followup.candidates)


async def test_expired_observation_and_its_cached_vector_do_not_reappear(components) -> None:
    archive, index = components
    await add_image_observation(archive, expires=NOW + timedelta(minutes=1))
    service = HybridContextRetriever(archive, index, worker=SyntheticEmbedder(), clock=lambda: NOW)
    first = await service.retrieve(current=message("query", "红色杯子"))
    assert any(candidate.observation_ids for candidate in first.candidates)
    service.clock = lambda: NOW + timedelta(minutes=2)
    second = await service.retrieve(current=message("query-later", "红色杯子"))
    assert second.candidates == ()
    assert not await index.vectors(SCOPE, "synthetic-v1")


async def test_page_body_and_card_title_are_retrieved_without_network(components) -> None:
    archive, _ = components
    await archive.put_message(
        message(
            "card",
            parts=(
                MessagePart(
                    part_id="card",
                    kind="card",
                    url="https://example.org/article",
                    title="合成天文资料",
                ),
            ),
        )
    )
    await archive.put_observation(
        Observation(
            observation_id="body",
            source=source("card", part_id="card"),
            kind="page_body",
            status="available",
            text="木星是太阳系最大的行星",
            extractor_version="synthetic/v1",
            created_at=NOW,
        )
    )
    result = await retriever(components).retrieve(current=message("query", "木星有多大"))
    assert result.candidates[0].observation_ids == ("body",)
    assert "keyword" in result.candidates[0].reasons


@pytest.mark.parametrize("failure", ["exit", "identity"])
async def test_gpu_failure_explicitly_degrades_without_cpu_or_retry(components, failure) -> None:
    archive, _ = components
    await archive.put_message(message("old", "活动暗号银杏"))
    worker = SyntheticEmbedder(fail=failure)
    service = retriever(components, worker=worker)
    result = await service.retrieve(current=message("query", "活动暗号"))
    assert result.candidates and all("semantic" not in c.reasons for c in result.candidates)
    assert result.degraded_reason in {"embedding_worker_failed", "embedding_result_invalid"}
    assert worker.closed and len(worker.requests) == 1
    await service.retrieve(current=message("next", "活动暗号"))
    assert len(worker.requests) == 1


async def test_vector_work_is_bounded_persistent_and_incremental_across_restart(components) -> None:
    archive, index = components
    for i in range(3):
        await archive.put_message(message(str(i), "汽车发动机"))
    options = RetrievalOptions(vector_chunks_per_refresh=1)
    worker = SyntheticEmbedder()
    report = await retriever(components, worker=worker, options=options).refresh(SCOPE)
    assert report.vectorized == 1 and report.pending_vectors == 2
    assert report.degraded_reason == "semantic_index_incomplete"
    reopened = SQLiteRetrievalIndex(index.path, archive.path)
    next_worker = SyntheticEmbedder()
    service = retriever((archive, reopened), worker=next_worker)
    report = await service.refresh(SCOPE)
    assert report.changed == 0 and report.vectorized == 2 and report.pending_vectors == 0
    assert len(next_worker.requests) == 2
    assert (await service.refresh(SCOPE)).vectorized == 0
    assert len(next_worker.requests) == 2
    other_worker = SyntheticEmbedder(version="synthetic-v2")
    assert (await retriever(components, worker=other_worker).refresh(SCOPE)).vectorized == 3


async def test_candidate_count_and_serialized_length_are_bounded(components) -> None:
    archive, _ = components
    for i in range(30):
        await archive.put_message(message(str(i), "活动资料" * 240))
    service = retriever(components, options=RetrievalOptions(candidate_char_budget=2048))
    result = await service.retrieve(current=message("query", "活动资料"))
    assert 0 < len(result.candidates) <= 20
    assert sum(len(candidate.model_dump_json()) for candidate in result.candidates) <= 2048
    with pytest.raises(RetrievalError, match="QueryBounds"):
        await service.retrieve(current=message("query"), limit=21)


async def test_archive_work_limit_fails_before_partial_index_commit(components) -> None:
    archive, index = components
    for i in range(2):
        await archive.put_message(message(str(i)))
    with pytest.raises(RetrievalError, match="ArchiveScopeTooLarge"):
        await retriever(components, options=RetrievalOptions(max_messages=1)).refresh(SCOPE)
    assert not index.path.exists()


async def test_index_refuses_archive_aliases_unrelated_database_and_other_archive(
    components, tmp_path
) -> None:
    archive, index = components
    with pytest.raises(RetrievalError, match="SeparateIndex"):
        SQLiteRetrievalIndex(archive.path, archive.path)
    for name, link in (("symlink", os.symlink), ("hardlink", os.link)):
        path = tmp_path / name
        link(archive.path, path)
        with pytest.raises(RetrievalError, match="SeparateIndex"):
            SQLiteRetrievalIndex(path, archive.path)
        path.unlink()
    await retriever(components).refresh(SCOPE)
    other = tmp_path / "other.sqlite3"
    upgrade_database(other)
    with pytest.raises(RetrievalError, match="ArchiveMismatch"):
        await SQLiteRetrievalIndex(index.path, other).units(SCOPE)
    unrelated = tmp_path / "unrelated.sqlite3"
    with sqlite3.connect(unrelated) as db:
        db.execute("CREATE TABLE precious(data TEXT)")
    with pytest.raises(RetrievalError, match="SchemaConflict"):
        await SQLiteRetrievalIndex(unrelated, archive.path).units(SCOPE)


async def test_rechecks_candidate_against_archive_after_index_read(components, monkeypatch) -> None:
    archive, index = components
    await archive.put_message(message("old", "活动暗号银杏"))
    original = index.units

    async def changed(scope: ConversationScope) -> tuple[IndexUnit, ...]:
        return tuple(
            unit.model_copy(update={"text": "foreign secret"}) for unit in await original(scope)
        )

    monkeypatch.setattr(index, "units", changed)
    result = await retriever(components).retrieve(current=message("query", "活动暗号"))
    assert result.candidates == () and "index_source_changed" in result.degraded_reason


@pytest.mark.parametrize(
    "status", [DeliveryStatus.UNKNOWN, DeliveryStatus.FAILED, DeliveryStatus.ACKNOWLEDGED]
)
async def test_only_confirmed_assistant_history_is_searchable(components, status) -> None:
    archive, _ = components
    await archive.put_message(message("cause"))
    database = Database.from_path(archive.path)
    repository = CoreRepository(database)
    outbound = OutboundMessage(
        channel=SCOPE.channel,
        chat_id=SCOPE.conversation_id,
        reply_to_event_id="cause",
        text="机器人暗号银杏",
    )
    try:
        row = await repository.create_outbox(outbound)
        await repository.reserve_next_outbox(SCOPE.channel)
        assert await repository.mark_dispatching(row.id)
        await repository.apply_receipt(
            row.id,
            DeliveryReceipt(
                message_id=outbound.message_id,
                status=status,
                external_id="exact-receipt",
                acknowledged_at=NOW if status is DeliveryStatus.ACKNOWLEDGED else None,
            ),
        )
    finally:
        await database.close()
    await archive.put_observed_batch(
        [
            (
                message(
                    "assistant",
                    "机器人暗号银杏",
                    event_id=None,
                    sender_id="bot",
                    direction="outbound",
                    delivery_state=OutboxState.ACKNOWLEDGED,
                ),
                "exact-receipt",
            )
        ]
    )
    result = await retriever(components, options=RetrievalOptions(recent_messages=0)).retrieve(
        current=message("query", "机器人暗号"),
    )
    assert bool(result.candidates) == (status is DeliveryStatus.ACKNOWLEDGED)


async def test_local_cli_omits_content_by_default_and_never_changes_archive(
    components, monkeypatch
) -> None:
    archive, index = components
    await archive.put_message(message("old", "活动暗号银杏"))
    await archive.put_message(message("query", "活动暗号是什么"))
    from lemonbot.config.settings import AppSettings

    monkeypatch.setattr(retrieval_cli, "load_settings", lambda path: AppSettings())
    before = fingerprint(archive.path)
    args = argparse.Namespace(
        config=None,
        database=archive.path,
        index=index.path,
        channel=SCOPE.channel,
        account=SCOPE.account_id,
        conversation=SCOPE.conversation_id,
        kind=SCOPE.kind,
        cuda=False,
        command="query",
        current_id="query",
        include_content=False,
        limit=20,
    )
    result = await retrieval_cli.execute(args)
    assert result["result"]["candidates"]
    assert "银杏" not in json.dumps(result, ensure_ascii=False)
    assert fingerprint(archive.path) == before
    args.include_content = True
    assert "银杏" in json.dumps(await retrieval_cli.execute(args), ensure_ascii=False)


async def test_fixed_stage_one_scenarios_return_sources_without_scope_or_image_leaks(
    components,
) -> None:
    archive, _ = components
    fixture = json.loads(
        (Path(__file__).parents[1] / "fixtures/context_memory/scenarios.json").read_text()
    )
    messages = {}
    for position, row in enumerate(fixture["messages"]):
        scope = SCOPE.model_copy(update={"conversation_id": row["chat"], "kind": row["kind"]})
        references = (
            ()
            if "quote" not in row
            else (
                SourceRef(
                    scope=scope,
                    message_id=row["quote"],
                    part_id=row["quote_part"],
                ),
            )
        )
        item = message(
            row["id"],
            scope=scope,
            sender_id=row["sender"],
            occurred_at=NOW + timedelta(seconds=position),
            parts=tuple(MessagePart.model_validate(part) for part in row["parts"]),
            references=references,
        )
        messages[item.message_id] = item
        await archive.put_message(item)
    service = retriever(components)
    for case in fixture["cases"]:
        result = await service.retrieve(current=messages[case["current"]])
        sources = {
            source.message_id for candidate in result.candidates for source in candidate.sources
        }
        assert set(case["expected_sources"]) <= sources, case["name"]
        assert not set(case["excluded_sources"]) & sources, case["name"]


async def test_same_body_different_ids_survive_and_future_messages_are_excluded(components) -> None:
    archive, _ = components
    for identifier in ("same-a", "same-b"):
        await archive.put_message(message(identifier, "活动暗号银杏"))
    await archive.put_message(
        message("future", "活动暗号银杏", occurred_at=NOW + timedelta(seconds=2))
    )
    result = await retriever(components).retrieve(current=message("query", "活动暗号"))
    assert {c.sources[0].message_id for c in result.candidates} == {"same-a", "same-b"}


async def test_foreign_quote_even_if_constructed_without_validation_is_rejected(components) -> None:
    scope = SCOPE.model_copy(update={"account_id": "other"})
    reference = SourceRef(scope=scope, message_id="foreign")
    current = message("query").model_copy(update={"references": (reference,)})
    with pytest.raises(RetrievalError, match="ScopeMismatch"):
        await retriever(components).retrieve(current=current)


async def test_query_gpu_failure_keeps_keywords_and_recent_candidates(components) -> None:
    archive, _ = components
    await archive.put_message(message("old", "活动暗号银杏"))
    worker = SyntheticEmbedder()
    service = retriever(components, worker=worker)
    await service.refresh(SCOPE)
    worker.fail = "exit"
    result = await service.retrieve(current=message("query", "活动暗号是什么"))
    assert result.candidates and "embedding_worker_failed" in result.degraded_reason
    assert worker.closed and service.worker is None
    later = await service.retrieve(current=message("query-again", "活动暗号是什么"))
    assert "embedding_worker_failed" in later.degraded_reason


async def test_exact_quote_outranks_strong_semantic_match_and_length_limits(components) -> None:
    archive, _ = components
    await archive.put_message(message("quoted", "毫不相关但被精确引用的原文"))
    await archive.put_message(message("semantic", "汽车发动机"))
    result = await retriever(components, worker=SyntheticEmbedder()).retrieve(
        current=message("query", "汽车动力", references=(source("quoted", part_id="text"),)),
        limit=1,
    )
    assert result.candidates[0].sources[0].message_id == "quoted"
    assert "quote" in result.candidates[0].reasons


async def test_diagnostic_cuda_unavailable_is_reported_without_cloud_or_cpu(
    components, monkeypatch
) -> None:
    archive, index = components
    await archive.put_message(message("old", "活动暗号银杏"))
    from lemonbot.config.settings import AppSettings

    settings = AppSettings.model_validate(
        {
            "context_memory": {
                "embedding_assets_root": str(archive.path.parent),
            }
        }
    )
    monkeypatch.setattr(retrieval_cli, "load_settings", lambda path: settings)

    async def unavailable(**kwargs):
        raise EmbeddingError("embedding_cuda_unavailable")

    monkeypatch.setattr(retrieval_cli.IsolatedEmbeddingWorker, "create", unavailable)
    args = argparse.Namespace(
        config=None,
        database=archive.path,
        index=index.path,
        channel=SCOPE.channel,
        account=SCOPE.account_id,
        conversation=SCOPE.conversation_id,
        kind=SCOPE.kind,
        cuda=True,
        command="index",
    )
    result = await retrieval_cli.execute(args)
    assert result["report"]["degraded_reason"] == "embedding_worker_unavailable"
    assert result["report"]["pending_vectors"] == 1


async def test_long_sources_keep_tail_retrievable_and_original_message_unchanged(
    components,
) -> None:
    archive, _ = components
    original = message("long", "合成资料。" * 900 + "精确尾部口令梧桐")
    await archive.put_message(original)
    result = await retriever(components, options=RetrievalOptions(recent_messages=0)).retrieve(
        current=message("query", "尾部口令"),
    )
    assert "梧桐" in result.candidates[0].summary
    assert await archive.get_message(source("long")) == original


async def test_twenty_candidate_cap_and_caller_limit(components) -> None:
    archive, _ = components
    for i in range(30):
        await archive.put_message(message(f"document-{i}", "活动资料"))
    service = retriever(components, options=RetrievalOptions(candidate_char_budget=32000))
    current = message("query", "活动资料")
    assert len((await service.retrieve(current=current)).candidates) == 20
    assert len((await service.retrieve(current=current, limit=3)).candidates) == 3


def test_mixed_text_query_limits_and_central_settings() -> None:
    from lemonbot.config.settings import AppSettings
    from lemonbot.memory.unified.documents import current_query, query_over_limit

    assert "python" in terms("Python列表") and "列表" in terms("Python列表")
    current = message("huge", "a" * 9000)
    assert len(current_query(current)) == 8000 and query_over_limit(current)
    settings = AppSettings.model_validate(
        {
            "context_memory": {
                "retrieval_max_messages": 12,
                "retrieval_recent_messages": 2,
            }
        }
    )
    options = retrieval_cli.configured_options(settings)
    assert options.max_messages == 12 and options.recent_messages == 2
    assert not settings.context_memory.enabled and not settings.context_memory.archive_enabled
    with pytest.raises(ValueError):
        AppSettings.model_validate({"context_memory": {"retrieval_semantic_min_similarity": 2}})


async def test_text_quote_does_not_grant_another_part_as_old_image_context(components) -> None:
    archive, _ = components
    await archive.put_message(
        message(
            "combined",
            "文字来源",
            parts=(
                MessagePart(part_id="text", kind="text", text="文字来源"),
                MessagePart(part_id="image", kind="image", unavailable_reason="not_fetched"),
            ),
        )
    )
    await archive.put_observation(
        Observation(
            observation_id="description",
            source=source("combined", part_id="image"),
            kind="image_description",
            status="available",
            text="红色杯子",
            extractor_version="synthetic/v1",
            created_at=NOW,
        )
    )
    current = message(
        "query",
        references=(source("combined", part_id="text"),),
        parts=(
            MessagePart(part_id="text", kind="text", text="这张图片里的杯子呢？"),
            MessagePart(part_id="image", kind="image", unavailable_reason="new_not_fetched"),
        ),
    )
    result = await retriever(components, worker=SyntheticEmbedder()).retrieve(current=current)
    assert result.candidates
    assert all(candidate.sources[0].part_id == "text" for candidate in result.candidates)
