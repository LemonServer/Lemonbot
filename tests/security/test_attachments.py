from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from lemonbot.domain import ToolContext
from lemonbot.models.vision import VisionResult
from lemonbot.models.vision_worker_protocol import VisionWorkerResult
from lemonbot.tools.attachments import (
    AttachmentCapacityError,
    AttachmentScopeError,
    AttachmentStore,
)
from lemonbot.tools.vision import ImagePreprocessor
from lemonbot.tools.vision_tool import ImageUnderstandingTool


def png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (16, 16), "yellow").save(output, format="PNG")
    return output.getvalue()


class NoOCR:
    def available(self) -> bool:
        return False

    def read(self, _image) -> str:  # type: ignore[no-untyped-def]
        raise AssertionError("OCR should not be invoked")


class FakeVision:
    async def analyze(self, request) -> VisionResult:  # type: ignore[no-untyped-def]
        return VisionResult(
            description="A yellow square.",
            ocr_text=request.ocr_text,
            model="fake-vision",
            semantic_available=True,
        )


class FakeIsolatedVision:
    def __init__(self) -> None:
        self.requests = []

    async def analyze_file(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        return VisionWorkerResult(
            operation_id="a" * 32,
            result=VisionResult(
                description="Isolated yellow square.",
                ocr_text="",
                model="fake-vision",
                semantic_available=True,
            ),
            sanitized_sha256="b" * 64,
            width=16,
            height=16,
            provider_call_started=True,
        )


async def test_attachment_is_bound_to_exact_conversation_event(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path / "lab.db", tmp_path / "objects")
    await store.initialize()
    attachment = await store.ingest(
        channel="wechat_personal_lab",
        chat_id="chat-1",
        event_id="event-1",
        content=png_bytes(),
        media_type="image/png",
        original_name="..\\screen.png",
    )
    record, content = await store.read_bound(
        attachment.attachment_id,
        channel="wechat_personal_lab",
        chat_id="chat-1",
        event_id="event-1",
    )
    assert record.original_name == "screen.png"
    assert content == png_bytes()
    try:
        await store.read_bound(
            attachment.attachment_id,
            channel="wechat_personal_lab",
            chat_id="another-chat",
            event_id="event-1",
        )
    except AttachmentScopeError:
        pass
    else:
        raise AssertionError("cross-chat attachment access was not rejected")


async def test_attachment_rejects_tampered_object_size_before_read(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path / "lab.db", tmp_path / "objects")
    await store.initialize()
    attachment = await store.ingest(
        channel="wechat_personal_lab",
        chat_id="chat-1",
        event_id="event-1",
        content=png_bytes(),
        media_type="image/png",
    )
    object_path = tmp_path / "objects" / attachment.sha256[:2] / attachment.sha256
    object_path.write_bytes(png_bytes() + b"tampered")

    try:
        await store.read_bound(
            attachment.attachment_id,
            channel="wechat_personal_lab",
            chat_id="chat-1",
            event_id="event-1",
        )
    except RuntimeError as exc:
        assert "size verification" in str(exc)
    else:
        raise AssertionError("tampered attachment size was not rejected")


async def test_vision_tool_enforces_event_scope_and_marks_output_untrusted(
    tmp_path: Path,
) -> None:
    store = AttachmentStore(tmp_path / "lab.db", tmp_path / "objects")
    await store.initialize()
    attachment = await store.ingest(
        channel="wechat_personal_lab",
        chat_id="chat-1",
        event_id="event-1",
        content=png_bytes(),
        media_type="image/png",
    )
    tool = ImageUnderstandingTool(
        store,
        ImagePreprocessor(),
        NoOCR(),  # type: ignore[arg-type]
        FakeVision(),  # type: ignore[arg-type]
    )
    context = ToolContext(
        profile="lab",
        channel="wechat_personal_lab",
        chat_id="chat-1",
        event_id="event-1",
        principal_id="owner",
        granted_scopes=frozenset({"vision.read"}),
    )
    result = await tool.invoke(context, {"attachment_id": str(attachment.attachment_id)})
    assert result.ok
    assert "yellow square" in result.content
    assert result.facts[0]["trust"] == "untrusted_image_analysis"

    wrong_event = context.model_copy(update={"event_id": "event-2"})
    denied = await tool.invoke(wrong_event, {"attachment_id": str(attachment.attachment_id)})
    assert not denied.ok


async def test_isolated_vision_receives_only_exact_bound_object_reference(
    tmp_path: Path,
) -> None:
    store = AttachmentStore(tmp_path / "lab.db", tmp_path / "objects")
    await store.initialize()
    attachment = await store.ingest(
        channel="wechat_personal_lab",
        chat_id="chat-1",
        event_id="event-1",
        content=png_bytes(),
        media_type="image/png",
    )
    isolated = FakeIsolatedVision()
    tool = ImageUnderstandingTool(
        store,
        isolated_backend=isolated,  # type: ignore[arg-type]
    )
    context = ToolContext(
        profile="lab",
        channel="wechat_personal_lab",
        chat_id="chat-1",
        event_id="event-1",
        principal_id="owner",
        granted_scopes=frozenset({"vision.read"}),
    )

    result = await tool.invoke(
        context,
        {"attachment_id": str(attachment.attachment_id)},
    )
    assert result.ok
    assert len(isolated.requests) == 1
    request = isolated.requests[0]
    assert request.expected_sha256 == attachment.sha256
    assert request.expected_size == attachment.size
    assert Path(request.object_path) == (
        tmp_path / "objects" / attachment.sha256[:2] / attachment.sha256
    )

    denied = await tool.invoke(
        context.model_copy(update={"chat_id": "other-chat"}),
        {"attachment_id": str(attachment.attachment_id)},
    )
    assert not denied.ok
    assert len(isolated.requests) == 1


async def test_low_disk_latches_attachment_intake_until_explicit_recheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    free = [119]
    monkeypatch.setattr(
        "lemonbot.tools.attachments.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=free[0]),
    )
    store = AttachmentStore(
        tmp_path / "lab.db",
        tmp_path / "objects",
        minimum_free_bytes=100,
    )
    await store.initialize()

    with pytest.raises(AttachmentCapacityError) as first:
        await store.ingest(
            channel="wechat_personal_lab",
            chat_id="chat-1",
            event_id="event-low-disk",
            content=b"x" * 20,
            media_type="application/octet-stream",
        )
    assert first.value.status.paused
    assert first.value.status.reason == "insufficient_free_space"
    assert first.value.status.last_free_bytes == 119
    assert first.value.status.required_object_bytes == 20
    assert not tuple((tmp_path / "objects").rglob("incoming-*"))

    free[0] = 1_000
    with pytest.raises(AttachmentCapacityError):
        await store.ingest(
            channel="wechat_personal_lab",
            chat_id="chat-1",
            event_id="event-still-latched",
            content=b"y",
            media_type="application/octet-stream",
        )

    status = await store.recheck_capacity()
    assert not status.paused
    stored = await store.ingest(
        channel="wechat_personal_lab",
        chat_id="chat-1",
        event_id="event-rearmed",
        content=b"y",
        media_type="application/octet-stream",
    )
    assert stored.size == 1


async def test_capacity_pause_does_not_block_existing_attachment_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    free = [1_000]
    monkeypatch.setattr(
        "lemonbot.tools.attachments.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=free[0]),
    )
    store = AttachmentStore(
        tmp_path / "lab.db",
        tmp_path / "objects",
        minimum_free_bytes=100,
    )
    await store.initialize()
    original = await store.ingest(
        channel="wechat_personal_lab",
        chat_id="chat-1",
        event_id="event-existing",
        content=b"existing attachment",
        media_type="text/plain",
    )
    free[0] = 0
    with pytest.raises(AttachmentCapacityError):
        await store.ingest(
            channel="wechat_personal_lab",
            chat_id="chat-1",
            event_id="event-new",
            content=b"new attachment",
            media_type="text/plain",
        )

    record, content = await store.read_bound(
        original.attachment_id,
        channel="wechat_personal_lab",
        chat_id="chat-1",
        event_id="event-existing",
    )
    assert record == original
    assert content == b"existing attachment"
