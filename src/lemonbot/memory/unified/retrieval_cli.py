"""Explicit local index/query diagnostics. No cloud calls, keyring or WeChat."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from pathlib import Path

from lemonbot.config.settings import AppSettings, load_settings
from lemonbot.embeddings.artifacts import EmbeddingError
from lemonbot.embeddings.config import embedding_paths, worker_config
from lemonbot.embeddings.proxy import IsolatedEmbeddingWorker

from .archive import ArchiveError, SQLiteMessageArchive
from .index import RetrievalError, SQLiteRetrievalIndex
from .maintenance import UnifiedMemoryStore
from .models import ConversationScope, SourceRef
from .remember import MemoryContextRetriever
from .retrieval import HybridContextRetriever, RetrievalOptions


def configured_options(settings: AppSettings) -> RetrievalOptions:
    options = settings.context_memory
    return RetrievalOptions(
        max_messages=options.retrieval_max_messages,
        vector_chunks_per_refresh=options.retrieval_vector_chunks_per_refresh,
        semantic_min_similarity=options.retrieval_semantic_min_similarity,
        candidate_char_budget=options.retrieval_candidate_char_budget,
        recent_messages=options.retrieval_recent_messages,
    )


async def execute(args: argparse.Namespace) -> dict[str, object]:
    settings = load_settings(args.config)
    scope = ConversationScope(
        channel=args.channel,
        account_id=args.account,
        conversation_id=args.conversation,
        kind=args.kind,
    )
    archive = SQLiteMessageArchive(args.database)
    # Validate the archive schema/scope before creating any index file or GPU worker.
    await archive.list_messages(scope=scope, before_message_id=None, limit=1)
    index = SQLiteRetrievalIndex(args.index, archive.path)
    worker = None
    unavailable = "semantic_disabled"
    if args.cuda:
        if settings.context_memory.embedding_backend != "tensorrt":
            raise RetrievalError("RetrievalCudaRequiresTensorRT")
        _, executable = embedding_paths(settings)
        try:
            worker = await IsolatedEmbeddingWorker.create(
                config=worker_config(settings),
                python_executable=executable,
                timeout_seconds=settings.context_memory.embedding_timeout_seconds,
            )
        except (EmbeddingError, OSError, ValueError, TimeoutError):
            unavailable = "embedding_worker_unavailable"
    retriever = HybridContextRetriever(
        archive,
        index,
        worker=worker,
        unavailable_reason=unavailable,
        options=configured_options(settings),
    )
    try:
        if args.command == "index":
            return {
                "mode": "local_index_only",
                "report": (await retriever.refresh(scope)).model_dump(),
            }
        current = await archive.get_message(SourceRef(scope=scope, message_id=args.current_id))
        if current is None:
            raise RetrievalError("RetrievalCurrentMessageMissing")
        result = await MemoryContextRetriever(retriever, UnifiedMemoryStore(archive)).retrieve(
            current=current, limit=args.limit
        )
        payload = result.model_dump(mode="json")
        if not args.include_content:
            for candidate in payload["candidates"]:
                candidate.pop("summary")
                for notice in candidate.get("corrections", []):
                    if notice.get("latest") is not None:
                        notice["latest"].pop("text", None)
        return {
            "mode": "local_retrieval_not_answer",
            "result": payload,
            "report": None if retriever.last_sync is None else retriever.last_sync.model_dump(),
        }
    finally:
        await retriever.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("index", "query"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path)
        command.add_argument("--database", type=Path, required=True)
        command.add_argument("--index", type=Path, required=True)
        for identifier in ("channel", "account", "conversation"):
            command.add_argument("--" + identifier, required=True)
        command.add_argument("--kind", choices=("group", "private"), required=True)
        command.add_argument(
            "--cuda", action="store_true", help="Explicitly start local CUDA worker"
        )
        if name == "query":
            command.add_argument("--current-id", required=True)
            command.add_argument("--limit", type=int, default=20)
            command.add_argument("--include-content", action="store_true")
    args = parser.parse_args()
    try:
        result = asyncio.run(execute(args))
    except (RetrievalError, ArchiveError) as error:
        parser.exit(1, str(error) + "\n")
    except (sqlite3.Error, OSError, ValueError):
        parser.exit(1, "Local retrieval refused; check archive, index and config.\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
