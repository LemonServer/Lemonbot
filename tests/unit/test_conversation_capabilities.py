from __future__ import annotations

import io
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError

from lemonbot.domain.models import (
    MessageRole,
    ModelImage,
    ModelMessage,
    ModelRequest,
    ToolCall,
    ToolContext,
    ToolManifest,
)
from lemonbot.models import (
    BudgetLimits,
    BudgetManager,
    MappingSecretStore,
    ModelPrice,
    ProviderConfig,
)
from lemonbot.models.gateway import ModelProtocolError, OpenAICompatibleBackend
from lemonbot.models.image_input import ImageInputReader
from lemonbot.models.worker_protocol import ModelGenerateRequest
from lemonbot.tools.attachments import AttachmentStore
from lemonbot.tools.clock import ClockTool
from lemonbot.tools.object_store import ContentAddressedStore
from lemonbot.tools.search import SearchQuota, SearchTool


def image_bytes() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (40, 20), "red").save(stream, format="PNG")
    return stream.getvalue()


def backend(root: Path, client: httpx.AsyncClient | None = None) -> OpenAICompatibleBackend:
    return OpenAICompatibleBackend(
        config=ProviderConfig(
            provider="deepseek",
            base_url="https://api.deepseek.com",
            secret_name="deepseek_api_key",  # noqa: S106 -- lookup identifier
            flash_model="deepseek-flash",
            pro_model="deepseek-v4-pro",
            context_tokens=8192,
        ),
        secret_store=MappingSecretStore({"deepseek_api_key": "synthetic"}),
        budget=BudgetManager(
            limits=BudgetLimits(Decimal(5), Decimal(100)),
            prices={("deepseek", "deepseek-flash"): ModelPrice(Decimal(2), Decimal(8))},
        ),
        client=client,
        image_reader=ImageInputReader(root),
    )


