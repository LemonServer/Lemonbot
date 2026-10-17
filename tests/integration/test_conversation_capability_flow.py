from __future__ import annotations

import io
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from lemonbot.domain import (
    DataClass,
    InboundEvent,
    ModelCapabilities,
    ModelResponse,
    ToolCall,
    ToolContext,
    ToolManifest,
    ToolResult,
)
from lemonbot.orchestration import EventPipeline, FakeModelBackend, PipelineConfig, PipelineStatus
from lemonbot.orchestration.prompts import CAPABILITY_SYSTEM_PROMPT
from lemonbot.policy import DeterministicPolicy, PolicyConfig
from lemonbot.storage import CoreRepository, Database
from lemonbot.tools.attachments import AttachmentStore


class ConservativeModel(FakeModelBackend):
    def count_tokens(self, messages: Sequence[object]) -> int:
        from lemonbot.models.worker_proxy import IsolatedModelBackend

        # Exercise the production byte/framing/image estimator without a worker,
        # network access or a provider credential.
        return IsolatedModelBackend.count_tokens(self, messages)

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(tools=True, vision=True, context_tokens=8192)


async def test_image_followup_is_scoped_persistent_and_expiring(tmp_path: Path) -> None:
    path = tmp_path / "core.db"
    db = Database.from_path(path)
    await db.initialise()
    repo = CoreRepository(db)
    await repo.set_allowlisted("fake", "chat")
    store = AttachmentStore(path, tmp_path / "objects", minimum_free_bytes=0)
    await store.initialize()
    stream = io.BytesIO()
    Image.new("RGB", (10, 10), "red").save(stream, format="PNG")
    attachment = await store.ingest(
        channel="fake",
        chat_id="chat",
        event_id="image",
        content=stream.getvalue(),
        media_type="image/png",
    )
    model = FakeModelBackend(["seen", "other speaker", "same picture", "expired"])
    pipeline = EventPipeline(
        repo,
        DeterministicPolicy(
            repo,
            PolicyConfig(
                quiet_enabled=False,
                fallback=PolicyConfig().fallback.model_copy(
                    update={
                        "reply_per_10_minutes": 0,
                        "reply_per_hour": 0,
                        "reply_per_day": 100,
                        "global_per_day": 100,
                    }
                ),
            ),
        ),
        model,
        attachments=store,
        config=PipelineConfig(images_enabled=True),
    )

    async def turn(event_id: str, scope: str, extra: dict[str, Any] | None = None) -> None:
        event = InboundEvent(
            channel="fake",
            event_id=event_id,
            chat_id="chat",
            sender_id=scope,
            text="Question",
            metadata={
                "wechat_mode": "conversation",
                "context_scope": "context_" + scope * 64,
                **(extra or {}),
            },
        )
        await pipeline.ingest(event)
        assert (await pipeline.process_once()).status is PipelineStatus.COMPLETED

    try:
        await turn("image", "a", {"attachment_ids": [str(attachment.attachment_id)]})
        assert model.requests[-1].messages[-1].images[0].sha256 == attachment.sha256
        await turn("other", "b")
        assert not model.requests[-1].messages[-1].images
        # Rebuild the pipeline from stored context, with no in-memory attachment selection.
        pipeline = EventPipeline(
            repo,
            DeterministicPolicy(
                repo,
                PolicyConfig(
                    quiet_enabled=False,
                    fallback=PolicyConfig().fallback.model_copy(
                        update={
                            "reply_per_10_minutes": 0,
                            "reply_per_hour": 0,
                            "reply_per_day": 100,
                            "global_per_day": 100,
                        }
                    ),
                ),
            ),
            model,
            attachments=store,
            config=PipelineConfig(images_enabled=True),
        )
        await turn("follow", "a")
        assert model.requests[-1].messages[-1].images[0].sha256 == attachment.sha256
        with sqlite3.connect(path) as sql:
            sql.execute(
                "UPDATE attachments SET created_at=?",
                ((datetime.now(UTC) - timedelta(days=8)).isoformat(),),
            )
        await turn("expired", "a")
        assert not model.requests[-1].messages[-1].images
        assert "expired" in model.requests[-1].messages[-1].content
        await turn("missing-quote", "a", {"image_unavailable": True})
        assert not model.requests[-1].messages[-1].images
    finally:
        await db.close()


