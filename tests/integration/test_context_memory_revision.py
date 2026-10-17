"""Stage 6 review regressions; synthetic inputs, no network or live account."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from test_two_stage_context import (
    SCOPE,
    enable_maintenance,
    ingest,
    memory_entry,
    msg,
    no_cloud_guard,
    ref,
    seed_consolidation,
)
from test_two_stage_context import (
    system as system,
)

from lemonbot.domain.models import ModelResponse
from lemonbot.memory.unified import maintenance
from lemonbot.memory.unified.archive import ArchiveError, scope_key
from lemonbot.memory.unified.assembly import ContextValidationError
from lemonbot.memory.unified.consolidation import MemoryConsolidator
from lemonbot.memory.unified.delivery import delivery_metadata
from lemonbot.memory.unified.evidence import EvidenceSnapshot
from lemonbot.memory.unified.models import (
    ContextDecision,
    CorrectionNotice,
    MessagePart,
    Observation,
)
from lemonbot.orchestration import FakeConnector, PipelineStatus


async def seed_page(system):
    store = await seed_consolidation(system)
    await system["archive"].put_message(
        msg(
            "page",
            occurred_at=datetime.now(UTC) - timedelta(days=1),
            parts=(MessagePart(part_id="link", kind="link", url="https://example.org"),),
        )
    )
    observation = Observation(
        observation_id="expiring",
        source=ref("page", "link"),
        kind="page_body",
        status="available",
        text="Only in page evidence",
        extractor_version="review/v1",
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    await system["archive"].put_observation(observation)
    return store, observation


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "expiry",
        "observation",
        "removed",
        "message",
        "catalog_version",
        "catalog_superseded",
        "catalog_expiry",
    ],
)
async def test_cloud_result_bound_to_request_snapshot(system, monkeypatch, fault):
    store, observation = await seed_page(system)
    await system["archive"].put_message(msg("catalog-source", "catalog fact"))
    if fault == "catalog_expiry":
        catalog = await memory_entry(system, "catalog", "page", sources=(ref("page", "link"),))
    else:
        catalog = await memory_entry(system, "catalog", "catalog-source")
    await store.put_memory(catalog)
    original = system["model"].answer

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(minutes=2)

    def answer(request):
        assert "Only in page evidence" in request.messages[-1].content
        assert "catalog" in json.loads(request.messages[-1].content)["memory_catalog"][0]["id"]
        if fault in {"expiry", "catalog_expiry"}:
            monkeypatch.setattr(maintenance, "datetime", Later)
        elif fault != "none":
            with sqlite3.connect(system["archive"].path) as db:
                if fault == "observation":
                    changed = observation.model_copy(update={"text": "changed after request"})
                    db.execute(
                        "UPDATE context_archive_observations SET payload_json=? "
                        "WHERE observation_id=?",
                        (changed.model_dump_json(), "expiring"),
                    )
                elif fault == "removed":
                    db.execute(
                        "DELETE FROM context_archive_observations WHERE observation_id=?",
                        ("expiring",),
                    )
                elif fault == "message":
                    db.execute(
                        "UPDATE context_archive_messages SET payload_sha256=? WHERE message_id=?",
                        ("0" * 64, "page"),
                    )
                elif fault == "catalog_version":
                    db.execute(
                        "UPDATE unified_memories SET payload=? WHERE memory_id=?",
                        (
                            catalog.model_copy(
                                update={"text": "changed version"}
                            ).model_dump_json(),
                            "catalog",
                        ),
                    )
                elif fault == "catalog_superseded":
                    newer = catalog.model_copy(
                        update={"memory_id": "replacement", "supersedes": ("catalog",)}
                    )
                    row = db.execute(
                        "SELECT evidence FROM unified_memories WHERE memory_id=?", ("catalog",)
                    ).fetchone()
                    db.execute(
                        "INSERT INTO unified_memories VALUES (?,?,?,?)",
                        (scope_key(SCOPE), "replacement", newer.model_dump_json(), row[0]),
                    )
                    db.execute(
                        "INSERT INTO unified_supersessions VALUES (?,?,?)",
                        (scope_key(SCOPE), "catalog", "replacement"),
                    )
        response = original(request)
        value = json.loads(response.content)
        # First proposal could be committed before the second fails without batch atomicity.
        value["entries"].append(
            {
                "kind": "fact",
                "text": "Only in page evidence",
                "source_ids": ["page"],
                "supersedes": [],
            }
        )
        return response.model_copy(update={"content": json.dumps(value)})

    system["model"].answer = answer
    runner = MemoryConsolidator(store, system["model"], system["budget"])
    if fault == "none":
        assert await runner.run_once(SCOPE, before_call=no_cloud_guard)
    else:
        with pytest.raises(ArchiveError, match=r"^MemoryEvidenceChanged$"):
            await runner.run_once(SCOPE, before_call=no_cloud_guard)
    async with system["archive"]._read() as db:
        job = await (await db.execute("SELECT state,charged FROM unified_jobs")).fetchone()
        payloads = await (await db.execute("SELECT payload FROM unified_memories")).fetchall()
    assert job[0] == ("done" if fault == "none" else "rejected")
    response = system["model"].requests[-1]
    expected_charge = (
        system["budget"]
        .price_for("deepseek", "deepseek-flash")
        .cost(prompt_tokens=system["model"].count_tokens(response.messages), completion_tokens=100)
    )
    assert Decimal(job[1]) == expected_charge
    assert sum("cloud-consolidation/v1" in row[0] for row in payloads) == (
        2 if fault == "none" else 0
    )
    restarted = MemoryConsolidator(
        maintenance.UnifiedMemoryStore(system["archive"]), system["model"], system["budget"]
    )
    if fault == "message":
        with pytest.raises(ArchiveError, match="ArchivePayloadCorrupt"):
            await restarted.run_once(SCOPE, before_call=no_cloud_guard)
    else:
        assert not await restarted.run_once(SCOPE, before_call=no_cloud_guard)
    assert len(system["model"].requests) == 1
    assert (await system["budget"].snapshot()).daily_reserved == 0
    if fault == "none":
        monkeypatch.setattr(maintenance, "datetime", Later)
        memories = await store.list_memories(SCOPE)
        assert not any(e.active for e in memories if e.text == "Only in page evidence")
        assert "Only in page evidence" not in await store.topics(SCOPE)


async def corrected_parts(system):
    store = enable_maintenance(system)
    await system["archive"].put_message(
        msg(
            "old",
            parts=(
                MessagePart(part_id="text", kind="text", text="喜欢青柠"),
                MessagePart(part_id="other", kind="text", text="项目编号 ALPHA42"),
            ),
        )
    )
    await store.put_memory(await memory_entry(system))
    await system["archive"].put_message(msg("correct", "更正：喜欢薄荷"))
    corrected = await memory_entry(
        system, "new", "correct", text="喜欢薄荷", supersedes=("memory",)
    )
    await store.put_memory(corrected)
    return store, corrected


@pytest.mark.parametrize("part", ["text", "other"])
async def test_partial_correction_preserves_precise_quote(system, part):
    store, corrected = await corrected_parts(system)
    current = msg(
        "question",
        "引用的内容是什么？",
        occurred_at=datetime.now(UTC),
        references=(ref("old", part),),
    )
    result = await system["flow"].retriever.retrieve(current=current)
    quoted = next(
        c for c in result.candidates if "quote" in c.reasons and c.sources == (ref("old", part),)
    )
    assert bool(quoted.corrections) is (part == "text")
    decision = ContextDecision(
        mode="related", relation="quote", selected_candidate_ids=(quoted.candidate_id,)
    )
    bundle = await system["flow"].assembler.assemble(
        current=current, retrieval=result, decision=decision
    )
    assert bundle.selected_sources == (ref("old", part),)
    assert bool(bundle.corrections) is (part == "text")
    if part == "text":
        assert bundle.corrections[0].latest == corrected
    metadata = delivery_metadata(bundle)
    assert (
        metadata["memory_ids"] == []
    )  # Displayed replacements are never implicit correction targets.
    assert metadata["corrected_sources"] == [ref("old", part).model_dump(mode="json")]
    assert (await store.get_memory(scope=SCOPE, memory_id="new")).active


async def test_classifier_and_answer_receive_history_with_current_correction(system):
    await corrected_parts(system)

    def classify(request):
        candidates = json.loads(request.messages[-1].content)["candidates"]
        chosen = next(c for c in candidates if c["sources"] == [ref("old").model_dump(mode="json")])
        assert chosen["summary"] == "喜欢青柠"
        assert chosen["corrections"][0]["latest"]["text"] == "喜欢薄荷"
        return ContextDecision(
            mode="related", relation="quote", selected_candidate_ids=(chosen["candidate_id"],)
        ).model_dump_json()

    system["model"].classify = classify

    def answer(request):
        history = json.loads(request.messages[1].content.split("\n", 1)[1])
        assert history[0]["excerpt"] == "喜欢青柠"
        assert history[0]["evidence_role"] == "verbatim_source_excerpt"
        assert history[0]["excerpt_truncated"] is False
        original = json.loads(request.messages[-1].content)
        assert original["corrections"][0]["latest"]["text"] == "喜欢薄荷"
        assert "currently valid claims" in request.messages[0].content
        return ModelResponse(model="deepseek-flash", content="以前青柠，现在薄荷")

    system["model"].answer = answer
    await ingest(
        system,
        msg(
            "question", "我以前怎么说的？", occurred_at=datetime.now(UTC), references=(ref("old"),)
        ),
    )
    assert (await system["pipeline"].process_once()).status is PipelineStatus.COMPLETED
    assert len(system["model"].requests) == 2
    assert not system["pipeline"].context_flow.last_bundle.memories


async def test_chain_revalidated_and_missing_latest_never_revives_old_fact(system):
    store, _ = await corrected_parts(system)
    await system["archive"].put_message(msg("third", "更正：喜欢柠檬"))
    third = await memory_entry(
        system, "third-memory", "third", text="喜欢柠檬", supersedes=("new",)
    )
    await store.put_memory(third)
    notices = await store.correction_notices((ref("old"),))
    assert notices[0].replacement_ids == ("new", "third-memory")
    assert notices[0].latest == third
    current = msg("q", "以前呢？", occurred_at=datetime.now(UTC), references=(ref("old"),))
    result = await system["flow"].retriever.retrieve(current=current)
    selected = next(c for c in result.candidates if c.sources == (ref("old"),))
    async with system["archive"]._write() as db:
        await db.execute(
            "UPDATE unified_memories SET evidence=? WHERE memory_id=?", ("[]", "third-memory")
        )
    assert (await store.correction_notices((ref("old"),)))[0].latest is None
    with pytest.raises(ContextValidationError, match="ContextCorrectionsChanged"):
        await system["flow"].assembler.assemble(
            current=current,
            retrieval=result,
            decision=ContextDecision(
                mode="related", relation="quote", selected_candidate_ids=(selected.candidate_id,)
            ),
        )
    fresh = await system["flow"].retriever.retrieve(current=current)
    selected = next(c for c in fresh.candidates if c.sources == (ref("old"),))
    bundle = await system["flow"].assembler.assemble(
        current=current,
        retrieval=fresh,
        decision=ContextDecision(
            mode="related", relation="quote", selected_candidate_ids=(selected.candidate_id,)
        ),
    )
    assert bundle.corrections[0].latest is None
    assert not bundle.memories


async def test_message_level_legacy_notice_does_not_delete_other_part(system):
    store, _ = await corrected_parts(system)
    # Existing message-level provenance is ambiguous, so mark the message rather than erase it.
    old = await memory_entry(
        system, "whole", sources=(ref("old").model_copy(update={"part_id": None}),)
    )
    await store.put_memory(old)
    await system["archive"].put_message(msg("whole-correction", "更正：青柠改为薄荷"))
    await store.put_memory(
        await memory_entry(system, "whole-new", "whole-correction", supersedes=("whole",))
    )
    current = msg(
        "q", "项目编号？", occurred_at=datetime.now(UTC), references=(ref("old", "other"),)
    )
    result = await system["flow"].retriever.retrieve(current=current)
    quoted = next(c for c in result.candidates if c.sources == (ref("old", "other"),))
    assert quoted.summary == "项目编号 ALPHA42"
    assert quoted.corrections[0].sources[0].part_id is None


@pytest.mark.parametrize(
    "fault", ["cycle", "too_long", "missing", "author", "foreign_scope", "notice_limit"]
)
async def test_invalid_correction_chains_fail_closed(system, fault):
    store, _ = await corrected_parts(system)
    async with system["archive"]._write() as db:
        if fault == "cycle":
            await db.execute(
                "INSERT INTO unified_supersessions VALUES (?,?,?)",
                (scope_key(SCOPE), "new", "memory"),
            )
        elif fault == "too_long":
            # Build valid lineage then cross the traversal bound; no cloud calls needed.
            last = await store._get(db, SCOPE, "new")
            for i in range(32):
                item = last.model_copy(
                    update={"memory_id": f"chain-{i}", "supersedes": (last.memory_id,)}
                )
                await store._put(db, item)
                last = item
        elif fault == "missing":
            await db.execute("UPDATE unified_supersessions SET new_id=?", ("missing",))
        elif fault in {"author", "foreign_scope"}:
            item = await store._get(db, SCOPE, "new")
            item = item.model_copy(
                update={"author_ids": ("bob",)}
                if fault == "author"
                else {"scope": SCOPE.model_copy(update={"account_id": "other-account"})}
            )
            await db.execute(
                "UPDATE unified_memories SET payload=? WHERE memory_id=?",
                (item.model_dump_json(), "new"),
            )
        else:
            for i in range(6):
                old = await memory_entry(system, f"old-{i}")
                await store._put(db, old)
                new = await memory_entry(system, f"new-{i}", "correct", supersedes=(old.memory_id,))
                await store._put(db, new)
    with pytest.raises(ArchiveError):
        await store.correction_notices((ref("old"),))
    await ingest(
        system, msg("q", "引用旧内容", occurred_at=datetime.now(UTC), references=(ref("old"),))
    )
    assert (await system["pipeline"].process_once()).status is PipelineStatus.COMPLETED
    assert system["flow"].last_bundle.decision.mode == "clarify"
    assert not system["flow"].last_bundle.source_messages
    assert len(system["model"].requests) == 2


async def test_snapshot_and_correction_contracts_are_immutable_and_scoped(system):
    store, corrected = await corrected_parts(system)
    original = await system["archive"].get_message(ref("old"))
    snapshot = EvidenceSnapshot.from_inputs(SCOPE, (original,), (), (corrected,))
    with pytest.raises(ValidationError):
        snapshot.memories = ()
    with pytest.raises(ValueError):
        EvidenceSnapshot.from_inputs(
            SCOPE,
            (
                original.model_copy(
                    update={"scope": SCOPE.model_copy(update={"account_id": "other"})}
                ),
            ),
            (),
            (),
        )
    notice = (await store.correction_notices((ref("old"),)))[0]
    with pytest.raises(ValidationError):
        CorrectionNotice.model_validate(
            {
                **notice.model_dump(),
                "latest": corrected.model_copy(
                    update={"scope": SCOPE.model_copy(update={"account_id": "other"})}
                ),
            }
        )


@pytest.mark.parametrize("unknown", [False, True])
async def test_ack_only_correction_is_part_scoped_and_keeps_other_text(system, unknown):
    store = enable_maintenance(system)
    await system["archive"].put_message(
        msg(
            "old",
            parts=(
                MessagePart(part_id="text", kind="text", text="喜欢青柠"),
                MessagePart(part_id="other", kind="text", text="项目编号 ALPHA42"),
            ),
        )
    )

    def classify(request):
        selected = next(
            c
            for c in json.loads(request.messages[-1].content)["candidates"]
            if c["sources"] == [ref("old").model_dump(mode="json")]
        )
        return ContextDecision(
            mode="related",
            relation="correction",
            selected_candidate_ids=(selected["candidate_id"],),
        ).model_dump_json()

    system["model"].classify = classify
    await ingest(system, msg("correct", "更正：喜欢薄荷", occurred_at=datetime.now(UTC)))
    await system["pipeline"].process_once()
    assert not await store.list_memories(SCOPE)
    connector = FakeConnector()
    connector.raise_after_accept = unknown
    result = await system["pipeline"].dispatch_once(connector, channel="fake")
    assert result.status is (PipelineStatus.UNKNOWN if unknown else PipelineStatus.ACKNOWLEDGED)
    entries = await store.list_memories(SCOPE)
    if unknown:
        assert not entries
    else:
        old = next(e for e in entries if not e.active)
        assert old.sources == (ref("old"),)
        assert old.text == "喜欢青柠" and "ALPHA42" not in old.text
        assert not await store.correction_notices((ref("old", "other"),))
    assert len(system["model"].requests) == 2
    assert len(connector.delivered) == 1


async def test_expiry_while_applying_batch_rolls_back_all_entries(system, monkeypatch):
    store, _ = await seed_page(system)
    real_put = store._put
    calls = 0

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(minutes=2)

    async def slow_put(db, entry, **kwargs):
        nonlocal calls
        result = await real_put(db, entry, **kwargs)
        calls += 1
        monkeypatch.setattr(maintenance, "datetime", Later)
        return result

    monkeypatch.setattr(store, "_put", slow_put)
    with pytest.raises(ArchiveError, match="MemoryEvidenceChanged"):
        await MemoryConsolidator(store, system["model"], system["budget"]).run_once(
            SCOPE, before_call=no_cloud_guard
        )
    assert calls == 1
    assert not await store.list_memories(SCOPE)
    async with system["archive"]._read() as db:
        assert (await (await db.execute("SELECT state FROM unified_jobs")).fetchone())[
            0
        ] == "rejected"


async def test_oversized_correction_uses_clarification_not_old_claim(system):
    store, _ = await corrected_parts(system)
    # A complete replacement cannot fit in the candidate budget; never truncate it into a claim.
    async with system["archive"]._write() as db:
        item = await store._get(db, SCOPE, "new")
        await db.execute(
            "UPDATE unified_memories SET payload=? WHERE memory_id=?",
            (item.model_copy(update={"text": "新版本" * 10000}).model_dump_json(), "new"),
        )
    await ingest(
        system, msg("q", "旧说法是什么？", occurred_at=datetime.now(UTC), references=(ref("old"),))
    )
    assert (await system["pipeline"].process_once()).status is PipelineStatus.COMPLETED
    offered = json.loads(system["model"].requests[0].messages[-1].content)["candidates"]
    quoted = next(c for c in offered if "quote" in c["reasons"])
    assert quoted["correction_error"] == "CorrectionContextExceedsBudget"
    assert system["flow"].last_bundle.decision.mode == "clarify"
    assert not system["flow"].last_bundle.source_messages
    assert not system["flow"].last_bundle.corrections
    assert len(system["model"].requests) == 2


async def test_trimming_corrected_quote_for_classifier_budget_clarifies(system, monkeypatch):
    await corrected_parts(system)
    flow = system["flow"]
    real_fits = flow._fits
    from lemonbot.memory.unified.flow import CLASSIFIER_PREFIX

    def limited(messages, extra=0):
        if messages[0].content == CLASSIFIER_PREFIX:
            candidates = json.loads(messages[-1].content)["candidates"]
            if any(c["corrections"] for c in candidates):
                return False
        return real_fits(messages, extra)

    monkeypatch.setattr(flow, "_fits", limited)
    await ingest(
        system,
        msg("q", "引用的旧说法呢？", occurred_at=datetime.now(UTC), references=(ref("old"),)),
    )
    assert (await system["pipeline"].process_once()).status is PipelineStatus.COMPLETED
    assert flow.last_bundle.decision.mode == "clarify"
    assert not flow.last_bundle.source_messages


async def test_expired_observation_keeps_raw_url_and_unrelated_part(system, monkeypatch):
    store, _ = await seed_page(system)
    await store.put_memory(
        await memory_entry(system, "page-memory", "page", sources=(ref("page", "link"),))
    )

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(minutes=2)

    monkeypatch.setattr(maintenance, "datetime", Later)
    current = msg(
        "q", "这个链接呢？", occurred_at=Later.now(UTC), references=(ref("page", "link"),)
    )
    # Retrieval and its units use their own clock; advance it as well, without touching real data.
    import lemonbot.memory.unified.assembly as assembly
    import lemonbot.memory.unified.retrieval as retrieval

    monkeypatch.setattr(retrieval, "datetime", Later)
    monkeypatch.setattr(assembly, "datetime", Later)
    result = await system["flow"].retriever.retrieve(current=current)
    url = next(
        c
        for c in result.candidates
        if c.sources == (ref("page", "link"),) and not c.observation_ids
    )
    assert url.summary == "https://example.org"
    assert not url.corrections
    assert not any(c.observation_ids for c in result.candidates)
    bundle = await system["flow"].assembler.assemble(
        current=current,
        retrieval=result,
        decision=ContextDecision(
            mode="related", relation="quote", selected_candidate_ids=(url.candidate_id,)
        ),
    )
    assert not bundle.observations
    assert not (await store.get_memory(scope=SCOPE, memory_id="page-memory")).active


async def test_correction_notice_never_restores_expired_original_image(system):
    from test_two_stage_context import picture

    image = await picture(system)
    store = enable_maintenance(system)
    old = await memory_entry(
        system,
        "image-memory",
        image.message_id,
        sources=(ref(image.message_id, "image"),),
        text="历史描述：红杯子",
    )
    await store.put_memory(old)
    await system["archive"].put_message(msg("image-correction", "更正：蓝杯子"))
    new = await memory_entry(
        system, "image-new", "image-correction", text="更正：蓝杯子", supersedes=("image-memory",)
    )
    await store.put_memory(new)
    system["images"].retention_days = -1
    current = msg(
        "q", "看看原图", occurred_at=datetime.now(UTC), references=(ref(image.message_id, "image"),)
    )
    result = await system["flow"].retriever.retrieve(current=current)
    quoted = next(c for c in result.candidates if "quote" in c.reasons)
    bundle = await system["flow"].assembler.assemble(
        current=current,
        retrieval=result,
        decision=ContextDecision(
            mode="related",
            relation="quote",
            selected_candidate_ids=(quoted.candidate_id,),
            image_source=ref(image.message_id, "image"),
            needs_original_image=True,
        ),
    )
    assert bundle.corrections[0].latest == new
    assert bundle.decision.image_source is None
    assert bundle.unavailable_sources == (ref(image.message_id, "image"),)
    assert await system["flow"].assembler.image(bundle) is None


async def test_classifier_turn_never_exposes_more_than_six_correction_notices(system):
    store = enable_maintenance(system)
    references = []
    for i in range(7):
        identifier = f"original-{i}"
        await system["archive"].put_message(msg(identifier, f"原说法 {i}"))
        old = await memory_entry(system, f"old-{i}", identifier)
        await store.put_memory(old)
        await system["archive"].put_message(msg(f"change-{i}", f"更正 {i}"))
        await store.put_memory(
            await memory_entry(system, f"new-{i}", f"change-{i}", supersedes=(old.memory_id,))
        )
        references.append(ref(identifier))
    current = msg(
        "q", "这些引用的旧说法呢？", occurred_at=datetime.now(UTC), references=tuple(references)
    )
    result = await system["flow"].retriever.retrieve(current=current)
    unique = {n.superseded_memory_id for c in result.candidates for n in c.corrections}
    assert len(unique) <= 6
    assert any(c.correction_error for c in result.candidates if "quote" in c.reasons)
    await ingest(system, current)
    assert (await system["pipeline"].process_once()).status is PipelineStatus.COMPLETED
    assert system["flow"].last_bundle.decision.mode == "clarify"
    assert not system["flow"].last_bundle.source_messages


@pytest.mark.parametrize("expire", [False, True])
@pytest.mark.parametrize("supersede", [False, True])
async def test_catalog_evidence_checked_at_commit_including_intended_correction(
    system, monkeypatch, expire, supersede
):
    store = await seed_consolidation(system)
    await system["archive"].put_message(
        msg(
            "catalog-page",
            occurred_at=datetime.now(UTC),
            parts=(MessagePart(part_id="link", kind="link", url="https://example.org/catalog"),),
        )
    )
    await system["archive"].put_observation(
        Observation(
            observation_id="catalog-body",
            source=ref("catalog-page", "link"),
            kind="page_body",
            status="available",
            text="Catalog evidence",
            extractor_version="review/v1",
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(seconds=30),
        )
    )
    catalog = await memory_entry(
        system, "catalog", "catalog-page", sources=(ref("catalog-page", "link"),)
    )
    await store.put_memory(catalog)
    original_answer = system["model"].answer

    def answer(request):
        body = json.loads(request.messages[-1].content)
        assert all(m["id"] != "catalog-page" for m in body["messages"])
        response = original_answer(request)
        content = json.loads(response.content)
        content["entries"][0]["supersedes"] = ["catalog"] if supersede else []
        return response.model_copy(update={"content": json.dumps(content)})

    system["model"].answer = answer
    original_put = store._put

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(minutes=2)

    async def slow_put(db, entry, **kwargs):
        result = await original_put(db, entry, **kwargs)
        if expire:
            monkeypatch.setattr(maintenance, "datetime", Later)
        return result

    monkeypatch.setattr(store, "_put", slow_put)
    runner = MemoryConsolidator(store, system["model"], system["budget"])
    if expire:
        with pytest.raises(ArchiveError, match="MemoryEvidenceChanged"):
            await runner.run_once(SCOPE, before_call=no_cloud_guard)
    else:
        assert await runner.run_once(SCOPE, before_call=no_cloud_guard)
    async with system["archive"]._read() as db:
        state = (await (await db.execute("SELECT state FROM unified_jobs")).fetchone())[0]
        count = (await (await db.execute("SELECT count(*) FROM unified_supersessions")).fetchone())[
            0
        ]
    assert state == ("rejected" if expire else "done")
    assert count == int(supersede and not expire)
