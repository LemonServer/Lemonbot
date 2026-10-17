from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from lemonbot.config.settings import AppSettings
from lemonbot.embeddings import artifacts, service, tensorrt_backend
from lemonbot.embeddings.artifacts import EmbeddingError
from lemonbot.embeddings.cache import VectorCache
from lemonbot.embeddings.config import embedding_paths
from lemonbot.embeddings.protocol import EmbeddingConfig
from lemonbot.embeddings.text import QUERY_INSTRUCTION, TokenProcessor, normalize_vectors
from lemonbot.memory.unified.models import EmbeddingRequest

UNIT_VECTOR = (1.0,) + (0.0,) * 511


class CharacterTokenizer:
    def encode(self, text: str, **_kwargs: Any) -> Any:
        return SimpleNamespace(
            ids=[101, *range(len(text)), 102],
            offsets=[(0, 0), *((i, i + 1) for i in range(len(text))), (0, 0)],
            type_ids=[0] * (len(text) + 2),
            original=text,
        )


def token_processor() -> TokenProcessor:
    processor = object.__new__(TokenProcessor)
    processor.tokenizer = CharacterTokenizer()
    return processor


@pytest.mark.parametrize("purpose", ["query", "document"])
@pytest.mark.parametrize("text", ["测试😀 mixed\n" * 600, " " * 4500, "short"])
def test_chunks_preserve_all_characters_and_exact_positions(purpose: Any, text: str) -> None:
    processor = token_processor()
    chunks = processor.chunks(text, purpose)
    assert "".join(chunk.text for chunk in chunks) == text
    assert chunks[0].start == 0 and chunks[-1].end == len(text)
    assert all(chunk.text == text[chunk.start : chunk.end] for chunk in chunks)
    assert all(chunk.tokens <= 512 for chunk in chunks)
    assert all(a.end == b.start for a, b in pairwise(chunks))
    assert processor.encoding("short", purpose).original == (
        QUERY_INSTRUCTION + "short" if purpose == "query" else "short"
    )


def test_oversized_requests_fail_instead_of_silently_truncating() -> None:
    processor = token_processor()
    with pytest.raises(EmbeddingError, match="requires_chunking"):
        processor.inputs(("中" * 511,), "document")
    with pytest.raises(EmbeddingError, match="batch_invalid"):
        processor.inputs(("x",) * 17, "document")
    with pytest.raises(EmbeddingError, match="text_invalid"):
        processor.chunks("x" * 100001, "query")
    with pytest.raises(EmbeddingError, match="text_invalid"):
        processor.chunks("", "document")


def test_normalization_is_finite_unit_length_and_dimension_checked() -> None:
    output = normalize_vectors(((3.0, 4.0) + (0.0,) * 510,))[0]
    assert output[:2] == (0.6, 0.8)
    assert sum(value * value for value in output) == pytest.approx(1)
    for invalid in (
        (0.0,) * 512,
        (float("nan"),) * 512,
        (float("inf"),) * 512,
        (1e308,) * 512,
        (1.0,),
    ):
        with pytest.raises(EmbeddingError, match="vector_invalid"):
            normalize_vectors((invalid,))


def test_vector_cache_is_versioned_bounded_private_and_persistent(tmp_path: Path) -> None:
    path = tmp_path / "vectors.sqlite3"
    cache = VectorCache(path, maximum_entries=2)
    key = cache.key("version-a", "query", "private input only hashed")
    assert key != cache.key("version-b", "query", "private input only hashed")
    assert key != cache.key("version-a", "document", "private input only hashed")
    cache.put(key, UNIT_VECTOR)
    cache.put("second", UNIT_VECTOR)
    assert cache.get(key) == UNIT_VECTOR
    cache.put("third", UNIT_VECTOR)
    assert cache.get("second") is None
    assert "private input only hashed" not in "\n".join(cache.db.iterdump())
    cache.close()
    reopened = VectorCache(path, maximum_entries=2)
    assert reopened.get(key) == UNIT_VECTOR
    assert path.stat().st_mode & 0o077 == 0
    reopened.db.execute("UPDATE vectors SET vector='[1,2,3]' WHERE key=?", (key,))
    reopened.db.commit()
    assert reopened.get(key) is None
    reopened.close()


def test_public_model_download_is_pinned_hash_checked_and_repeatable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"synthetic model bytes"
    monkeypatch.setattr(
        artifacts,
        "FILES",
        {"model.onnx": ("onnx/model.onnx", len(content), hashlib.sha256(content).hexdigest())},
    )
    calls = []

    def download(url: str, **_kwargs: Any) -> io.BytesIO:
        calls.append(url)
        return io.BytesIO(content)

    monkeypatch.setattr(artifacts.urllib.request, "urlopen", download)
    root = tmp_path / "model"
    artifacts.download_artifacts(root)
    artifacts.download_artifacts(root)
    assert len(calls) == 1
    assert artifacts.MODEL_REVISION in calls[0] and "/main/" not in calls[0]
    assert (root / "model.onnx").stat().st_mode & 0o077 == 0
    (root / "model.onnx").write_bytes(b"x" * len(content))
    with pytest.raises(EmbeddingError, match="hash_mismatch"):
        artifacts.verify_artifacts(root)
    with pytest.raises(EmbeddingError, match="artifact_conflict"):
        artifacts.download_artifacts(root)


