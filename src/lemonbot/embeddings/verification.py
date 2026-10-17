"""Explicit, non-private GPU acceptance; never run automatically during chats."""

from __future__ import annotations

import asyncio
import importlib
import math
import resource
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from lemonbot.memory.unified.models import EmbeddingRequest

from .artifacts import FILES, MODEL_REPOSITORY, MODEL_REVISION, EmbeddingError
from .protocol import EmbeddingConfig
from .proxy import IsolatedEmbeddingWorker
from .tensorrt_backend import TensorRTBackend
from .text import TokenProcessor, normalize_vectors

DOCUMENTS = (
    "群聊活动约定的口令是青柠。",
    "私聊用户最喜欢喝薄荷茶。",
    "Python 列表调用 sort 方法可以原地按升序排列。",
    "公众号链接遇到微信登录或验证页面时，无法确认文章正文。",
    "北京旅游可以参观故宫博物院等历史建筑。",
    "Jetson 的语义向量模型使用 CUDA GPU 加速推理。",
)
QUERIES = (
    "群里约定的活动口令是什么？",
    "私聊用户喜欢哪种茶？",
    "Python 如何把列表按升序排序？",
    "遇到微信验证页面还能读到文章正文吗？",
    "北京有什么值得参观的历史景点？",
    "Jetson 的语义向量推理使用什么硬件加速？",
)


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    return sum(x * y for x, y in zip(left, right, strict=True))


def _timings(backend: TensorRTBackend, inputs: dict[str, Any], iterations: int) -> dict[str, float]:
    for _ in range(3):
        backend.infer(inputs)
    samples = []
    for _ in range(iterations):
        start = time.perf_counter()
        backend.infer(inputs)
        samples.append((time.perf_counter() - start) * 1000)
    samples.sort()
    return {
        "median_ms": statistics.median(samples),
        "p95_ms": samples[math.ceil(len(samples) * 0.95) - 1],
        "texts_per_second": len(inputs["input_ids"]) * 1000 / statistics.mean(samples),
    }


async def _worker_acceptance(root: Path, precision: Literal["fp16", "fp32"]) -> dict[str, Any]:
    config = EmbeddingConfig(assets_root=str(root), precision=precision)
    start = time.perf_counter()
    worker = await IsolatedEmbeddingWorker.create(
        config=config, python_executable=Path(sys.executable)
    )
    startup_ms = (time.perf_counter() - start) * 1000
    text = "本地向量缓存合成验收 " + uuid4().hex
    try:
        request = EmbeddingRequest(request_id=uuid4().hex, purpose="query", texts=(text,))
        start = time.perf_counter()
        first = await worker.embed(request)
        miss_ms = (time.perf_counter() - start) * 1000
        start = time.perf_counter()
        again = await worker.embed(request.model_copy(update={"request_id": uuid4().hex}))
        hit_ms = (time.perf_counter() - start) * 1000
        if first.vectors != again.vectors:
            raise EmbeddingError("embedding_cache_verification_failed")
        long_text = "这是包含中文、English 和 😀 的长文分块验证。\n" * 500
        chunks = await worker.chunks(long_text, "document")
        if "".join(chunk.text for chunk in chunks) != long_text:
            raise EmbeddingError("embedding_chunk_verification_failed")
        stats = (await worker.stats()).model_dump()
    finally:
        await worker.close()
    reopened = await IsolatedEmbeddingWorker.create(
        config=config, python_executable=Path(sys.executable)
    )
    try:
        persisted = await reopened.embed(
            EmbeddingRequest(
                request_id=uuid4().hex,
                purpose="query",
                texts=(text,),
            )
        )
        restart_stats = (await reopened.stats()).model_dump()
        if persisted.vectors != first.vectors or restart_stats["inference_batches"] != 0:
            raise EmbeddingError("embedding_cache_restart_failed")
    finally:
        await reopened.close()
    return {
        "startup_ms": startup_ms,
        "cache_miss_rpc_ms": miss_ms,
        "cache_hit_rpc_ms": hit_ms,
        "stats": stats,
        "restart_stats": restart_stats,
        "lossless_chunks": len(chunks),
    }