async def test_native_image_wire_budget_and_small_ipc(tmp_path: Path) -> None:
    obj = ContentAddressedStore(tmp_path).put_bytes(image_bytes())
    plain = ModelMessage(role=MessageRole.USER, content="What color is it?")
    message = plain.model_copy(update={"images": (ModelImage(sha256=obj.sha256, size=obj.size),)})
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "red"}}],
                "usage": {"prompt_tokens": 1010, "completion_tokens": 3},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = backend(tmp_path, client)
        assert model.count_tokens((message,)) - model.count_tokens((plain,)) == 1024
        request = ModelRequest(messages=(message,), max_tokens=50)
        frame = ModelGenerateRequest(request=request).model_dump_json()
        assert len(frame) < 2048 and "base64" not in frame and str(tmp_path) not in frame
        response = await model.generate(request)
        assert response.content == "red" and len(seen) == 1
        blocks = seen[0]["messages"][0]["content"]
        assert blocks[0]["type"] == "text"
        assert blocks[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        assert blocks[1]["image_url"]["detail"] == "original"
        assert (await model._budget.snapshot()).daily_spent == Decimal("0.002044")
        with pytest.raises(ModelProtocolError):
            await model.generate(request.model_copy(update={"deep": True}))
        assert len(seen) == 1


def test_image_refs_reject_wrong_roles_symlinks_and_tampering(tmp_path: Path) -> None:
    obj = ContentAddressedStore(tmp_path).put_bytes(image_bytes())
    ref = ModelImage(sha256=obj.sha256, size=obj.size)
    with pytest.raises(ValidationError):
        ModelMessage(role=MessageRole.SYSTEM, content="x", images=(ref,))
    reader = ImageInputReader(tmp_path)
    obj.path.write_bytes(b"x" * obj.size)
    with pytest.raises(ValueError, match="integrity"):
        reader.data_url(ref)
    obj.path.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(image_bytes())
    obj.path.symlink_to(outside)
    with pytest.raises(ValueError, match="link"):
        reader.data_url(ref)


async def test_rejected_image_becomes_honest_text_before_single_post(tmp_path: Path) -> None:
    obj = ContentAddressedStore(tmp_path).put_bytes(b"not a valid image")
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Unavailable"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 3},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = backend(tmp_path, client)
        await model.generate(
            ModelRequest(
                messages=(
                    ModelMessage(
                        role=MessageRole.USER,
                        content="see",
                        images=(ModelImage(sha256=obj.sha256, size=obj.size),),
                    ),
                ),
                max_tokens=50,
            )
        )
    assert len(seen) == 1
    assert "IMAGE REJECTED" in seen[0]["messages"][0]["content"]
    assert "base64" not in json.dumps(seen)


async def test_retention_preserves_records_and_unexpired_shared_object(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path / "a.db", tmp_path / "objects", minimum_free_bytes=0)
    await store.initialize()
    old = await store.ingest(
        channel="c", chat_id="one", event_id="old", content=image_bytes(), media_type="image/png"
    )
    new = await store.ingest(
        channel="c", chat_id="one", event_id="new", content=image_bytes(), media_type="image/png"
    )
    with sqlite3.connect(tmp_path / "a.db") as db:
        db.execute(
            "UPDATE attachments SET created_at=? WHERE attachment_id=?",
            ((datetime.now(UTC) - timedelta(days=8)).isoformat(), str(old.attachment_id)),
        )
    await store.expire_images()
    with pytest.raises(ValueError):
        await store.resolve_bound(old.attachment_id, channel="c", chat_id="one", event_id="old")
    _, path = await store.resolve_bound(
        new.attachment_id, channel="c", chat_id="one", event_id="new"
    )
    assert path.exists()


async def test_local_search_is_bounded_and_quota_survives_restart(tmp_path: Path) -> None:
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "Python", "url": "https://docs.python.org/3/", "content": "docs"}
                ]
                * 8
            },
        )

    context = ToolContext(
        channel="c", chat_id="one", event_id="e", granted_scopes=frozenset({"web.search"})
    )
    quota = SearchQuota(tmp_path / "q.db", daily=1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        tool = SearchTool(endpoint="http://127.0.0.1:6177/search", quota=quota, client=client)
        result = await tool.invoke(context, {"query": "Python"})
        assert result.ok and len(json.loads(result.content)) == 5
        assert seen[0].url.params["format"] == "json"
        assert "authorization" not in seen[0].headers
        tool.quota = SearchQuota(tmp_path / "q.db", daily=1)
        assert (
            await tool.invoke(context, {"query": "again"})
        ).error_code == "search_quota_exceeded"
        assert len(seen) == 1
    with pytest.raises(ValueError):
        SearchTool(endpoint="http://example.com/search", quota=quota)


async def test_clock_timezones() -> None:
    tool = ClockTool()
    context = ToolContext(
        channel="c", chat_id="one", event_id="e", granted_scopes=frozenset({"clock.read"})
    )
    for zone in ["Asia/Shanghai", "UTC", "America/New_York"]:
        result = await tool.invoke(context, {"timezone": zone})
        assert result.ok and json.loads(result.content)["timezone"] == zone
    assert not (await tool.invoke(context, {"timezone": "missing/zone"})).ok


async def test_wire_tool_names_roundtrip_without_changing_broker_identity(tmp_path: Path) -> None:
    names = []

    def respond(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        name = data["tools"][0]["function"]["name"]
        names.append(name)
        assert "." not in name and len(name) <= 64
        if len(names) == 2:
            assert data["messages"][0]["tool_calls"][0]["function"]["name"] == name
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call",
                                    "type": "function",
                                    "function": {"name": name, "arguments": "{}"},
                                }
                            ]
                        }
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10},
            },
        )

    manifest = ToolManifest(
        name="clock.now",
        description="Clock",
        input_schema={"type": "object"},
        action_kind="read_time",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = backend(tmp_path, client)
        response = await model.generate(
            ModelRequest(
                messages=(ModelMessage(role=MessageRole.USER, content="time"),),
                tools=(manifest,),
                max_tokens=50,
            )
        )
        assert response.tool_calls[0].name == "clock.now"
        await model.generate(
            ModelRequest(
                messages=(
                    ModelMessage(
                        role=MessageRole.ASSISTANT,
                        tool_calls=(ToolCall(call_id="call", name="clock.now"),),
                    ),
                    ModelMessage(
                        role=MessageRole.TOOL, tool_call_id="call", name="clock.now", content="time"
                    ),
                ),
                tools=(manifest,),
                max_tokens=50,
            )
        )
    assert names[0] == names[1]


async def test_nullable_tool_preamble_and_exhausted_tools_preserve_wire_protocol(
    tmp_path: Path,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert "tools" not in payload
        assistant, result = payload["messages"][1:]
        assert assistant["content"] is None
        assert assistant["reasoning_content"] == "synthetic continuation"
        call = assistant["tool_calls"][0]
        assert call["id"] == result["tool_call_id"] == "read-1"
        assert "." not in call["function"]["name"]
        assert json.loads(call["function"]["arguments"]) == {"url": "https://example.com/"}
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Page unavailable."}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 4},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = backend(tmp_path, client)
        response = await model.generate(
            ModelRequest(
                messages=(
                    ModelMessage(role=MessageRole.USER, content="Read the supplied page."),
                    ModelMessage(
                        role=MessageRole.ASSISTANT,
                        content=None,
                        reasoning_content="synthetic continuation",
                        tool_calls=(
                            ToolCall(
                                call_id="read-1",
                                name="web.read",
                                arguments={"url": "https://example.com/"},
                            ),
                        ),
                    ),
                    ModelMessage(
                        role=MessageRole.TOOL,
                        name="web.read",
                        tool_call_id="read-1",
                        content="Page requires login; no body was read.",
                    ),
                ),
                max_tokens=64,
            )
        )
    assert response.content == "Page unavailable."