def test_download_rejects_wrong_content_and_does_not_leave_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        artifacts,
        "FILES",
        {"model.onnx": ("onnx/model.onnx", 2, hashlib.sha256(b"ok").hexdigest())},
    )
    monkeypatch.setattr(artifacts.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"bad"))
    with pytest.raises(EmbeddingError, match="too_large"):
        artifacts.download_artifacts(tmp_path)
    assert not (tmp_path / "model.onnx").exists()
    assert not list(tmp_path.glob("*.download"))


def test_artifact_directory_cannot_be_a_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "model"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(EmbeddingError, match="symlink_forbidden"):
        artifacts.verify_artifacts(link)
    with pytest.raises(EmbeddingError, match="symlink_forbidden"):
        artifacts.download_artifacts(link)


class FakeBackend:
    identity: ClassVar[dict[str, str]] = {"fake": "version-1"}
    calls = 0
    closed = False

    def __init__(self, *_args: Any) -> None:
        pass

    def infer(self, inputs: dict[str, Any]) -> Any:
        self.calls += 1
        return [UNIT_VECTOR for _ in inputs["texts"]]

    def close(self) -> None:
        self.closed = True


def fake_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(service, "verify_artifacts", lambda root: None)
    monkeypatch.setattr(service, "TensorRTBackend", FakeBackend)
    monkeypatch.setattr(
        service,
        "TokenProcessor",
        lambda root: SimpleNamespace(inputs=lambda texts, purpose: {"texts": texts}),
    )
    return service.EmbeddingService(EmbeddingConfig(assets_root=str(tmp_path)))


def test_service_cache_deduplicates_inference_without_losing_request_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = fake_service(tmp_path, monkeypatch)
    try:
        first = instance.embed(
            EmbeddingRequest(request_id="first", purpose="query", texts=("a", "a"))
        )
        assert len(first.vectors) == 2 and instance.backend.calls == 1
        second = instance.embed(
            EmbeddingRequest(request_id="second", purpose="query", texts=("a",))
        )
        assert second.request_id == "second" and first.vectors[0] == second.vectors[0]
        assert instance.backend.calls == 1
        assert instance.stats().cache_hits == 1
        assert instance.stats().inference_batches == 1
    finally:
        instance.close()
    assert instance.backend.closed


def test_gpu_failure_does_not_instantiate_cpu_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = fake_service(tmp_path, monkeypatch)
    fake.close()

    def unavailable(*_args: Any) -> Any:
        raise EmbeddingError("embedding_cuda_unavailable")

    def forbidden(*_args: Any) -> Any:
        pytest.fail("CPU fallback must never be automatic")

    monkeypatch.setattr(service, "TensorRTBackend", unavailable)
    monkeypatch.setattr(service, "CPUBackend", forbidden)
    with pytest.raises(EmbeddingError, match="cuda_unavailable"):
        service.EmbeddingService(EmbeddingConfig(assets_root=str(tmp_path)))


def test_cpu_backend_requires_explicit_selection_and_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "onnxruntime", None)
    with pytest.raises(EmbeddingError, match="cpu_backend_not_installed"):
        service.CPUBackend(tmp_path)
    instance = fake_service(tmp_path, monkeypatch)
    instance.close()
    with pytest.raises(EmbeddingError, match="cpu_requires_fp32"):
        service.EmbeddingService(EmbeddingConfig(assets_root=str(tmp_path), backend="cpu"))
    monkeypatch.setattr(service, "CPUBackend", FakeBackend)
    cpu = service.EmbeddingService(
        EmbeddingConfig(assets_root=str(tmp_path), backend="cpu", precision="fp32")
    )
    try:
        assert cpu.ready().backend == "cpu"
    finally:
        cpu.close()


def test_engine_drift_is_rejected_without_rebuild(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tensorrt_backend, "verify_artifacts", lambda root: None)
    monkeypatch.setattr(tensorrt_backend, "_trt", lambda: SimpleNamespace(__version__="10.test"))
    real_import = tensorrt_backend.importlib.import_module
    monkeypatch.setattr(
        tensorrt_backend.importlib,
        "import_module",
        lambda name: SimpleNamespace() if name == "numpy" else real_import(name),
    )
    closed = []
    monkeypatch.setattr(
        tensorrt_backend,
        "CudaRuntime",
        lambda: SimpleNamespace(
            fingerprint=lambda: {"gpu": "synthetic"}, close=lambda: closed.append(True)
        ),
    )
    (tmp_path / "engine-fp16.plan").write_bytes(b"synthetic engine")
    (tmp_path / "engine-fp16.json").write_text(json.dumps({"identity": {"wrong": "device"}}))
    with pytest.raises(EmbeddingError, match="engine_incompatible"):
        tensorrt_backend.TensorRTBackend(tmp_path, "fp16")
    assert closed == [True]


def test_core_config_paths_are_independent_and_default_cuda(tmp_path: Path) -> None:
    settings = AppSettings.model_validate(
        {"runtime": {"data_root": str(tmp_path)}, "profile": "lab"}
    )
    assets, python = embedding_paths(settings)
    assert assets == tmp_path / "embeddings/lab"
    assert python == tmp_path / "embedding-worker/bin/python"
    assert settings.context_memory.embedding_backend == "tensorrt"
    assert not settings.context_memory.enabled and not settings.context_memory.archive_enabled
    with pytest.raises(ValueError, match="embedding paths must be absolute"):
        AppSettings.model_validate(
            {"context_memory": {"embedding_worker_python": "relative/bin/python"}}
        )


def test_importing_proxy_never_imports_cuda_dependencies() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; import lemonbot.embeddings.proxy; "
            "assert not {'numpy', 'tensorrt', 'onnxruntime', 'torch'} & sys.modules.keys()",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
