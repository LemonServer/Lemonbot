#!/usr/bin/env python3
"""Synthetic stage-4 acceptance in temporary DBs; optional real CUDA, never WeChat/cloud."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import statistics
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lemonbot.config.settings import load_settings
from lemonbot.embeddings.artifacts import EmbeddingError
from lemonbot.embeddings.config import embedding_paths, worker_config
from lemonbot.embeddings.proxy import IsolatedEmbeddingWorker
from lemonbot.embeddings.verification import DOCUMENTS, QUERIES
from lemonbot.memory.unified.archive import ArchiveError, SQLiteMessageArchive
from lemonbot.memory.unified.index import RetrievalError, SQLiteRetrievalIndex
from lemonbot.memory.unified.models import ConversationScope, Message, MessagePart
from lemonbot.memory.unified.retrieval import HybridContextRetriever
from lemonbot.memory.unified.retrieval_cli import configured_options
from lemonbot.storage.migrate import upgrade_database


def fingerprint(path: Path) -> str:
    with sqlite3.connect(path) as database:
        return hashlib.sha256("\n".join(database.iterdump()).encode()).hexdigest()


async def check(args: argparse.Namespace) -> dict[str, object]:
    settings = load_settings(args.config)
    with tempfile.TemporaryDirectory(prefix="lemonbot-retrieval-") as temporary:
        root = Path(temporary)
        path = root / "synthetic.sqlite3"
        await asyncio.to_thread(upgrade_database, path)
        archive = SQLiteMessageArchive(path)
        index = SQLiteRetrievalIndex(root / "index.sqlite3", path)
        now = datetime.now(UTC)
        scope = ConversationScope(
            channel="synthetic",
            account_id="synthetic-account",
            conversation_id="synthetic-group",
            kind="group",
        )
        for position, text in enumerate(DOCUMENTS):
            await archive.put_message(
                Message(
                    scope=scope,
                    message_id=f"document-{position}",
                    sender_id=f"author-{position}",
                    direction="inbound",
                    occurred_at=now - timedelta(minutes=1),
                    parts=(MessagePart(part_id="text", kind="text", text=text),),
                )
            )
        before = await asyncio.to_thread(fingerprint, path)
        worker = None
        if args.cuda:
            if settings.context_memory.embedding_backend != "tensorrt":
                raise RetrievalError("RetrievalCudaRequiresTensorRT")
            _, executable = embedding_paths(settings)
            worker = await IsolatedEmbeddingWorker.create(
                config=worker_config(settings),
                python_executable=executable,
                timeout_seconds=settings.context_memory.embedding_timeout_seconds,
            )
        service = HybridContextRetriever(
            archive,
            index,
            worker=worker,
            unavailable_reason="semantic_disabled",
            options=configured_options(settings),
        )
        try:
            report = await service.refresh(scope)
            elapsed = []
            correct = 0
            top_one = 0
            for position, text in enumerate(QUERIES):
                current = Message(
                    scope=scope,
                    message_id=f"query-{position}",
                    sender_id="other-author",
                    direction="inbound",
                    occurred_at=now,
                    parts=(MessagePart(part_id="text", kind="text", text=text),),
                )
                start = time.perf_counter()
                result = await service.retrieve(current=current)
                elapsed.append((time.perf_counter() - start) * 1000)
                identifiers = [candidate.sources[0].message_id for candidate in result.candidates]
                correct += f"document-{position}" in identifiers
                top_one += bool(identifiers) and identifiers[0] == f"document-{position}"
                assert not args.cuda or result.degraded_reason is None
            foreign = current.model_copy(
                update={"scope": scope.model_copy(update={"account_id": "other"})}
            )
            assert not (await service.retrieve(current=foreign)).candidates
            assert before == await asyncio.to_thread(fingerprint, path)
            ready = None if worker is None else worker.ready
        finally:
            await service.close()
        reopened = HybridContextRetriever(
            archive, SQLiteRetrievalIndex(index.path, path), unavailable_reason="semantic_disabled"
        )
        # Reopen the actual GPU process and index to verify persisted vectors are reused.
        if args.cuda:
            reopened.worker = await IsolatedEmbeddingWorker.create(
                config=worker_config(settings),
                python_executable=executable,
                timeout_seconds=settings.context_memory.embedding_timeout_seconds,
            )
        try:
            restart = await reopened.refresh(scope)
            assert restart.changed == 0 and restart.vectorized == 0
        finally:
            await reopened.close()
        assert correct == len(QUERIES)
        return {
            "passed": True,
            "scope": "synthetic_stage4_not_wechat",
            "backend": "keyword_recent" if ready is None else ready.backend,
            "model_version": None if ready is None else ready.model_version,
            "precision": None if ready is None else ready.precision,
            "cases": len(QUERIES),
            "expected_source_recalled": correct,
            "expected_source_top_one": top_one,
            "cross_account_isolated": True,
            "archive_unchanged": True,
            "restart_changed_units": restart.changed,
            "restart_vectorized_units": restart.vectorized,
            "initial_index": report.model_dump(),
            "median_retrieval_ms": statistics.median(elapsed),
            "maximum_retrieval_ms": max(elapsed),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--cuda", action="store_true")
    args = parser.parse_args()
    try:
        report = asyncio.run(check(args))
    except (EmbeddingError, RetrievalError, ArchiveError, OSError, ValueError, AssertionError):
        parser.exit(1, "Synthetic retrieval acceptance failed; no service changes.\n")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
