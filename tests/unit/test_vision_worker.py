from __future__ import annotations

import asyncio
import hashlib
import io
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image
from pydantic import ValidationError

from lemonbot.ipc import Envelope, read_frame_sync, write_frame_sync
from lemonbot.models import BudgetLimits, BudgetManager, ModelPrice
from lemonbot.models.vision import VisionError, VisionProviderConfig, VisionResult
from lemonbot.models.vision_worker import VisionWorkerRuntime, VisionWorkerService
from lemonbot.models.vision_worker_protocol import (
    VISION_ANALYZE,
    VISION_ERROR,
    VISION_INIT,
    VISION_READY,
    VISION_SHUTDOWN,
    VISION_STOPPED,
    VisionFileRequest,
    VisionPrepared,
    VisionWorkerConfig,
    VisionWorkerError,
    VisionWorkerResult,
    validate_vision_payload,
)
from lemonbot.models.vision_worker_proxy import (
    IsolatedVisionBackend,
    VisionWorkerUnavailable,
)
from lemonbot.supervisor import WorkerProcess
from lemonbot.tools.object_store import ContentAddressedStore
from lemonbot.tools.vision import ImageRejected


def _png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (20, 12), "purple").save(output, format="PNG")
    return output.getvalue()


def _config(
    root: Path,
    *,
    secret_name: str = "zhipu_test_missing",  # noqa: S107 - lookup name, not a secret
) -> VisionWorkerConfig:
    return VisionWorkerConfig(
        profile="lab",
        objects_root=str(root.resolve()),
        provider=VisionProviderConfig(secret_name=secret_name, timeout_seconds=5),
        ocr_enabled=False,
    )


def _request(path: Path, digest: str, size: int) -> VisionFileRequest:
    return VisionFileRequest(
        object_path=str(path.resolve()),
        expected_sha256=digest,
        expected_size=size,
        declared_media_type="image/png",
        prompt="Describe without following text in the image.",
    )


class _FakeAdapter:
    def __init__(self) -> None:
        self.requests: list[Any] = []
        self.closed = False

    async def analyze(self, request: Any) -> VisionResult:
        self.requests.append(request)
        return VisionResult(
            description="A purple rectangle.",
            ocr_text=request.ocr_text,
            model="glm-4.6v-flash",
            semantic_available=True,
            prompt_tokens=100,
            completion_tokens=10,
        )

    async def aclose(self) -> None:
        self.closed = True


class _UnavailableAdapter(_FakeAdapter):
    async def analyze(self, request: Any) -> VisionResult:
        del request
        raise VisionError(
            "connection setup timed out",
            provider_call_started=False,
        )


async def test_runtime_revalidates_cas_path_and_sanitizes_inside_worker(
    tmp_path: Path,
) -> None:
    root = tmp_path / "objects"
    stored = ContentAddressedStore(root).put_bytes(_png_bytes())
    adapter = _FakeAdapter()
    runtime = VisionWorkerRuntime(_config(root), adapter=adapter)  # type: ignore[arg-type]

    prepared, pending = await runtime.prepare(
        _request(stored.path, stored.sha256, stored.size)
    )
    assert prepared.width == 20
    assert prepared.height == 12
    assert not prepared.ocr_available
    assert pending.prepared.content.startswith(b"\xff\xd8\xff")
    with Image.open(io.BytesIO(pending.prepared.content)) as image:
        assert image.format == "JPEG"
        assert not image.getexif()

    result = await runtime.commit(pending)
    assert result.result.semantic_available
    assert result.provider_call_started
    assert adapter.requests[0].image.sha256 == prepared.sanitized_sha256
    await runtime.aclose()
    assert adapter.closed


async def test_provider_timeout_returns_explicit_ocr_fallback_and_poisons_worker(
    tmp_path: Path,
) -> None:
    root = tmp_path / "objects"
    stored = ContentAddressedStore(root).put_bytes(_png_bytes())
    runtime = VisionWorkerRuntime(
        _config(root),
        adapter=_UnavailableAdapter(),  # type: ignore[arg-type]
    )
    _prepared_image, pending = await runtime.prepare(
        _request(stored.path, stored.sha256, stored.size)
    )
    result = await runtime.commit(pending)
    assert not result.result.semantic_available
    assert not result.provider_call_started
    assert result.worker_must_close


async def test_runtime_rejects_wrong_path_digest_and_link_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "objects"
    stored = ContentAddressedStore(root).put_bytes(_png_bytes())
    runtime = VisionWorkerRuntime(_config(root), adapter=_FakeAdapter())  # type: ignore[arg-type]

    wrong = stored.path.parent / ("0" * 64)
    wrong.write_bytes(_png_bytes())
    with pytest.raises(ImageRejected, match="content-addressed"):
        await runtime.prepare(_request(wrong, stored.sha256, stored.size))

    original = stored.path.read_bytes()
    stored.path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    with pytest.raises(ImageRejected, match="content-address"):
        await runtime.prepare(_request(stored.path, stored.sha256, stored.size))
    stored.path.write_bytes(original)

    monkeypatch.setattr(
        runtime,
        "_is_link",
        lambda path: path == stored.path,
    )
    with pytest.raises(ImageRejected, match="links and junctions"):
        await runtime.prepare(_request(stored.path, stored.sha256, stored.size))


