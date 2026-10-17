from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from lemonbot.embeddings.artifacts import EmbeddingError
from lemonbot.embeddings.protocol import EmbeddingConfig
from lemonbot.embeddings.proxy import IsolatedEmbeddingWorker
from lemonbot.memory.unified.models import EmbeddingRequest
from lemonbot.supervisor import WorkerSupervisor

FIXTURE = Path(__file__).parents[1] / "fixtures/embedding_worker.py"


class SyntheticSupervisor(WorkerSupervisor):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode

    async def spawn(self, name, executable, *arguments, **kwargs):
        return await super().spawn(name, executable, "-I", str(FIXTURE), self.mode, **kwargs)


def request(
    identifier: str = "request", purpose: str = "query", count: int = 1
) -> EmbeddingRequest:
    return EmbeddingRequest.model_validate(
        {
            "request_id": identifier,
            "purpose": purpose,
            "texts": ["synthetic text"] * count,
        }
    )


async def create(
    tmp_path: Path, mode: str = "happy", rpc_timeout: float = 2
) -> IsolatedEmbeddingWorker:
    return await IsolatedEmbeddingWorker.create(
        config=EmbeddingConfig(assets_root=str(tmp_path)),
        python_executable=Path(sys.executable),
        supervisor=SyntheticSupervisor(mode),
        timeout_seconds=rpc_timeout,
    )


async def test_framed_worker_roundtrip_and_graceful_close(tmp_path: Path) -> None:
    worker = await create(tmp_path)
    try:
        response = await worker.embed(request(count=2))
        assert response.request_id == "request" and len(response.vectors) == 2
        assert response.backend == "tensorrt"
        assert (await worker.chunks("synthetic", "document"))[0].text == "synthetic"
        assert (await worker.stats()).requests == 1
    finally:
        await worker.close()
        await worker.close()
    assert worker._worker.process.returncode == 0


async def test_nonfatal_input_error_does_not_retry_and_worker_remains_usable(
    tmp_path: Path,
) -> None:
    worker = await create(tmp_path)
    try:
        with pytest.raises(EmbeddingError, match="text_requires_chunking"):
            await worker.embed(request("reject"))
        assert (await worker.embed(request("valid"))).request_id == "valid"
        assert (await worker.stats()).requests == 2
    finally:
        await worker.close()


@pytest.mark.parametrize(
    "mode",
    [
        "exit",
        "timeout",
        "wrong_dimensions",
        "wrong_count",
        "not_normalized",
        "wrong_version",
        "oversized_frame",
    ],
)
async def test_faults_poison_worker_and_never_silently_restart_or_fallback(
    tmp_path: Path,
    mode: str,
) -> None:
    worker = await create(tmp_path, mode, rpc_timeout=0.5)
    try:
        with pytest.raises(EmbeddingError):
            await worker.embed(request(count=2))
        with pytest.raises(EmbeddingError, match="worker_unavailable"):
            await worker.embed(request("must-not-run"))
        assert worker._worker.process.returncode is not None
    finally:
        await worker.close()


@pytest.mark.parametrize("mode", ["wrong_id", "wrong_backend", "gpu_unavailable"])
async def test_initialization_rejects_wrong_identity_or_unavailable_gpu(
    tmp_path: Path,
    mode: str,
) -> None:
    owner = SyntheticSupervisor(mode)
    with pytest.raises(EmbeddingError):
        await IsolatedEmbeddingWorker.create(
            config=EmbeddingConfig(assets_root=str(tmp_path)),
            python_executable=Path(sys.executable),
            supervisor=owner,
        )
    assert owner._workers == {}


async def test_invalid_chunk_cannot_rewrite_original_text(tmp_path: Path) -> None:
    worker = await create(tmp_path, "wrong_chunk")
    try:
        with pytest.raises(EmbeddingError, match="worker_result_invalid"):
            await worker.chunks("original text", "document")
        assert worker._worker.process.returncode is not None
    finally:
        await worker.close()


async def wait_inflight(worker: IsolatedEmbeddingWorker) -> None:
    async with asyncio.timeout(1):
        while worker._inflight is None:  # noqa: ASYNC110
            await asyncio.sleep(0.001)  # ASYNC110: bounded observation of the real queue task


async def test_queries_precede_queued_background_batches(tmp_path: Path) -> None:
    worker = await create(tmp_path, "priority")
    try:
        hold = asyncio.create_task(worker.embed(request("hold", "document")))
        await wait_inflight(worker)
        background = asyncio.create_task(worker.embed(request("background", "document")))
        query = asyncio.create_task(worker.embed(request("query", "query")))
        _, doc, foreground = await asyncio.gather(hold, background, query)
        assert foreground.vectors[0][1] < doc.vectors[0][1]
    finally:
        await worker.close()


async def test_queue_is_bounded(tmp_path: Path) -> None:
    worker = await create(tmp_path, "priority")
    try:
        hold = asyncio.create_task(worker.embed(request("hold", "document")))
        await wait_inflight(worker)
        pending = [asyncio.create_task(worker.embed(request(str(i)))) for i in range(17)]
        results = await asyncio.gather(*pending, return_exceptions=True)
        await hold
        assert sum(isinstance(result, EmbeddingError) for result in results) == 1
        assert any(
            isinstance(result, EmbeddingError) and str(result) == "embedding_worker_busy"
            for result in results
        )
    finally:
        await worker.close()


async def test_cancellation_of_active_inference_kills_only_embedding_worker(tmp_path: Path) -> None:
    worker = await create(tmp_path, "timeout")
    try:
        job = asyncio.create_task(worker.embed(request()))
        await wait_inflight(worker)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert worker._worker.process.returncode is not None
        with pytest.raises(EmbeddingError, match="worker_unavailable"):
            await worker.embed(request("next"))
    finally:
        await worker.close()


async def test_close_while_busy_completes_inflight_waiter(tmp_path: Path) -> None:
    worker = await create(tmp_path, "timeout")
    job = asyncio.create_task(worker.embed(request()))
    await wait_inflight(worker)
    await asyncio.wait_for(worker.close(), timeout=2)
    with pytest.raises(EmbeddingError, match="worker_unavailable"):
        await asyncio.wait_for(job, timeout=1)


async def test_actual_entrypoint_rejects_missing_artifacts_before_gpu_import(
    tmp_path: Path,
) -> None:
    owner = WorkerSupervisor()
    with pytest.raises(EmbeddingError, match="artifact_missing_or_invalid"):
        await IsolatedEmbeddingWorker.create(
            config=EmbeddingConfig(assets_root=str(tmp_path)),
            python_executable=Path(sys.executable),
            supervisor=owner,
        )
    assert owner._workers == {}
