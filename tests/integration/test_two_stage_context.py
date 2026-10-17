from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from lemonbot.domain import (
    InboundEvent,
    ModelRequest,
    ModelResponse,
    ToolCall,
    ToolManifest,
    ToolResult,
)
from lemonbot.memory.unified.archive import SQLiteMessageArchive
from lemonbot.memory.unified.assembly import ContextValidationError, VerifiedContextAssembler
from lemonbot.memory.unified.flow import CLASSIFIER_PREFIX, TwoStageContextFlow, parse_decision
from lemonbot.memory.unified.images import BoundImageResolver
from lemonbot.memory.unified.index import SQLiteRetrievalIndex
from lemonbot.memory.unified.models import (
    ContextDecision,
    ConversationScope,
    Message,
    MessagePart,
    Observation,
    SourceRef,
)
from lemonbot.memory.unified.retrieval import HybridContextRetriever
from lemonbot.models.budget import BudgetLimits, ModelPrice
from lemonbot.models.gateway import ModelTransportError
from lemonbot.models.persistent_budget import PersistentBudgetManager
from lemonbot.models.worker_proxy import IsolatedModelBackend
from lemonbot.orchestration import (
    EventPipeline,
    FakeConnector,
    FakeModelBackend,
    PipelineConfig,
    PipelineStatus,
)
from lemonbot.policy import DeterministicPolicy
from lemonbot.storage import CoreRepository, Database
from lemonbot.storage.migrate import upgrade_database
from lemonbot.tools.attachments import AttachmentStore

NOW = datetime.now(UTC)
SCOPE = ConversationScope(
    channel="fake", account_id="synthetic-account", conversation_id="group-a", kind="group"
)


def msg(identifier: str, text: str = "current question", **changes: Any) -> Message:
    return Message.model_validate(
        {
            "scope": SCOPE,
            "message_id": identifier,
            "event_id": identifier,
            "sender_id": "alice",
            "direction": "inbound",
            "occurred_at": NOW,
            "parts": (MessagePart(part_id="text", kind="text", text=text),),
            **changes,
        }
    )


def ref(identifier: str, part: str = "text", scope: ConversationScope = SCOPE) -> SourceRef:
    return SourceRef(scope=scope, message_id=identifier, part_id=part)


def standalone(_request: ModelRequest) -> str:
    return ContextDecision(mode="standalone", relation="none").model_dump_json()


def related(request: ModelRequest) -> str:
    candidates = json.loads(request.messages[-1].content)["candidates"]
    return ContextDecision(
        mode="related", relation="followup", selected_candidate_ids=(candidates[0]["candidate_id"],)
    ).model_dump_json()


class MeteredFake(FakeModelBackend):
    def __init__(self, budget: PersistentBudgetManager) -> None:
        super().__init__()
        self.budget = budget
        self.classify = standalone
        self.answer = lambda request: ModelResponse(
            content="synthetic answer", model="deepseek-flash"
        )
        self.fail = False
        self.before_reserved = []
        self.class_image_accepted = True

    def count_tokens(self, messages) -> int:
        return IsolatedModelBackend.count_tokens(self, messages)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.before_reserved.append((await self.budget.snapshot()).daily_reserved)
        reservation = await self.budget.reserve(
            provider="deepseek",
            model="deepseek-flash",
            prompt_tokens=self.count_tokens(request.messages),
            maximum_completion_tokens=request.max_tokens,
        )
        self.requests.append(request)
        if self.fail:
            await self.budget.settle_unknown(reservation.reservation_id)
            raise ModelTransportError("synthetic timeout")
        is_class = request.messages[0].content == CLASSIFIER_PREFIX
        response = (
            ModelResponse(
                content=self.classify(request),
                model="deepseek-flash",
                image_input_accepted=self.class_image_accepted
                if any(m.images for m in request.messages)
                else None,
            )
            if is_class
            else self.answer(request)
        )
        response = response.model_copy(
            update={"prompt_tokens": self.count_tokens(request.messages), "completion_tokens": 100}
        )
        await self.budget.settle(
            reservation.reservation_id,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
        )
        return response


@pytest.fixture
async def system(tmp_path):
    upgrade_database(tmp_path / "archive.db")
    database = Database.from_path(tmp_path / "archive.db")
    await database.initialise()
    repository = CoreRepository(database)
    await repository.set_allowlisted(SCOPE.channel, SCOPE.conversation_id)
    archive = SQLiteMessageArchive(tmp_path / "archive.db")
    budget = await PersistentBudgetManager.create(
        database_path=archive.path,
        limits=BudgetLimits(daily=Decimal("5"), monthly=Decimal("100")),
        prices={("deepseek", "deepseek-flash"): ModelPrice(Decimal("1"), Decimal("1"))},
    )
    attachments = AttachmentStore(archive.path, tmp_path / "objects", minimum_free_bytes=0)
    await attachments.initialize()
    images = BoundImageResolver(archive, repository, attachments)
    retriever = HybridContextRetriever(
        archive, SQLiteRetrievalIndex(tmp_path / "index.db", archive.path)
    )
    assembler = VerifiedContextAssembler(archive, images)
    model = MeteredFake(budget)
    flow = TwoStageContextFlow(
        archive=archive,
        retriever=retriever,
        assembler=assembler,
        model=model,
        budget=budget,
        scopes={SCOPE.conversation_id: SCOPE},
        system_prompt="Answer briefly; do not invent image content.",
    )
    pipeline = EventPipeline(
        repository,
        DeterministicPolicy(repository),
        model,
        context_flow=flow,
        config=PipelineConfig(images_enabled=True, model_max_tokens=512, max_context_tokens=8192),
    )
    components = {
        "database": database,
        "repository": repository,
        "archive": archive,
        "budget": budget,
        "attachments": attachments,
        "flow": flow,
        "model": model,
        "pipeline": pipeline,
        "retriever": retriever,
        "images": images,
    }
    try:
        yield components
    finally:
        await retriever.close()
        await database.close()


async def ingest(system, current: Message, **metadata: Any) -> InboundEvent:
    await system["archive"].put_message(current)
    event = InboundEvent(
        channel=current.scope.channel,
        chat_id=current.scope.conversation_id,
        event_id=current.message_id,
        sender_id=current.sender_id,
        text="\n".join(p.text or p.url or "" for p in current.parts),
        occurred_at=current.occurred_at,
        metadata={
            "wechat_mode": "conversation",
            "context_scope": "context_" + "a" * 64,
            **metadata,
        },
    )
    await system["pipeline"].ingest(event)
    return event


async def picture(system, identifier: str = "image") -> Message:
    item = msg(
        identifier,
        parts=(MessagePart(part_id="image", kind="image", unavailable_reason="not_loaded"),),
    )
    attachment = await system["attachments"].ingest(
        channel=SCOPE.channel,
        chat_id=SCOPE.conversation_id,
        event_id=identifier,
        content=b"synthetic image fixture, not real vision acceptance",
        media_type="image/png",
    )
    await ingest(
        system,
        item,
        attachment_ids=[str(attachment.attachment_id)],
        image_source="validated_wal_snapshot",
        image_source_ref=identifier,
    )
    return item