class ReadProbe:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[dict[str, Any]] = []

    def manifest(self) -> ToolManifest:
        search = self.name == "web.search"
        return ToolManifest(
            name=self.name,
            description="Synthetic read-only tool",
            input_schema={"type": "object"},
            action_kind="search_public_web" if search else "browse_public_https",
            allowed_data=frozenset({DataClass.PUBLIC, DataClass.CONVERSATION}),
        )

    async def invoke(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        self.calls.append(arguments)
        return ToolResult(
            ok=True,
            content="Page evidence https://example.com/result",
            metadata={"result_urls": ["https://example.com/result"]},
        )


class UnavailableReadProbe(ReadProbe):
    async def invoke(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        self.calls.append(arguments)
        return ToolResult(
            ok=False,
            error_code="page_unavailable",
            content="The public page requires login. "
            + "Synthetic unavailable-page details. " * 40,
        )


async def test_mandatory_oversized_tool_protocol_is_not_truncated_or_retried(
    tmp_path: Path,
) -> None:
    db = Database.from_path(tmp_path / "mandatory.db")
    await db.initialise()
    repo = CoreRepository(db)
    await repo.set_allowlisted("fake", "chat")
    url = "https://example.com/article"
    response = ModelResponse(
        reasoning_content="r" * 9000,
        tool_calls=(ToolCall(call_id="mandatory", name="web.read", arguments={"url": url}),),
    )
    model = ConservativeModel([response, "Must never be requested."])
    read = UnavailableReadProbe("web.read")
    pipeline = EventPipeline(
        repo,
        DeterministicPolicy(repo, PolicyConfig(quiet_enabled=False)),
        model,
        tools={"web.read": read},
        config=PipelineConfig(
            system_prompt=CAPABILITY_SYSTEM_PROMPT,
            max_context_tokens=8192,
            model_max_tokens=512,
            tool_headroom_tokens=1024,
            granted_tool_scopes=frozenset({"browser.read_public"}),
        ),
    )
    try:
        await pipeline.ingest(
            InboundEvent(
                channel="fake",
                chat_id="chat",
                sender_id="a",
                event_id="oversized",
                text=url,
                metadata={"wechat_mode": "conversation", "context_scope": "context_" + "a" * 64},
            )
        )
        result = await pipeline.process_once("fake")
        assert result.status is PipelineStatus.DEAD
        assert result.detail == "tool protocol messages exceed model context"
        assert len(model.requests) == 1
        assert (await pipeline.process_once("fake")).status is PipelineStatus.IDLE
        assert await repo.reserve_next_outbox() is None
    finally:
        await db.close()


@pytest.mark.parametrize("image", [False, True])
async def test_tool_continuation_fits_after_long_history_and_unavailable_pages(
    tmp_path: Path,
    image: bool,
) -> None:
    db = Database.from_path(tmp_path / "tool-pressure.db")
    await db.initialise()
    repo = CoreRepository(db)
    await repo.set_allowlisted("fake", "chat")
    scope = "context_" + "a" * 64
    metadata: dict[str, Any] = {"wechat_mode": "conversation", "context_scope": scope}
    store = AttachmentStore(
        tmp_path / "tool-pressure.db", tmp_path / "objects", minimum_free_bytes=0
    )
    await store.initialize()
    if image:
        stream = io.BytesIO()
        Image.new("RGB", (10, 10), "blue").save(stream, format="PNG")
        attachment = await store.ingest(
            channel="fake",
            chat_id="chat",
            event_id="current",
            content=stream.getvalue(),
            media_type="image/png",
        )
        metadata["attachment_ids"] = [str(attachment.attachment_id)]
    url = "https://example.com/article"
    responses = [
        ModelResponse(
            content="Synthetic explanation. " + "x" * 900,
            reasoning_content="r" * 200,
            tool_calls=(ToolCall(call_id=f"read-{i}", name="web.read", arguments={"url": url}),),
        )
        for i in range(2)
    ]
    model = ConservativeModel(
        [*responses, "Only the card title is available; its body requires login."]
    )
    read = UnavailableReadProbe("web.read")
    pipeline = EventPipeline(
        repo,
        DeterministicPolicy(repo, PolicyConfig(quiet_enabled=False)),
        model,
        attachments=store,
        tools={"web.read": read},
        config=PipelineConfig(
            system_prompt=CAPABILITY_SYSTEM_PROMPT,
            max_context_tokens=8192,
            model_max_tokens=512,
            images_enabled=image,
            tool_headroom_tokens=1024,
            tool_result_bytes=2000,
            max_tool_calls=5,
            max_navigations=2,
            granted_tool_scopes=frozenset({"browser.read_public"}),
        ),
    )
    try:
        for i in range(8):
            await repo.record_inbound(
                InboundEvent(
                    channel="fake",
                    chat_id="chat",
                    sender_id="a",
                    event_id=f"old-{i}",
                    text="Older conversation. " + "o" * 600,
                    occurred_at=datetime.now(UTC) - timedelta(minutes=10 - i),
                    metadata={"wechat_mode": "conversation", "context_scope": scope},
                )
            )
            item = await repo.claim_next_inbox("fake")
            assert item is not None
            await repo.complete_inbox(item.id)
        current = "Please read the public card body: " + url
        await pipeline.ingest(
            InboundEvent(
                channel="fake",
                chat_id="chat",
                sender_id="a",
                event_id="current",
                text=current,
                metadata=metadata,
            )
        )
        result = await pipeline.process_once("fake")
        assert result.status is PipelineStatus.COMPLETED, result.detail
        assert len(model.requests) == 3 and len(read.calls) == 2
        assert not model.requests[-1].tools  # Exhausted page reads are no longer offered.
        initial_old = [
            m for m in model.requests[0].messages if "Older conversation." in (m.content or "")
        ]
        final_old = [
            m for m in model.requests[-1].messages if "Older conversation." in (m.content or "")
        ]
        assert len(initial_old) > len(final_old)
        for request in model.requests:
            assert (
                model.count_tokens(request.messages) + pipeline._reserved_context_tokens() <= 8192
            )
            users = [m for m in request.messages if m.role.value == "user"]
            assert users[-1].content.startswith(current)
            assert bool(users[-1].images) is image
            calls = [call for m in request.messages for call in m.tool_calls]
            results = [m for m in request.messages if m.role.value == "tool"]
            assert [c.call_id for c in calls] == [m.tool_call_id for m in results]
            for response in responses:
                for message in request.messages:
                    if message.tool_calls == response.tool_calls:
                        assert message.content in (response.content, None)
                        assert message.reasoning_content == response.reasoning_content
        assert (await pipeline.process_once("fake")).status is PipelineStatus.IDLE
    finally:
        await db.close()


async def test_search_urls_are_authorized_only_in_current_turn_and_context_is_retained(
    tmp_path: Path,
) -> None:
    db = Database.from_path(tmp_path / "c.db")
    await db.initialise()
    repo = CoreRepository(db)
    await repo.set_allowlisted("fake", "chat")
    search, read = ReadProbe("web.search"), ReadProbe("web.read")
    model = FakeModelBackend(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(call_id="s", name="web.search", arguments={"query": "public"}),
                )
            ),
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        call_id="r",
                        name="web.read",
                        arguments={"url": "https://example.com/result"},
                    ),
                )
            ),
            "Answer",
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        call_id="r2",
                        name="web.read",
                        arguments={"url": "https://example.com/result"},
                    ),
                )
            ),
            "Followup",
        ]
    )
    pipeline = EventPipeline(
        repo,
        DeterministicPolicy(
            repo,
            PolicyConfig(
                quiet_enabled=False,
                fallback=PolicyConfig().fallback.model_copy(
                    update={
                        "reply_per_10_minutes": 0,
                        "reply_per_hour": 0,
                        "reply_per_day": 100,
                        "global_per_day": 100,
                    }
                ),
            ),
        ),
        model,
        tools={"web.search": search, "web.read": read},
        config=PipelineConfig(max_tool_calls=5),
    )
    try:
        for event_id in ["one", "two"]:
            await pipeline.ingest(
                InboundEvent(
                    channel="fake",
                    event_id=event_id,
                    chat_id="chat",
                    sender_id="a",
                    text="Question",
                    metadata={
                        "wechat_mode": "conversation",
                        "context_scope": "context_" + "a" * 64,
                    },
                )
            )
            assert (await pipeline.process_once()).status is PipelineStatus.COMPLETED
        assert len(search.calls) == 1 and len(read.calls) == 1
        assert any(
            "earlier tool observations" in (m.content or "") for m in model.requests[3].messages
        )
        history = await repo.recent_messages("fake", "chat", context_scope="context_" + "a" * 64)
        assert len(history[0].metadata["read_context"]) == 2
        assert "not" in (model.requests[-1].messages[-1].content or "")
    finally:
        await db.close()