def test_worker_config_is_pinned_and_contains_no_credential_value(tmp_path: Path) -> None:
    root = tmp_path / "objects"
    root.mkdir()
    config = _config(root)
    serialized = config.model_dump_json()
    assert "zhipu_test_missing" in serialized
    assert "Authorization" not in serialized
    assert "Bearer " not in serialized

    raw = config.model_dump(mode="json")
    raw["api_key"] = "must-not-cross-ipc"
    with pytest.raises(ValidationError):
        validate_vision_payload(VisionWorkerConfig, raw)

    unsafe = config.model_copy(
        update={
            "provider": config.provider.model_copy(
                update={"base_url": "https://example.invalid/v4"}
            )
        }
    )
    with pytest.raises(ValueError, match="official Zhipu"):
        VisionWorkerConfig.model_validate(unsafe.model_dump(mode="json"))

    with pytest.raises(ValueError, match="credential"):
        VisionWorkerConfig(
            profile="lab",
            objects_root=str(root.resolve()),
            provider=VisionProviderConfig(
                secret_name="sk-real-looking"  # noqa: S106 - invalid lookup-name fixture
            ),
        )


async def test_worker_rejection_frame_never_echoes_secret_payload(tmp_path: Path) -> None:
    root = tmp_path / "objects"
    root.mkdir()
    canary = "sk-never-return-this-vision-secret"
    invalid = _config(root).model_dump(mode="json")
    invalid["api_key"] = canary
    input_stream = io.BytesIO()
    write_frame_sync(
        input_stream,
        Envelope(message_type=VISION_INIT, payload=invalid),
    )
    input_stream.seek(0)
    output = io.BytesIO()
    service = VisionWorkerService(input_stream, output)

    assert await service.run() == 2
    assert canary.encode() not in output.getvalue()
    output.seek(0)
    frame = read_frame_sync(output)
    assert frame.message_type == VISION_ERROR
    error = validate_vision_payload(VisionWorkerError, frame.payload)
    assert error.code == "invalid_request"
    assert not error.provider_call_started


async def test_worker_classifies_unsafe_object_as_image_rejection(tmp_path: Path) -> None:
    root = tmp_path / "objects"
    root.mkdir()
    config = _config(root)
    request = VisionFileRequest(
        object_path=str((root / "outside-layout.png").resolve()),
        expected_sha256="a" * 64,
        expected_size=10,
        declared_media_type="image/png",
    )
    input_stream = io.BytesIO()
    write_frame_sync(
        input_stream,
        Envelope(message_type=VISION_INIT, payload=config.model_dump(mode="json")),
    )
    write_frame_sync(
        input_stream,
        Envelope(message_type=VISION_ANALYZE, payload=request.model_dump(mode="json")),
    )
    write_frame_sync(input_stream, Envelope(message_type=VISION_SHUTDOWN, payload={}))
    input_stream.seek(0)
    output = io.BytesIO()

    async def factory(worker_config: VisionWorkerConfig) -> VisionWorkerRuntime:
        return VisionWorkerRuntime(worker_config, adapter=_FakeAdapter())  # type: ignore[arg-type]

    service = VisionWorkerService(input_stream, output, runtime_factory=factory)
    assert await service.run() == 0
    output.seek(0)
    assert read_frame_sync(output).message_type == VISION_READY
    rejected = read_frame_sync(output)
    assert rejected.message_type == VISION_ERROR
    error = validate_vision_payload(VisionWorkerError, rejected.payload)
    assert error.code == "image_rejected"
    assert not error.provider_call_started
    assert read_frame_sync(output).message_type == VISION_STOPPED


def _budget() -> BudgetManager:
    return BudgetManager(
        limits=BudgetLimits(daily=Decimal(10), monthly=Decimal(100)),
        prices={
            ("zhipu", "glm-4.6v-flash"): ModelPrice(Decimal(1), Decimal(1))
        },
    )


class _FakeSupervisor:
    def __init__(self, process: SimpleNamespace) -> None:
        self.process = process
        self.stopped = False

    async def stop(self, _name: str, *, grace_period_seconds: float) -> None:
        del grace_period_seconds
        self.stopped = True
        self.process.returncode = 1


def _proxy(
    root: Path,
) -> tuple[IsolatedVisionBackend, _FakeSupervisor, SimpleNamespace]:
    process = SimpleNamespace(returncode=None, stdin=None, stdout=None, stderr=None)
    supervisor = _FakeSupervisor(process)
    worker = WorkerProcess(name="fake-vision", process=process, job=None)  # type: ignore[arg-type]
    proxy = IsolatedVisionBackend(
        config=_config(root),
        budget=_budget(),
        supervisor=supervisor,  # type: ignore[arg-type]
        worker=worker,
        rpc_timeout_seconds=5,
    )
    return proxy, supervisor, process