async def test_standalone_drops_all_old_text_and_uses_two_calls_single_outbox(system) -> None:
    await system["archive"].put_message(
        msg("old", "old unrelated secret", occurred_at=NOW - timedelta(minutes=1))
    )
    await ingest(system, msg("new", "Python list sorting?"))
    assert (await system["pipeline"].process_once()).status is PipelineStatus.COMPLETED
    requests = system["model"].requests
    assert len(requests) == 2
    assert not requests[0].tools and not requests[0].deep
    assert "old unrelated secret" in requests[0].messages[-1].content
    assert "old unrelated secret" not in " ".join(m.content or "" for m in requests[1].messages)
    assert (
        json.loads(requests[1].messages[-1].content)["current"]["parts"][0]["text"]
        == "Python list sorting?"
    )
    assert system["model"].before_reserved[0] == Decimal("0.017408")
    connector = FakeConnector()
    assert (
        await system["pipeline"].dispatch_once(connector, channel="fake")
    ).status is PipelineStatus.ACKNOWLEDGED
    assert len(connector.delivered) == 1
    assert (
        await system["pipeline"].dispatch_once(connector, channel="fake")
    ).status is PipelineStatus.IDLE


async def test_related_text_is_selected_with_author_time_and_exact_current(system) -> None:
    await system["archive"].put_message(
        msg("old", "活动暗号银杏", sender_id="bob", occurred_at=NOW - timedelta(minutes=1))
    )
    system["model"].classify = related
    await ingest(system, msg("query", "活动暗号是什么？"))
    assert (await system["pipeline"].process_once()).status is PipelineStatus.COMPLETED
    history = system["model"].requests[-1].messages[1].content
    assert "UNTRUSTED" in history and "bob" in history and "银杏" in history
    assert system["flow"].last_bundle.source_messages[0].sender_id == "bob"


@pytest.mark.parametrize(
    "invalid",
    [
        "not json",
        '{"mode":"standalone","mode":"related"}',
        '{"mode":"standalone","relation":"none","extra":"rewrite"}',
        '{"mode":"related","relation":"followup","selected_candidate_ids":["not-offered"]}',
        '{"mode":"related","relation":"followup","selected_candidate_ids":["x","x"]}',
        '{"mode":"standalone","relation":"none","needs_original_image":"false"}',
        '{"mode":"standalone","relation":"none","selected_candidate_ids":[],"current_observation":NaN}',
    ],
)
async def test_invalid_classification_falls_back_without_retry_or_old_context(
    system, invalid
) -> None:
    await system["archive"].put_message(msg("old", "unrelated history"))
    system["model"].classify = lambda request: invalid
    await ingest(system, msg("query", "tell me a joke"))
    assert (await system["pipeline"].process_once()).status is PipelineStatus.COMPLETED
    assert len(system["model"].requests) == 2
    assert system["flow"].last_fallback == "classification_invalid"
    assert not system["flow"].last_bundle.source_messages


async def test_invalid_dependent_question_requests_clarification(system) -> None:
    system["model"].classify = lambda request: "invalid"
    await ingest(system, msg("query", "刚才那个是什么？"))
    assert (await system["pipeline"].process_once()).status is PipelineStatus.COMPLETED
    assert system["flow"].last_bundle.decision.mode == "clarify"


async def test_image_current_followup_and_unrelated_switch_never_automatically_reuse(
    system,
) -> None:
    image = await picture(system)
    assert (await system["pipeline"].process_once()).status is PipelineStatus.COMPLETED
    assert all(
        sum(len(m.images) for m in request.messages) == 1 for request in system["model"].requests
    )
    system["model"].classify = lambda request: ContextDecision(
        mode="related",
        relation="followup",
        selected_candidate_ids=(
            json.loads(request.messages[-1].content)["candidates"][0]["candidate_id"],
        ),
        image_source=ref(image.message_id, "image"),
        needs_original_image=True,
    ).model_dump_json()
    await ingest(system, msg("followup", "刚才图片里是什么？"))
    assert (await system["pipeline"].process_once()).status is PipelineStatus.COMPLETED
    assert not any(m.images for m in system["model"].requests[-2].messages)
    assert sum(len(m.images) for m in system["model"].requests[-1].messages) == 1
    system["model"].classify = standalone
    await ingest(system, msg("switch", "Python list sorting?"))
    assert (await system["pipeline"].process_once()).status is PipelineStatus.COMPLETED
    assert not any(m.images for request in system["model"].requests[-2:] for m in request.messages)


async def test_expired_image_can_only_use_labeled_historical_description(system) -> None:
    await picture(system)
    await system["pipeline"].process_once()
    await system["archive"].put_observation(
        Observation(
            observation_id="description",
            source=ref("image", "image"),
            kind="image_description",
            status="available",
            text="历史描述：一个红杯子",
            extractor_version="synthetic/v1",
            created_at=NOW,
        )
    )
    # Preserve the record, simulate expiry through the resolver's configured lifetime.
    system["images"].retention_days = -1

    def choose(request):
        candidates = json.loads(request.messages[-1].content)["candidates"]
        chosen = next(candidate for candidate in candidates if candidate["observation_ids"])
        return ContextDecision(
            mode="related",
            relation="followup",
            selected_candidate_ids=(chosen["candidate_id"],),
            image_source=ref("image", "image"),
            needs_original_image=True,
        ).model_dump_json()

    system["model"].classify = choose
    await ingest(system, msg("followup", "刚才图片中的红杯子呢？"))
    assert (await system["pipeline"].process_once()).status is PipelineStatus.COMPLETED
    bundle = system["flow"].last_bundle
    assert bundle.decision.image_source is None and bundle.unavailable_sources == (
        ref("image", "image"),
    )
    assert "历史描述" in system["model"].requests[-1].messages[-1].content
    assert not any(m.images for m in system["model"].requests[-1].messages)


async def test_image_tamper_or_foreign_attachment_cannot_be_uploaded(system) -> None:
    image = await picture(system)
    source = ref("image", "image")
    verified = await system["images"].resolve(current=image, source=source)
    assert verified is not None
    path = system["attachments"]._objects_path(verified.sha256)
    path.write_bytes(b"tampered")
    assert await system["images"].resolve(current=image, source=source) is None
    assert (
        await system["images"].resolve(
            current=image,
            source=source.model_copy(
                update={"scope": SCOPE.model_copy(update={"account_id": "other"})}
            ),
        )
        is None
    )


async def test_image_selected_without_candidate_or_other_new_image_is_refused(system) -> None:
    image = await picture(system)
    retrieval = await system["retriever"].retrieve(current=msg("query", "图片"))
    with pytest.raises(ContextValidationError, match="ImageNotSelected"):
        await system["flow"].assembler.assemble(
            current=msg("query", "图片"),
            retrieval=retrieval,
            decision=ContextDecision(
                mode="standalone",
                relation="none",
                needs_original_image=True,
                image_source=ref(image.message_id, "image"),
            ),
        )


async def test_policy_and_pause_block_classification_before_any_paid_call(system) -> None:
    await ingest(system, msg("query"))
    await system["repository"].set_paused(channel="fake", paused=True)
    assert (await system["pipeline"].process_once()).status is PipelineStatus.SKIPPED
    assert not system["model"].requests


async def test_budget_fits_one_call_but_not_two_no_model_is_called(system) -> None:
    system["budget"]._limits = BudgetLimits(daily=Decimal("0.01"), monthly=Decimal("100"))
    await ingest(system, msg("query"))
    assert (await system["pipeline"].process_once()).status is PipelineStatus.DEAD
    assert not system["model"].requests
    assert (await system["budget"].snapshot()).daily_reserved == 0


