"""Synchronous local embedding service used only inside the isolated worker."""

from __future__ import annotations

import hashlib
import importlib
import json
import resource
from pathlib import Path
from typing import Any

from lemonbot.memory.unified.models import EmbeddingRequest, EmbeddingResult

from .artifacts import MODEL_REVISION, EmbeddingError, verify_artifacts
from .cache import VectorCache
from .protocol import EmbeddingConfig, Ready, WorkerStats
from .tensorrt_backend import TensorRTBackend
from .text import TEXT_VERSION, TokenProcessor, normalize_vectors


class CPUBackend:
    """Explicit optional backend; never selected as a CUDA error fallback."""

    def __init__(self, root: Path) -> None:
        try:
            ort = importlib.import_module("onnxruntime")
        except ImportError:
            raise EmbeddingError("embedding_cpu_backend_not_installed") from None
        verify_artifacts(root / "model")
        self.session = ort.InferenceSession(
            str(root / "model/model.onnx"), providers=["CPUExecutionProvider"]
        )
        self.identity = {"backend": "cpu", "onnxruntime": ort.__version__, "precision": "fp32"}

    def infer(self, inputs: dict[str, Any]) -> Any:
        return self.session.run(["last_hidden_state"], inputs)[0][:, 0, :]

    def close(self) -> None:
        self.session = None


class EmbeddingService:
    def __init__(self, config: EmbeddingConfig) -> None:
        self.config = config
        root = Path(config.assets_root)
        verify_artifacts(root / "model")
        self.tokens = TokenProcessor(root / "model/tokenizer.json")
        if config.backend == "cpu" and config.precision != "fp32":
            raise EmbeddingError("embedding_cpu_requires_fp32")
        self.backend = (
            TensorRTBackend(root, config.precision)
            if config.backend == "tensorrt"
            else CPUBackend(root)
        )
        identity = json.dumps([self.backend.identity, TEXT_VERSION, MODEL_REVISION], sort_keys=True)
        self.version = "bge-small-zh-v1.5:" + hashlib.sha256(identity.encode()).hexdigest()
        try:
            self.cache = VectorCache(
                root / "vector-cache.sqlite3", maximum_entries=config.cache_entries
            )
        except BaseException:
            self.backend.close()
            raise
        self.requests = self.hits = self.misses = self.batches = 0

    def ready(self) -> Ready:
        return Ready(
            model_version=self.version,
            backend=self.config.backend,
            precision=self.config.precision,
            engine_sha256=getattr(self.backend, "engine_sha256", None),
        )

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        # Even a cache hit must respect the same tokenizer/token limit.
        self.tokens.inputs(request.texts, request.purpose)
        self.requests += 1
        keys = [self.cache.key(self.version, request.purpose, text) for text in request.texts]
        vectors = {key: self.cache.get(key) for key in dict.fromkeys(keys)}
        missing = list(dict.fromkeys(key for key in keys if vectors[key] is None))
        self.hits += sum(vectors[key] is not None for key in keys)
        self.misses += sum(vectors[key] is None for key in keys)
        if missing:
            text_by_key = dict(zip(keys, request.texts, strict=True))
            inputs = self.tokens.inputs([text_by_key[key] for key in missing], request.purpose)
            outputs = normalize_vectors(self.backend.infer(inputs))
            if len(outputs) != len(missing):
                raise EmbeddingError("embedding_vector_count_mismatch")
            self.batches += 1
            for key, vector in zip(missing, outputs, strict=True):
                self.cache.put(key, vector)
                vectors[key] = vector
        selected = tuple(vectors[key] for key in keys)
        if any(vector is None for vector in selected):
            raise EmbeddingError("embedding_cache_inconsistent")
        return EmbeddingResult.model_validate(
            {
                "request_id": request.request_id,
                "model_version": self.version,
                "backend": self.config.backend,
                "precision": self.config.precision,
                "vectors": selected,
            }
        )

    def stats(self) -> WorkerStats:
        return WorkerStats(
            requests=self.requests,
            cache_hits=self.hits,
            cache_misses=self.misses,
            inference_batches=self.batches,
            peak_process_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        )

    def close(self) -> None:
        try:
            self.cache.close()
        finally:
            self.backend.close()