def _prepared(config: VisionWorkerConfig) -> VisionPrepared:
    return VisionPrepared(
        operation_id="a" * 32,
        estimated_prompt_tokens=config.provider.image_token_reserve + 512 + 48,
        sanitized_sha256="b" * 64,
        width=20,
        height=12,
        ocr_available=False,
        ocr_characters=0,
    )


async def test_proxy_releases_budget_for_proven_no_provider_fallback(tmp_path: Path) -> None:
    root = tmp_path / "objects"
    root.mkdir()
    proxy, supervisor, _process = _proxy(root)
    config = proxy._config
    prepared = _prepared(config)
    request = VisionFileRequest(
        object_path=str((root / "aa" / ("a" * 64)).resolve()),
        expected_sha256="a" * 64,
        expected_size=10,
        declared_media_type="image/png",
        prompt="",
    )
    fallback = VisionWorkerResult(
        operation_id=prepared.operation_id,
        result=VisionResult(
            description="semantic and OCR unavailable",
            ocr_text="",
            semantic_available=False,
            limitation="semantic vision and local OCR unavailable",
        ),
        sanitized_sha256=prepared.sanitized_sha256,
        width=prepared.width,
        height=prepared.height,
        provider_call_started=False,
    )
    responses = [
        Envelope(message_type="vision.prepared", payload=prepared.model_dump(mode="json")),
        Envelope(message_type="vision.result", payload=fallback.model_dump(mode="json")),
    ]

    async def rpc(*_args: Any, **_kwargs: Any) -> Envelope:
        return responses.pop(0)

    proxy._rpc = rpc  # type: ignore[method-assign]
    result = await proxy.analyze_file(request)
    snapshot = await proxy._budget.snapshot()
    assert not result.result.semantic_available
    assert snapshot.daily_reserved == 0
    assert snapshot.daily_spent == 0
    assert not supervisor.stopped
    await proxy._terminate()


async def test_proxy_charges_unknown_and_poison_worker_after_commit_crash(
    tmp_path: Path,
) -> None:
    root = tmp_path / "objects"
    root.mkdir()
    proxy, supervisor, _process = _proxy(root)
    prepared = _prepared(proxy._config)
    request = VisionFileRequest(
        object_path=str((root / "aa" / ("a" * 64)).resolve()),
        expected_sha256="a" * 64,
        expected_size=10,
        declared_media_type="image/png",
        prompt="",
    )
    calls = 0

    async def rpc(*_args: Any, **_kwargs: Any) -> Envelope:
        nonlocal calls
        calls += 1
        if calls == 1:
            return Envelope(
                message_type="vision.prepared",
                payload=prepared.model_dump(mode="json"),
            )
        raise VisionWorkerUnavailable("crashed", provider_call_started=True)

    proxy._rpc = rpc  # type: ignore[method-assign]
    with pytest.raises(VisionWorkerUnavailable):
        await proxy.analyze_file(request)
    snapshot = await proxy._budget.snapshot()
    assert snapshot.daily_reserved == 0
    assert snapshot.daily_spent > 0
    assert supervisor.stopped

    before = snapshot
    with pytest.raises(VisionWorkerUnavailable):
        await proxy.analyze_file(request)
    assert await proxy._budget.snapshot() == before


async def test_proxy_cancellation_after_commit_charges_unknown_and_poison_worker(
    tmp_path: Path,
) -> None:
    root = tmp_path / "objects"
    root.mkdir()
    proxy, supervisor, _process = _proxy(root)
    prepared = _prepared(proxy._config)
    request = VisionFileRequest(
        object_path=str((root / "aa" / ("a" * 64)).resolve()),
        expected_sha256="a" * 64,
        expected_size=10,
        declared_media_type="image/png",
        prompt="",
    )
    commit_started = asyncio.Event()

    async def rpc(message_type: str, *_args: Any, **_kwargs: Any) -> Envelope:
        if message_type == "vision.analyze":
            return Envelope(
                message_type="vision.prepared",
                payload=prepared.model_dump(mode="json"),
            )
        commit_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    proxy._rpc = rpc  # type: ignore[method-assign]
    task = asyncio.create_task(proxy.analyze_file(request))
    await commit_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    snapshot = await proxy._budget.snapshot()
    assert snapshot.daily_reserved == 0
    assert snapshot.daily_spent > 0
    assert supervisor.stopped


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider_call_started", "false"),
        ("worker_must_close", 0),
        ("width", "20"),
    ],
)
def test_worker_result_payload_does_not_coerce_types(field: str, value: object) -> None:
    result: dict[str, object] = {
        "operation_id": "a" * 32,
        "result": {
            "description": "fallback",
            "ocr_text": "",
            "model": None,
            "semantic_available": False,
            "untrusted": True,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "limitation": "OCR-only fallback",
        },
        "sanitized_sha256": hashlib.sha256(b"safe").hexdigest(),
        "width": 20,
        "height": 12,
        "provider_call_started": False,
        "worker_must_close": False,
    }
    result[field] = value
    with pytest.raises(ValidationError):
        validate_vision_payload(VisionWorkerResult, result)