async def test_uncertain_classifier_call_stops_answer_and_survives_restart_without_retry(
    system,
) -> None:
    system["model"].fail = True
    await ingest(system, msg("query"))
    assert (await system["pipeline"].process_once()).status is PipelineStatus.DEAD
    assert len(system["model"].requests) == 1
    assert (await system["pipeline"].process_once()).status is PipelineStatus.IDLE
    assert (await system["budget"].snapshot()).daily_spent == Decimal("0.008704")
    assert (await system["budget"].snapshot()).daily_reserved == 0
    recovered = await PersistentBudgetManager.create(
        database_path=system["archive"].path,
        limits=BudgetLimits(daily=Decimal("5"), monthly=Decimal("100")),
        prices={("deepseek", "deepseek-flash"): ModelPrice(Decimal("1"), Decimal("1"))},
    )
    assert (await recovered.snapshot()).daily_spent == Decimal("0.008704")


class ReadProbe:
    def __init__(self) -> None:
        self.calls = 0

    def manifest(self):
        return ToolManifest(
            name="web.read",
            description="synthetic read-only web",
            action_kind="browse_public_https",
            input_schema={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        )

    async def invoke(self, context, arguments):
        self.calls += 1
        return ToolResult(ok=True, content="synthetic page")


async def test_historical_url_cannot_grant_answer_tool_permission(system) -> None:
    await system["archive"].put_message(msg("old", "https://example.org/historical"))
    system["model"].classify = related
    probe = ReadProbe()
    system["pipeline"].tools = {"web.read": probe}
    count = 0

    def answer(request):
        nonlocal count
        count += 1
        return (
            ModelResponse(
                content=None,
                model="deepseek-flash",
                tool_calls=(
                    ToolCall(
                        call_id="read-old",
                        name="web.read",
                        arguments={"url": "https://example.org/historical"},
                    ),
                ),
            )
            if count == 1
            else ModelResponse(content="done", model="deepseek-flash")
        )

    system["model"].answer = answer
    await ingest(system, msg("query", "讨论之前的链接。"))
    assert (await system["pipeline"].process_once()).status is PipelineStatus.COMPLETED
    assert probe.calls == 0 and len(system["model"].requests) == 3
    assert not system["model"].requests[0].tools
    assert "only an HTTPS URL" in " ".join(
        m.content or "" for m in system["model"].requests[-1].messages
    )


async def test_unknown_send_uses_existing_outbox_and_never_regenerates(system) -> None:
    await ingest(system, msg("query"))
    assert (await system["pipeline"].process_once()).status is PipelineStatus.COMPLETED
    connector = FakeConnector()
    connector.raise_after_accept = True
    assert (
        await system["pipeline"].dispatch_once(connector, channel="fake")
    ).status is PipelineStatus.UNKNOWN
    assert (
        await system["pipeline"].dispatch_once(connector, channel="fake")
    ).status is PipelineStatus.IDLE
    assert (await system["pipeline"].process_once()).status is PipelineStatus.IDLE
    assert len(connector.delivered) == 1 and len(system["model"].requests) == 2


async def test_supplied_link_read_becomes_verified_followup_evidence(system) -> None:
    url = "https://example.org/current"
    probe = ReadProbe()
    system["pipeline"].tools = {"web.read": probe}
    turns = 0

    def answer(request):
        nonlocal turns
        turns += 1
        return (
            ModelResponse(
                model="deepseek-flash",
                tool_calls=(ToolCall(call_id="read", name="web.read", arguments={"url": url}),),
            )
            if turns == 1
            else ModelResponse(content="synthetic answer", model="deepseek-flash")
        )

    system["model"].answer = answer
    await ingest(
        system,
        msg("link", parts=(MessagePart(part_id="link", kind="link", url=url),)),
    )
    assert (await system["pipeline"].process_once()).status is PipelineStatus.COMPLETED
    observed = await system["archive"].get_observations(ref("link", "link"))
    assert len(observed) == 1 and observed[0].text == "synthetic page"

    def choose(request):
        candidates = json.loads(request.messages[-1].content)["candidates"]
        selected = next(c for c in candidates if c["observation_ids"])
        return ContextDecision(
            mode="related", relation="followup", selected_candidate_ids=(selected["candidate_id"],)
        ).model_dump_json()

    system["model"].classify = choose
    await ingest(system, msg("followup", "刚才网页内容是什么？"))
    assert (await system["pipeline"].process_once()).status is PipelineStatus.COMPLETED
    assert system["flow"].last_bundle.observations == observed
    assert "synthetic page" in " ".join(
        m.content or "" for m in system["model"].requests[-1].messages
    )
    assert probe.calls == 1


@pytest.mark.parametrize("quotes", [("“", "”"), ("‘", "’"), ("'", "'"), ("", "")])
async def test_long_page_exact_passage_followup_keeps_excerpt_not_entire_body(
    system, quotes
) -> None:
    from lemonbot.orchestration.prompts import CAPABILITY_SYSTEM_PROMPT

    scope = SCOPE.model_copy(update={
        "account_id": "account_" + "a" * 64,
        "conversation_id": "chat_" + "b" * 64,
    })
    system["flow"].scopes = {scope.conversation_id: scope}
    system["flow"].system_prompt = CAPABILITY_SYSTEM_PROMPT
    await system["repository"].set_allowlisted(scope.channel, scope.conversation_id)

    def scoped_msg(identifier, text="", **changes):
        return msg(identifier.ljust(70, "x"), text, scope=scope,
                   sender_id="sender_" + "d" * 64, **changes)

    def scoped_ref(identifier, part):
        return SourceRef(scope=scope, message_id=identifier.ljust(70, "x"), part_id=part)

    phrase = "合成植物用坚韧花瓣保护自己"
    continuation = "下一句是它会散发清新的香气。"
    body = "无关导航文字" * 1000 + phrase + "。" + continuation + "其他页面内容" * 2000
    await system["archive"].put_message(
        scoped_msg("long-page", parts=(MessagePart(part_id="link", kind="link", url="https://example.org/page"),))
    )
    observation = Observation(
        observation_id="full-page",
        source=scoped_ref("long-page", "link"),
        kind="page_body",
        status="available",
        text=body,
        extractor_version="context-web-read/v1",
        created_at=NOW,
    )
    await system["archive"].put_observation(observation)
    for i in range(16):
        await system["archive"].put_message(
            scoped_msg(f"summary-{i}", f"关于{phrase}的提问：没有保存的正文，无法回答。")
        )

    def choose(request):
        candidates = json.loads(request.messages[-1].content)["candidates"]
        chosen = candidates[0]
        assert phrase in chosen["summary"] and continuation in chosen["summary"]
        assert chosen["observation_ids"] == ["full-page"]
        return ContextDecision(
            mode="related", relation="followup", selected_candidate_ids=(chosen["candidate_id"],)
        ).model_dump_json()

    system["model"].classify = choose
    await ingest(
        system,
        scoped_msg("page-question", f"页面中，{quotes[0]}{phrase}{quotes[1]}，下一句是什么？"),
    )
    assert (await system["pipeline"].process_once()).status is PipelineStatus.COMPLETED
    assert system["flow"].last_fallback is None
    rendered = " ".join(m.content or "" for m in system["model"].requests[-1].messages)
    assert continuation in rendered and body not in rendered
    assert system["flow"].last_bundle.observations == (observation,)
    saved = await system["archive"].get_observations(scoped_ref("long-page", "link"))
    assert saved[0].text == body
    assert len(system["model"].requests) == 2


@pytest.mark.parametrize("ok", [True, False])
async def test_read_observation_idempotence_failure_and_exact_url_scope(system, ok) -> None:
    url = "https://example.org/supplied"
    event = await ingest(
        system,
        msg("card", parts=(MessagePart(part_id="card", kind="card", title="card", url=url),)),
    )
    result = ToolResult(
        ok=ok,
        content="synthetic page" if ok else "verification required",
        error_code=None if ok else "login_required",
    )
    for _ in range(2):
        await system["flow"].observe_read(event, "same-read", {"url": url}, result)
    observed = await system["archive"].get_observations(ref("card", "card"))
    assert len(observed) == 1
    assert observed[0].status == ("available" if ok else "unavailable")
    assert observed[0].text == ("synthetic page" if ok else None)
    await system["flow"].observe_read(
        event,
        "search-derived",
        {"url": "https://example.org/other"},
        ToolResult(ok=True, content="other"),
    )
    assert await system["archive"].get_observations(ref("card", "card")) == observed


@pytest.mark.parametrize("accepted", [True, False])
async def test_model_observation_only_accepted_with_worker_image_attestation(
    system, accepted
) -> None:
    await picture(system)
    system["model"].classify = lambda request: ContextDecision(
        mode="standalone", relation="none", current_observation="synthetic observed cup"
    ).model_dump_json()
    system["model"].class_image_accepted = accepted
    assert (await system["pipeline"].process_once()).status is PipelineStatus.COMPLETED
    assert bool(system["flow"].last_bundle.observations) is accepted


async def test_overlarge_original_is_not_truncated_and_no_cloud_call_occurs(system) -> None:
    await ingest(system, msg("query", "原始正文" * 4000))
    assert (await system["pipeline"].process_once()).status is PipelineStatus.DEAD
    assert not system["model"].requests


def test_json_parser_rejects_markdown_and_duplicate_ids() -> None:
    with pytest.raises(ContextValidationError):
        parse_decision('```json\n{"mode":"standalone","relation":"none"}\n```')


async def test_genuine_quote_uses_exact_original_image_without_archiving_an_extra_reply(
    system,
) -> None:
    original = msg(
        "ordinary-group-image",
        parts=(MessagePart(part_id="image", kind="image", unavailable_reason="not_loaded"),),
    )
    await system["archive"].put_message(original)
    assert (await system["pipeline"].process_once()).status is PipelineStatus.IDLE
    attachment = await system["attachments"].ingest(
        channel=SCOPE.channel,
        chat_id=SCOPE.conversation_id,
        event_id="quote",
        content=b"synthetic quoted image",
        media_type="image/png",
    )
    current = msg("quote", "这个图片中的内容呢？", references=(ref(original.message_id, "image"),))
    await ingest(
        system,
        current,
        image_source="validated_wal_snapshot",
        image_source_ref=original.message_id,
        attachment_ids=[str(attachment.attachment_id)],
    )

    def choose(request):
        candidates = json.loads(request.messages[-1].content)["candidates"]
        selected = next(
            candidate
            for candidate in candidates
            if candidate["sources"][0]["message_id"] == original.message_id
        )
        return ContextDecision(
            mode="related",
            relation="quote",
            selected_candidate_ids=(selected["candidate_id"],),
            image_source=ref(original.message_id, "image"),
            needs_original_image=True,
        ).model_dump_json()

    system["model"].classify = choose
    assert (await system["pipeline"].process_once()).status is PipelineStatus.COMPLETED
    assert len(system["model"].requests) == 2
    assert all(
        sum(len(m.images) for m in request.messages) == 1 for request in system["model"].requests
    )
    assert await system["archive"].get_message(ref(original.message_id, "image")) == original


async def test_selected_observation_expiring_during_classification_is_not_answer_context(
    system,
) -> None:
    import sqlite3

    await system["archive"].put_message(
        msg(
            "card",
            parts=(MessagePart(part_id="card", kind="card", url="https://example.org/source"),),
        )
    )
    await system["archive"].put_observation(
        Observation(
            observation_id="body",
            source=ref("card", "card"),
            kind="page_body",
            status="available",
            text="unique observation body",
            extractor_version="test/v1",
            created_at=NOW - timedelta(hours=2),
            expires_at=NOW + timedelta(hours=2),
        )
    )

    def choose(request):
        candidates = json.loads(request.messages[-1].content)["candidates"]
        selected = next(candidate for candidate in candidates if candidate["observation_ids"])
        with sqlite3.connect(system["archive"].path) as connection:
            connection.execute(
                "UPDATE context_archive_observations SET payload_json=json_set("
                "payload_json,'$.expires_at',?)",
                ((NOW - timedelta(hours=1)).isoformat(),),
            )
        return ContextDecision(
            mode="related", relation="followup", selected_candidate_ids=(selected["candidate_id"],)
        ).model_dump_json()

    system["model"].classify = choose
    await ingest(system, msg("query", "之前的 unique observation body 呢？"))
    assert (await system["pipeline"].process_once()).status is PipelineStatus.COMPLETED
    assert system["flow"].last_bundle.decision.mode == "clarify"
    assert (
        "unique observation body"
        not in system["model"].requests[-1].messages[-1].content.split('"parts"')[0]
    )
    assert not system["flow"].last_bundle.observations


async def test_image_expiring_before_tool_continuation_is_removed(system) -> None:
    await picture(system)
    service = system["flow"]

    async def answer(messages, bundle):
        assert any(message.images for message in messages)
        system["images"].retention_days = -1
        refreshed = await service.refresh_images(bundle, messages)
        assert not any(message.images for message in refreshed)
        assert "expired" in refreshed[-1].content
        payload = json.loads(refreshed[-1].content.split("\n[Original image", 1)[0])
        assert payload["original_image_supplied"] is False
        assert payload["current"]["parts"][0]["unavailable_reason"] == "not_loaded"
        return "done"

    async def before():
        pass

    event = await system["repository"].inbound_event("fake", SCOPE.conversation_id, "image")
    assert await service.run(event, answer=answer, before_call=before) == "done"


async def test_incorrect_author_or_changed_source_excerpt_cannot_be_assembled(system) -> None:
    current = msg("query", "活动暗号")
    await system["archive"].put_message(msg("old", "活动暗号银杏"))
    result = await system["retriever"].retrieve(current=current)
    candidate = result.candidates[0]
    for fields in ({"author_ids": ("impostor",)}, {"summary": "modified or injected source"}):
        forged = result.model_copy(update={"candidates": (candidate.model_copy(update=fields),)})
        with pytest.raises(ContextValidationError, match="EvidenceChanged"):
            await system["flow"].assembler.assemble(
                current=current,
                retrieval=forged,
                decision=ContextDecision(
                    mode="related",
                    relation="followup",
                    selected_candidate_ids=(candidate.candidate_id,),
                ),
            )


async def test_clipped_classifier_does_not_accept_unoffered_but_existing_source(system) -> None:
    for index in range(8):
        await system["archive"].put_message(msg(f"old-{index}", "合成历史" * 200))
    current = msg("query", "讲一个笑话")
    all_candidates = await system["retriever"].retrieve(current=current)
    hidden = all_candidates.candidates[-1].candidate_id

    def choose(request):
        offered = json.loads(request.messages[-1].content)["candidates"]
        assert len(offered) < len(all_candidates.candidates)
        assert hidden not in {candidate["candidate_id"] for candidate in offered}
        return ContextDecision(
            mode="related", relation="topic", selected_candidate_ids=(hidden,)
        ).model_dump_json()

    system["model"].classify = choose
    await ingest(system, current)
    assert (await system["pipeline"].process_once()).status is PipelineStatus.COMPLETED
    assert system["flow"].last_fallback == "classification_invalid"
    assert not system["flow"].last_bundle.source_messages


async def test_runtime_assembly_default_off_and_enabled_graph_use_same_budget(
    system, tmp_path, monkeypatch
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from lemonbot.config.settings import AppSettings, load_settings
    from lemonbot.connectors.wechat_agent_conversation import ConversationMultiplexer
    from lemonbot.connectors.wechat_agent_runtime import RuntimeAgentWechatConnector
    from lemonbot.embeddings.artifacts import EmbeddingError
    from lemonbot.embeddings.proxy import IsolatedEmbeddingWorker
    from lemonbot.runtime import LemonbotRuntime
    from lemonbot.storage.migrate import upgrade_database

    factory = AsyncMock(side_effect=EmbeddingError("embedding_cuda_unavailable"))
    monkeypatch.setattr(IsolatedEmbeddingWorker, "create", factory)
    disabled = LemonbotRuntime(
        AppSettings.model_validate({"runtime": {"data_root": str(tmp_path)}})
    )
    assert await disabled._build_context_flow() is None
    factory.assert_not_called()
    await disabled.database.close()
    raw = load_settings(Path("config/wechat-agent-conversation.example.toml")).model_dump()
    raw["runtime"].update(connector="wechat_agent", data_root=str(tmp_path))
    raw["wechat_agent"]["enabled"] = True
    raw["models"]["provider"] = "deepseek"
    raw["context_memory"].update(enabled=True, archive_enabled=True)
    runtime = LemonbotRuntime(AppSettings.model_validate(raw))
    upgrade_database(runtime.paths.database)
    runtime.model, runtime._budget = system["model"], system["budget"]
    multiplexer = object.__new__(ConversationMultiplexer)
    multiplexer.children = {
        SCOPE.conversation_id: SimpleNamespace(
            binding=SimpleNamespace(account_ref=SCOPE.account_id, chat_ref=SCOPE.conversation_id),
            target=SimpleNamespace(is_group=True),
        )
    }
    connector = object.__new__(RuntimeAgentWechatConnector)
    connector.transport = multiplexer
    runtime.connector = connector
    runtime.paths.embedding_assets.mkdir(parents=True)
    flow = await runtime._build_context_flow()
    assert flow is not None and flow.budget is system["budget"] and flow.model is system["model"]
    assert flow.scopes[SCOPE.conversation_id].account_id == SCOPE.account_id
    assert flow.retriever.base.worker is None
    factory.assert_awaited_once()
    await runtime._context_flow_close()
    await runtime.database.close()
    raw["wechat_agent"]["memory_enabled"] = True
    with pytest.raises(ValueError, match="legacy memory disabled"):
        AppSettings.model_validate(raw)


# Stage 6: all identities, receipts and cloud results below are synthetic.
def enable_maintenance(system):
    from lemonbot.domain.models import OutboxState
    from lemonbot.memory.unified.maintenance import UnifiedMemoryStore
    from lemonbot.memory.unified.remember import MemoryContextRetriever

    store = UnifiedMemoryStore(system["archive"])
    system["store"] = store
    flow = system["flow"]
    flow.memories = store
    flow.assembler.memories = store
    flow.retriever = MemoryContextRetriever(system["retriever"], store)

    def observed(outbound):
        return (
            msg(
                "synthetic-self",
                outbound.text,
                sender_id="synthetic-self",
                direction="outbound",
                delivery_state=OutboxState.ACKNOWLEDGED,
                occurred_at=datetime.now(UTC),
                event_id=None,
            ),
            "fake:1",
        )

    flow.confirmed_source = observed
    return store


async def memory_entry(system, identifier="memory", source="old", **changes):
    from lemonbot.memory.unified.models import MemoryEntry

    return MemoryEntry.model_validate(
        {
            "memory_id": identifier,
            "scope": SCOPE,
            "kind": "fact",
            "text": "喜欢青柠",
            "sources": (ref(source),),
            "author_ids": ("alice",),
            "derivation_version": "fixture/v1",
            "created_at": datetime.now(UTC),
            **changes,
        }
    )


async def test_memory_versions_correction_immediately_changes_retrieval(system) -> None:
    store = enable_maintenance(system)
    await system["archive"].put_message(msg("old", "喜欢青柠"))
    entry = await memory_entry(system)
    assert await store.put_memory(entry)
    assert not await store.put_memory(entry)
    await system["archive"].put_message(msg("correction", "更正：喜欢薄荷"))
    corrected = await memory_entry(
        system, "new", "correction", text="喜欢薄荷", supersedes=("memory",)
    )
    assert await store.put_memory(corrected)
    assert not (await store.get_memory(scope=SCOPE, memory_id="memory")).active
    assert (await store.get_memory(scope=SCOPE, memory_id="new")).active
    assert (
        await store.get_memory(
            scope=SCOPE.model_copy(update={"account_id": "other"}), memory_id="new"
        )
        is None
    )
    result = await system["flow"].retriever.retrieve(
        current=msg("question", "喜欢什么？", occurred_at=datetime.now(UTC))
    )
    assert any(c.memory_ids == ("new",) for c in result.candidates)
    historical = [c for c in result.candidates if any(s.message_id == "old" for s in c.sources)]
    assert historical and all(c.corrections[0].latest == corrected for c in historical)
    bundle = await system["flow"].assembler.assemble(
        current=msg("question", occurred_at=datetime.now(UTC)),
        retrieval=result,
        decision=ContextDecision(
            mode="related",
            relation="followup",
            selected_candidate_ids=(
                next(c.candidate_id for c in result.candidates if c.memory_ids),
            ),
        ),
    )
    assert bundle.memories == (corrected,)


@pytest.mark.parametrize("fault", ["author", "missing", "cross_author_correction", "conflict"])
async def test_memory_sources_and_versions_fail_closed(system, fault) -> None:
    from lemonbot.memory.unified.archive import ArchiveError

    store = enable_maintenance(system)
    await system["archive"].put_message(msg("old"))
    original = await memory_entry(system)
    await store.put_memory(original)
    if fault == "author":
        entry = await memory_entry(system, "new", author_ids=("bob",))
    elif fault == "missing":
        entry = await memory_entry(system, "new", "missing")
    elif fault == "conflict":
        entry = original.model_copy(update={"text": "changed"})
    else:
        await system["archive"].put_message(msg("bob", sender_id="bob"))
        entry = await memory_entry(
            system, "new", "bob", author_ids=("bob",), supersedes=("memory",)
        )
    with pytest.raises(ArchiveError):
        await store.put_memory(entry)
    assert (await store.get_memory(scope=SCOPE, memory_id="memory")).active


async def test_expired_observation_invalidates_derived_memory_and_topic_cache(
    system, monkeypatch
) -> None:
    import lemonbot.memory.unified.maintenance as maintenance

    store = enable_maintenance(system)
    await system["archive"].put_message(
        msg("old", parts=(MessagePart(part_id="link", kind="link", url="https://example.org"),))
    )
    observation = Observation(
        observation_id="obs",
        source=ref("old", "link"),
        kind="page_body",
        status="available",
        text="synthetic",
        extractor_version="fixture/v1",
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    await system["archive"].put_observation(observation)
    await store.put_memory(await memory_entry(system, sources=(ref("old", "link"),)))
    before = await store.topics(SCOPE)

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(minutes=2)

    monkeypatch.setattr(maintenance, "datetime", Later)
    assert not (await store.get_memory(scope=SCOPE, memory_id="memory")).active
    assert await store.topics(SCOPE) == "[]"
    assert "memory" in before


@pytest.mark.parametrize("unknown", [False, True])
async def test_only_confirmed_reply_persists_image_observation_and_refreshes_index(
    system, unknown
) -> None:
    store = enable_maintenance(system)
    await picture(system)
    system["model"].classify = lambda request: ContextDecision(
        mode="standalone", relation="none", current_observation="synthetic image description"
    ).model_dump_json()
    assert (await system["pipeline"].process_once()).status is PipelineStatus.COMPLETED
    assert not await system["archive"].get_observations(ref("image", "image"))
    connector = FakeConnector()
    connector.raise_after_accept = unknown
    result = await system["pipeline"].dispatch_once(connector, channel="fake")
    assert result.status is (PipelineStatus.UNKNOWN if unknown else PipelineStatus.ACKNOWLEDGED)
    observations = await system["archive"].get_observations(ref("image", "image"))
    assert bool(observations) is not unknown
    assert (
        bool(await store.cached_content(await system["archive"].get_message(ref("image", "image"))))
        is not unknown
    )
    if not unknown:
        assert system["retriever"].last_sync is not None
        await system["flow"].confirmed(connector.delivered[0])
        assert await system["archive"].get_observations(ref("image", "image")) == observations
    assert len(system["model"].requests) == 2


async def test_confirmed_correction_uses_current_raw_text_not_model_paraphrase(system) -> None:
    store = enable_maintenance(system)
    await system["archive"].put_message(msg("old", "喜欢青柠"))
    system["model"].classify = lambda request: ContextDecision(
        mode="related",
        relation="correction",
        selected_candidate_ids=(
            next(
                c["candidate_id"]
                for c in json.loads(request.messages[-1].content)["candidates"]
                if c["sources"][0]["message_id"] == "old"
            ),
        ),
    ).model_dump_json()
    await ingest(system, msg("correct", "更正：我喜欢薄荷"))
    await system["pipeline"].process_once()
    assert not await store.list_memories(SCOPE)
    assert (
        await system["pipeline"].dispatch_once(FakeConnector(), channel="fake")
    ).status is PipelineStatus.ACKNOWLEDGED
    entries = await store.list_memories(SCOPE)
    assert len(entries) == 2
    assert [e.text for e in entries if e.active] == ["更正：我喜欢薄荷"]
    assert [e.text for e in entries if not e.active] == ["喜欢青柠"]


async def no_cloud_guard():
    return None


async def seed_consolidation(system, *, confirmed=True):
    from lemonbot.memory.unified.archive import scope_key

    store = enable_maintenance(system)
    for i in range(20):
        await system["archive"].put_message(msg(f"fixture-{i:02}", f"合成事实{i}"))
    if confirmed:
        async with system["archive"]._write() as db:
            await db.execute(
                "INSERT INTO unified_receipts VALUES (?,?,?)",
                ("synthetic-ack", scope_key(SCOPE), datetime.now(UTC).isoformat()),
            )
    system["model"].answer = lambda request: ModelResponse(
        model="deepseek-flash",
        content=json.dumps(
            {
                "entries": [
                    {
                        "kind": "fact",
                        "text": "合成事实0",
                        "source_ids": ["fixture-00"],
                        "supersedes": [],
                    }
                ]
            }
        ),
    )
    return store


@pytest.mark.parametrize("confirmed", [False, True])
async def test_idle_consolidation_threshold_and_at_most_once_across_restart(
    system, confirmed
) -> None:
    from lemonbot.memory.unified.consolidation import MemoryConsolidator
    from lemonbot.memory.unified.maintenance import UnifiedMemoryStore

    store = await seed_consolidation(system, confirmed=confirmed)
    consolidator = MemoryConsolidator(store, system["model"], system["budget"])
    assert await consolidator.run_once(SCOPE, before_call=no_cloud_guard) is confirmed
    restarted = MemoryConsolidator(
        UnifiedMemoryStore(system["archive"]), system["model"], system["budget"]
    )
    assert not await restarted.run_once(SCOPE, before_call=no_cloud_guard)
    assert len(system["model"].requests) == int(confirmed)
    if confirmed:
        serialized = system["model"].requests[0].messages[-1].content
        assert serialized.index('"memory_catalog"') < serialized.index('"current_time"')
        assert serialized.index('"current_time"') < serialized.index('"messages"')
        assert len(await store.list_memories(SCOPE)) == 1
        assert len(await store.processed(SCOPE)) == 20
        assert (await system["budget"].snapshot()).daily_reserved == 0


@pytest.mark.parametrize("invalid", ["timeout", "bad_json", "fake_source", "paused"])
async def test_consolidation_failure_is_durable_and_never_paid_retried(system, invalid) -> None:
    from lemonbot.memory.unified.consolidation import MemoryConsolidator

    store = await seed_consolidation(system)
    if invalid == "timeout":
        system["model"].fail = True
    elif invalid == "bad_json":
        system["model"].answer = lambda request: ModelResponse(
            model="deepseek-flash", content="broken JSON"
        )
    elif invalid == "fake_source":
        system["model"].answer = lambda request: ModelResponse(
            model="deepseek-flash",
            content=json.dumps(
                {
                    "entries": [
                        {"kind": "fact", "text": "invented", "source_ids": ["other-private-chat"]}
                    ]
                }
            ),
        )

    async def guard():
        if invalid == "paused":
            raise ValueError("paused")

    service = MemoryConsolidator(store, system["model"], system["budget"])
    with pytest.raises((ValueError, ModelTransportError)):
        await service.run_once(SCOPE, before_call=guard)
    assert not await service.run_once(SCOPE, before_call=no_cloud_guard)
    assert not await store.list_memories(SCOPE)
    assert len(system["model"].requests) == (0 if invalid == "paused" else 1)
    async with system["archive"]._read() as db:
        async with db.execute("SELECT state,charged FROM unified_jobs") as cursor:
            rows = await cursor.fetchall()
    assert rows[0][0] == "unknown" and Decimal(rows[0][1]) == Decimal("0.008704")


async def test_consolidation_daily_cap_and_hourly_limit_are_global_and_persistent(system) -> None:
    from lemonbot.memory.unified.maintenance import UnifiedMemoryStore

    store = enable_maintenance(system)
    now = datetime.now(UTC)
    args = {
        "now": now,
        "reserve": Decimal("0.49"),
        "daily_cap": Decimal("0.50"),
        "timezone": "Asia/Shanghai",
    }
    assert await store.claim_job(SCOPE, ("one",), **args)
    assert not await store.claim_job(SCOPE, ("two",), **{**args, "reserve": Decimal("0.001")})
    restarted = UnifiedMemoryStore(system["archive"])
    other = SCOPE.model_copy(update={"conversation_id": "other"})
    assert not await restarted.claim_job(other, ("three",), **{**args, "reserve": Decimal("0.02")})
    assert await restarted.claim_job(other, ("four",), **{**args, "reserve": Decimal("0.01")})


async def test_usage_separates_classification_answer_tools_and_cache_tokens(system) -> None:
    store = enable_maintenance(system)
    system["pipeline"].tools = {"web.read": ReadProbe()}
    await ingest(system, msg("query", "Hello"))
    await system["pipeline"].process_once()
    async with system["archive"]._read() as db:
        async with db.execute(
            "SELECT stage,elapsed_ms,prompt_tokens FROM unified_stage_usage ORDER BY stage"
        ) as c:
            usage = await c.fetchall()
    assert {r[0] for r in usage} == {"answer", "classification"}
    assert all(r[1] >= 0 and r[2] > 0 for r in usage)
    await store.record_usage(
        "cache",
        "answer",
        12,
        ModelResponse(
            model="deepseek-flash", prompt_tokens=100, completion_tokens=10, cached_prompt_tokens=80
        ),
        system["budget"],
        outcome="known",
    )
    async with system["archive"]._read() as db:
        async with db.execute(
            "SELECT cached_tokens FROM unified_stage_usage WHERE operation_id='cache'"
        ) as c:
            assert (await c.fetchone())[0] == 80


@pytest.mark.parametrize("marker", ["<｜｜DSML｜｜ calls>", "<|DSML|invoke>"])
async def test_raw_tool_protocol_is_not_sent_executed_or_paid_retried(system, marker) -> None:
    system["model"].answer = lambda request: ModelResponse(
        model="deepseek-flash", content=marker + "untrusted tool payload"
    )
    await ingest(system, msg("protocol-error", "概括这个公开网页"))
    assert (await system["pipeline"].process_once()).status is PipelineStatus.COMPLETED
    connector = FakeConnector()
    assert (
        await system["pipeline"].dispatch_once(connector, channel="fake")
    ).status is PipelineStatus.ACKNOWLEDGED
    assert len(connector.delivered) == 1
    assert "DSML" not in connector.delivered[0].text
    assert "异常的工具调用格式" in connector.delivered[0].text
    assert len(system["model"].requests) == 2  # Classification and answer only.
    assert (await system["pipeline"].process_once()).status is PipelineStatus.IDLE
    assert len(system["model"].requests) == 2


def test_delivery_metadata_only_writes_fresh_classifier_observation() -> None:
    from lemonbot.memory.unified.delivery import delivery_metadata
    from lemonbot.memory.unified.models import ContextBundle

    fresh = Observation(
        observation_id="classifier-current-image",
        source=ref("image", "image"),
        kind="image_description",
        status="available",
        text="synthetic cup",
        extractor_version="context-classifier/v1",
        created_at=NOW,
    )
    historical = fresh.model_copy(update={"observation_id": "persisted-description"})
    bundle = ContextBundle(
        current=msg("image"),
        decision=ContextDecision(mode="standalone", relation="none"),
        observations=(historical, fresh),
    )
    assert delivery_metadata(bundle)["observations"] == [fresh.model_dump(mode="json")]
    assert len(bundle.observations) == 2  # Historical evidence remains in the answer context.


@pytest.mark.parametrize("legacy_history", [False, True])
async def test_ack_maintenance_recovers_locally_without_cloud_or_resending(
    system, monkeypatch, legacy_history
) -> None:
    from lemonbot.memory.unified import delivery

    store = enable_maintenance(system)
    await picture(system)
    system["model"].classify = lambda request: ContextDecision(
        mode="standalone", relation="none", current_observation="synthetic cup"
    ).model_dump_json()
    await system["pipeline"].process_once()
    original = delivery.confirm_delivery

    async def failed(*args):
        raise OSError("synthetic local disk fault after durable ACK")

    monkeypatch.setattr(delivery, "confirm_delivery", failed)
    connector = FakeConnector()
    assert (
        await system["pipeline"].dispatch_once(connector, channel="fake")
    ).status is PipelineStatus.ACKNOWLEDGED
    assert not await system["archive"].get_observations(ref("image", "image"))
    if legacy_history:
        historical = Observation(
            observation_id="previous-confirmed-description",
            source=ref("image", "image"),
            kind="image_description",
            status="available",
            text="previous synthetic cup description",
            extractor_version="context-classifier/v1",
            created_at=NOW,
        )
        await system["archive"].put_observation(historical)
        async with system["archive"]._write() as db:
            async with db.execute("SELECT message_id,metadata_json FROM outbox") as c:
                row = await c.fetchone()
            metadata = json.loads(row[1])
            metadata["unified_context"]["observations"].insert(
                0, historical.model_dump(mode="json")
            )
            await db.execute(
                "UPDATE outbox SET metadata_json=? WHERE message_id=?",
                (json.dumps(metadata), row[0]),
            )
    monkeypatch.setattr(delivery, "confirm_delivery", original)
    await system["flow"].recover_confirmed()
    await system["flow"].recover_confirmed()
    observations = await system["archive"].get_observations(ref("image", "image"))
    assert len(observations) == 1 + int(legacy_history)
    assert sum(o.text == "synthetic cup" for o in observations) == 1
    assert len(connector.delivered) == 1 and len(system["model"].requests) == 2
    async with store.archive._read() as db:
        async with db.execute("SELECT count(*) FROM unified_receipts") as c:
            assert (await c.fetchone())[0] == 1


async def test_content_cache_and_aggregate_stats_never_override_expired_source(
    system, monkeypatch
) -> None:
    import lemonbot.memory.unified.maintenance as maintenance
    from lemonbot.memory.unified.stats_cli import statistics

    store = enable_maintenance(system)
    current = msg(
        "page", parts=(MessagePart(part_id="link", kind="link", url="https://example.org"),)
    )
    await system["archive"].put_message(current)
    observation = Observation(
        observation_id="page-body",
        source=ref("page", "link"),
        kind="page_body",
        status="available",
        text="synthetic public content",
        extractor_version="extract/v1",
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    await system["archive"].put_observation(observation)
    await store.cache_content(observation)
    assert await store.cached_content(current) == (observation,)

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(minutes=2)

    monkeypatch.setattr(maintenance, "datetime", Later)
    assert await store.cached_content(current) == ()
    await store.record_usage(
        "stats",
        "answer",
        10,
        ModelResponse(
            model="deepseek-flash", prompt_tokens=100, completion_tokens=20, cached_prompt_tokens=75
        ),
        system["budget"],
        outcome="known",
    )
    report = statistics(store.archive.path)
    assert report["stages"]["answer"]["uncached_tokens"] == 25
    assert "synthetic public content" not in json.dumps(report)


async def test_consolidation_does_not_run_below_threshold_or_on_unknown_request(system) -> None:
    from lemonbot.memory.unified.consolidation import MemoryConsolidator

    store = await seed_consolidation(system)
    # Simulate an UNKNOWN answer for one of the 20 user sources without changing it.
    from lemonbot.domain.models import OutboundMessage

    await system["repository"].create_outbox(
        OutboundMessage(
            channel=SCOPE.channel,
            chat_id=SCOPE.conversation_id,
            text="synthetic",
            reply_to_event_id="fixture-00",
        )
    )
    service = MemoryConsolidator(store, system["model"], system["budget"])
    assert not await service.run_once(SCOPE, before_call=no_cloud_guard)
    assert not system["model"].requests
    assert not await store.processed(SCOPE)


def test_consolidation_default_off_and_bounds() -> None:
    from lemonbot.config.settings import ContextMemorySettings

    assert not ContextMemorySettings().consolidation_enabled
    with pytest.raises(ValueError, match="requires unified context"):
        ContextMemorySettings(consolidation_enabled=True)
    with pytest.raises(ValueError):
        ContextMemorySettings(consolidation_daily_cny=Decimal("0.51"))
    with pytest.raises(ValueError):
        ContextMemorySettings(consolidation_min_messages=19)


async def test_memory_semantic_candidates_use_existing_cuda_cache_boundary(system) -> None:
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from lemonbot.memory.unified.models import RetrievalResult
    from lemonbot.memory.unified.remember import MemoryContextRetriever
    from lemonbot.memory.unified.retrieval import RetrievalOptions

    store = enable_maintenance(system)
    await system["archive"].put_message(msg("old", "合成证据"))
    await store.put_memory(await memory_entry(system, text="苹果"))
    vector = (1.0,) + (0.0,) * 511
    base = SimpleNamespace(
        retrieve=AsyncMock(return_value=RetrievalResult(scope=SCOPE, index_version="synthetic")),
        options=RetrievalOptions(),
        worker=SimpleNamespace(close=AsyncMock()),
        _lock=asyncio.Lock(),
        _vector=AsyncMock(return_value=(vector, 1)),
    )
    result = await MemoryContextRetriever(base, store).retrieve(
        current=msg("query", "水果", occurred_at=datetime.now(UTC))
    )
    assert result.candidates[0].memory_ids == ("memory",)
    assert "semantic" in result.candidates[0].reasons
    assert base._vector.await_count == 2


@pytest.mark.parametrize("tamper", [False, True])
async def test_classifier_gets_verified_image_availability_not_stale_intake_label(system, tamper):
    image = await picture(system)
    await system["pipeline"].process_once()
    source = ref(image.message_id, "image")
    original = await system["archive"].get_message(source)
    if tamper:
        verified = await system["images"].resolve(current=image, source=source)
        system["attachments"]._objects_path(verified.sha256).write_bytes(b"changed")

    def classify(request):
        payload = json.loads(request.messages[-1].content)
        available = payload["verified_image_sources"]
        assert (source.model_dump(mode="json") in available) is not tamper
        assert not request.messages[-1].images  # Availability is not image upload.
        assert any(source.model_dump(mode="json") in c["sources"] for c in payload["candidates"])
        return standalone(request)

    system["model"].classify = classify
    await ingest(system, msg("detail", "图里左边是什么颜色？"))
    assert (await system["pipeline"].process_once()).status is PipelineStatus.COMPLETED
    assert await system["archive"].get_message(source) == original
    assert not any(m.images for m in system["model"].requests[-1].messages)
    assert "中文问题用中文" in system["model"].requests[-1].messages[0].content


async def test_supplied_current_image_hides_stale_marker_without_mutating_archive(system):
    original = await picture(system)
    assert (await system["pipeline"].process_once()).status is PipelineStatus.COMPLETED
    for request in system["model"].requests:
        payload = json.loads(request.messages[-1].content)
        assert payload["original_image_supplied"] is True
        part = payload["current"]["parts"][0]
        assert part["kind"] == "image" and "unavailable_reason" not in part
        assert request.messages[-1].images
    archived = await system["archive"].get_message(ref(original.message_id, "image"))
    assert archived == original
    assert archived.parts[0].unavailable_reason == "not_loaded"


async def test_budget_fallback_preserves_actual_image_supply_and_intake_archive(system):
    original = await picture(system)
    event = await system["repository"].inbound_event("fake", SCOPE.conversation_id, "image")
    flow = system["flow"]

    async def before():
        pass

    async def answer(messages, bundle):
        assert flow.last_fallback == "selected_context_exceeds_budget"
        assert messages[-1].images
        payload = json.loads(messages[-1].content)
        assert payload["original_image_supplied"] is True
        assert "unavailable_reason" not in payload["current"]["parts"][0]
        refreshed = await flow.refresh_images(bundle, messages)
        assert refreshed[-1].images
        assert json.loads(refreshed[-1].content)["original_image_supplied"] is True
        return "ok"

    assert await flow.run(
        event, answer=answer, before_call=before, answer_reserve_tokens=7000
    ) == "ok"
    assert await system["archive"].get_message(ref(original.message_id, "image")) == original
    assert len(system["model"].requests) == 1  # No automatic extra paid classifier call.


async def test_real_length_identifiers_image_quote_fits_with_tool_reserve(system):
    from lemonbot.orchestration.prompts import CAPABILITY_SYSTEM_PROMPT

    scope = SCOPE.model_copy(
        update={"account_id": "account_" + "a" * 64, "conversation_id": "chat_" + "b" * 64}
    )
    image_id, quote_id = "event_" + "c" * 64, "event_" + "d" * 64
    original = msg(
        image_id,
        scope=scope,
        sender_id="sender_" + "e" * 64,
        parts=(MessagePart(part_id="image", kind="image", unavailable_reason="not_loaded"),),
    )
    await system["archive"].put_message(original)
    attachment = await system["attachments"].ingest(
        channel=scope.channel,
        chat_id=scope.conversation_id,
        event_id=image_id,
        content=b"synthetic image",
        media_type="image/png",
    )
    await system["repository"].record_inbound(
        InboundEvent(
            channel=scope.channel,
            chat_id=scope.conversation_id,
            event_id=image_id,
            sender_id=original.sender_id,
            occurred_at=NOW,
            metadata={
                "attachment_ids": [str(attachment.attachment_id)],
                "image_source": "validated_wal_snapshot",
                "image_source_ref": image_id,
            },
        )
    )
    source = SourceRef(scope=scope, message_id=image_id, part_id="image")
    current = msg(
        quote_id,
        "图中右边是什么颜色？",
        scope=scope,
        sender_id=original.sender_id,
        references=(source,),
    )
    event = await ingest(system, current)
    flow = system["flow"]
    flow.scopes = {scope.conversation_id: scope}
    flow.system_prompt = CAPABILITY_SYSTEM_PROMPT

    def choose(request):
        payload = json.loads(request.messages[-1].content)
        return ContextDecision(
            mode="related",
            relation="quote",
            selected_candidate_ids=(payload["candidates"][0]["candidate_id"],),
            image_source=source,
            needs_original_image=True,
            current_observation="左蓝色方块，右红色圆形",
        ).model_dump_json()

    system["model"].classify = choose

    async def before():
        pass

    async def answer(messages, bundle):
        assert flow._fits(messages, 2000)
        assert bundle.decision.image_source == source
        assert bundle.observations
        assert messages[-1].images
        assert flow.last_fallback is None
        return "ok"

    assert (
        await flow.run(event, answer=answer, before_call=before, answer_reserve_tokens=2000) == "ok"
    )


async def test_optional_tool_schemas_cannot_evict_selected_image_or_history(system):
    from lemonbot.orchestration.prompts import CAPABILITY_SYSTEM_PROMPT

    await picture(system)
    await system["pipeline"].process_once()
    system["flow"].system_prompt = CAPABILITY_SYSTEM_PROMPT

    class LargeTool:
        def manifest(self):
            return ToolManifest(
                name="web.read",
                description="x" * 1900,
                input_schema={"type": "object", "properties": {}},
                action_kind="read",
            )

        async def invoke(self, *args):
            raise AssertionError("no tool execution in this test")

    system["pipeline"].tools = {"web.read": LargeTool()}
    system["pipeline"].config = system["pipeline"].config.model_copy(
        update={"tool_headroom_tokens": 1024, "max_context_tokens": 8192}
    )

    def choose(request):
        payload = json.loads(request.messages[-1].content)
        c = next(c for c in payload["candidates"] if c["sources"][0]["part_id"] == "image")
        return ContextDecision(
            mode="related",
            relation="quote",
            selected_candidate_ids=(c["candidate_id"],),
            image_source=ref("image", "image"),
            needs_original_image=True,
        ).model_dump_json()

    system["model"].classify = choose
    await ingest(system, msg("detail", "图中左边是什么？", references=(ref("image", "image"),)))
    assert (await system["pipeline"].process_once()).status is PipelineStatus.COMPLETED
    answer = system["model"].requests[-1]
    assert any(m.images for m in answer.messages)
    assert any("UNTRUSTED SELECTED HISTORY" in (m.content or "") for m in answer.messages)
    assert not answer.tools
    assert system["flow"].last_fallback is None