def verify_device(root: Path) -> dict[str, Any]:
    tokens = TokenProcessor(root / "model/tokenizer.json")
    long_chunk = tokens.chunks("测试语义模型的长序列。" * 100, "document")[0].text
    cases: dict[Literal["document", "query"], tuple[str, ...]] = {
        "document": (*DOCUMENTS, " ", long_chunk, "Mixed 中文 English 😀"),
        "query": QUERIES,
    }
    outputs: dict[str, dict[str, tuple[tuple[float, ...], ...]]] = {}
    metrics = {}
    engine_hashes = {}
    device: dict[str, Any] = {}
    fp16_error = None
    for precision in ("fp32", "fp16"):
        start = time.perf_counter()
        backend = None
        try:
            backend = TensorRTBackend(root, precision)
            cold_ms = (time.perf_counter() - start) * 1000
            device = {k: v for k, v in backend.cuda.fingerprint().items() if k != "uuid"}
            outputs[precision] = {
                purpose: normalize_vectors(backend.infer(tokens.inputs(texts, purpose)))
                for purpose, texts in cases.items()
            }
            outputs[precision]["maximum_profile"] = normalize_vectors(
                backend.infer(tokens.inputs((long_chunk,) * 16, "document"))
            )
            ranks = [
                max(
                    range(len(DOCUMENTS)),
                    key=lambda j: _cosine(query, outputs[precision]["document"][j]),
                )
                for query in outputs[precision]["query"]
            ]
            if precision == "fp32" and ranks != list(range(len(QUERIES))):
                raise EmbeddingError("embedding_reference_ranking_failed")
            single = _timings(backend, tokens.inputs((DOCUMENTS[0],), "document"), 30)
            batch = _timings(backend, tokens.inputs((DOCUMENTS * 3)[:16], "document"), 10)
            metrics[precision] = {
                "cold_backend_ms": cold_ms,
                "batch_1": single,
                "batch_16": batch,
                "golden_ranks": ranks,
                "memory": backend.cuda.memory(),
            }
            engine_hashes[precision] = backend.engine_sha256
        except EmbeddingError as error:
            if precision == "fp32":
                raise
            fp16_error = str(error)
            outputs.pop("fp16", None)
        finally:
            if backend is not None:
                backend.close()
    minimum = None
    if "fp16" in outputs:
        minimum = min(
            _cosine(a, b)
            for purpose in outputs["fp32"]
            for a, b in zip(outputs["fp32"][purpose], outputs["fp16"][purpose], strict=True)
        )
    fp16_passed = (
        minimum is not None
        and minimum >= 0.999
        and metrics["fp16"]["golden_ranks"] == list(range(len(QUERIES)))
    )
    selected: Literal["fp16", "fp32"] = "fp16" if fp16_passed else "fp32"
    worker = asyncio.run(_worker_acceptance(root, selected))
    return {
        "schema_version": 1,
        "passed": True,
        "selected_backend": "tensorrt",
        "selected_precision": selected,
        "fp16_passed": fp16_passed,
        "fp16_error": fp16_error,
        "minimum_fp16_fp32_cosine": minimum,
        "model_repository": MODEL_REPOSITORY,
        "model_revision": MODEL_REVISION,
        "onnx_sha256": FILES["model.onnx"][2],
        "engine_sha256": engine_hashes,
        "device": device,
        "metrics": metrics,
        "worker": worker,
        "peak_process_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        "execution": "TensorRT execute_async_v3 + CUDA synchronization; no CPU model fallback",
        "scope": "synthetic_device_acceptance_not_wechat",
        "numpy_version": importlib.import_module("numpy").__version__,
        "tokenizers_version": importlib.import_module("tokenizers").__version__,
        "tensorrt_version": importlib.import_module("tensorrt").__version__,
    }