async def test_search_time_page_chain_can_finish_without_optional_tool_schemas(
    tmp_path: Path,
) -> None:
    from lemonbot.domain import MessageRole
    from lemonbot.tools.clock import ClockTool

    db = Database.from_path(tmp_path / "chain.db")
    await db.initialise()
    repo = CoreRepository(db)
    await repo.set_allowlisted("fake", "chat")
    store = AttachmentStore(tmp_path / "chain.db", tmp_path / "objects", minimum_free_bytes=0)
    await store.initialize()
    stream = io.BytesIO()
    Image.new("RGB", (10, 10), "red").save(stream, format="PNG")
    image = await store.ingest(
        channel="fake",
        chat_id="chat",
        event_id="chain",
        content=stream.getvalue(),
        media_type="image/png",
    )
    url = "https://example.com/result"
    replies = [
        ModelResponse(
            reasoning_content="r" * 900,
            tool_calls=(
                ToolCall(call_id="search", name="web.search", arguments={"query": "public"}),
                ToolCall(call_id="time", name="clock.now", arguments={"timezone": "UTC"}),
            ),
        ),
        ModelResponse(
            reasoning_content="r" * 900,
            tool_calls=(ToolCall(call_id="read", name="web.read", arguments={"url": url}),),
        ),
    ]
    model = ConservativeModel([*replies, "Search snippets and time available; page unavailable."])
    pipeline = EventPipeline(
        repo,
        DeterministicPolicy(repo, PolicyConfig(quiet_enabled=False)),
        model,
        attachments=store,
        tools={
            "web.search": ReadProbe("web.search"),
            "clock.now": ClockTool(),
            "web.read": UnavailableReadProbe("web.read"),
        },
        config=PipelineConfig(
            system_prompt=CAPABILITY_SYSTEM_PROMPT,
            max_context_tokens=8192,
            model_max_tokens=512,
            images_enabled=True,
            tool_headroom_tokens=1024,
            tool_result_bytes=2000,
            max_tool_calls=5,
            max_navigations=2,
            granted_tool_scopes=frozenset({"web.search", "clock.read", "browser.read_public"}),
        ),
    )
    try:
        await pipeline.ingest(
            InboundEvent(
                channel="fake",
                event_id="chain",
                chat_id="chat",
                sender_id="a",
                text="Search public information, read the result and check UTC time.",
                metadata={
                    "wechat_mode": "conversation",
                    "context_scope": "context_" + "a" * 64,
                    "attachment_ids": [str(image.attachment_id)],
                },
            )
        )
        result = await pipeline.process_once("fake")
        assert result.status is PipelineStatus.COMPLETED, result.detail
        assert len(model.requests) == 3
        assert not model.requests[-1].tools
        for request in model.requests:
            assert (
                model.count_tokens(request.messages)
                + 512
                + pipeline._tool_schema_tokens(request.tools)
                <= 8192
            )
        messages = model.requests[-1].messages
        assert [m.reasoning_content for m in messages if m.tool_calls] == [
            r.reasoning_content for r in replies
        ]
        assert [m.tool_call_id for m in messages if m.role is MessageRole.TOOL] == [
            "search",
            "time",
            "read",
        ]
        assert next(m for m in reversed(messages) if m.role is MessageRole.USER).images
    finally:
        await db.close()


@pytest.mark.parametrize("authorized", [True, False])
async def test_share_card_reads_exact_current_url_before_model(
    tmp_path: Path, authorized: bool
) -> None:
    db = Database.from_path(tmp_path / "card.db")
    await db.initialise()
    repo = CoreRepository(db)
    await repo.set_allowlisted("fake", "chat")
    read = UnavailableReadProbe("web.read")
    url = "https://example.com/card"
    model = ConservativeModel(["The card title is available; the article body could not be read."])
    pipeline = EventPipeline(
        repo,
        DeterministicPolicy(repo, PolicyConfig(quiet_enabled=False)),
        model,
        tools={"web.read": read},
        config=PipelineConfig(
            system_prompt=CAPABILITY_SYSTEM_PROMPT,
            max_context_tokens=8192,
            model_max_tokens=512,
            tool_headroom_tokens=1024,
            granted_tool_scopes=frozenset({"browser.read_public"}),
        ),
    )
    try:
        await pipeline.ingest(
            InboundEvent(
                channel="fake",
                event_id="card",
                chat_id="chat",
                sender_id="a",
                text="Shared webpage: title\n" + url,
                metadata={
                    "wechat_mode": "conversation",
                    "context_scope": "context_" + "a" * 64,
                    "shared_link_url": url if authorized else "https://example.com/other",
                },
            )
        )
        assert (await pipeline.process_once("fake")).status is PipelineStatus.COMPLETED
        assert read.calls == ([{"url": url}] if authorized else [])
        content = model.requests[0].messages[-1].content
        assert ("shared-card read result" in content) is authorized
        if authorized:
            assert '"ok": false' in content and "page" in content
    finally:
        await db.close()
