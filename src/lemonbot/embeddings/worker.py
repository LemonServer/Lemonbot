"""Embedding-only stdio entry point. No tools, keyring, network or chat runtime."""

from __future__ import annotations

import json
import os
import sys

from pydantic import ValidationError

from lemonbot.ipc import Envelope, IPCError, read_frame_sync, write_frame_sync
from lemonbot.memory.unified.models import EmbeddingRequest

from .artifacts import EmbeddingError
from .protocol import NONFATAL_CODES, ChunkRequest, ChunkResult, EmbeddingConfig, WorkerError
from .service import EmbeddingService


def main() -> None:
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    # Native libraries may write diagnostics to stdout. Preserve the protocol FD
    # separately so neither TensorRT logs nor user text can corrupt framing.
    writer = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    service = None
    try:
        while True:
            try:
                request = read_frame_sync(sys.stdin.buffer)
            except IPCError:
                break
            fatal = False
            try:
                if request.message_type == "embedding.init" and service is None:
                    config = EmbeddingConfig.model_validate_json(
                        json.dumps(request.payload, allow_nan=False), strict=True
                    )
                    service = EmbeddingService(config)
                    kind, payload = "embedding.ready", service.ready().model_dump(mode="json")
                elif service is None:
                    raise EmbeddingError("embedding_not_initialized")
                elif request.message_type == "embedding.embed":
                    item = EmbeddingRequest.model_validate(request.payload)
                    kind, payload = "embedding.result", service.embed(item).model_dump(mode="json")
                elif request.message_type == "embedding.chunk":
                    chunk_request = ChunkRequest.model_validate(request.payload)
                    chunks = service.tokens.chunks(chunk_request.text, chunk_request.purpose)
                    kind, payload = (
                        "embedding.chunks",
                        ChunkResult(chunks=chunks).model_dump(mode="json"),
                    )
                elif request.message_type == "embedding.stats" and not request.payload:
                    kind, payload = "embedding.stats", service.stats().model_dump(mode="json")
                elif request.message_type == "embedding.shutdown" and not request.payload:
                    write_frame_sync(
                        writer,
                        Envelope(
                            request_id=request.request_id,
                            message_type="embedding.stopped",
                            payload={},
                        ),
                    )
                    break
                else:
                    raise EmbeddingError("embedding_invalid_request")
            except ValidationError:
                kind = "embedding.error"
                payload = WorkerError(code="embedding_invalid_request", fatal=False).model_dump()
            except EmbeddingError as error:
                kind = "embedding.error"
                fatal = str(error) not in NONFATAL_CODES
                payload = WorkerError(code=str(error), fatal=fatal).model_dump()
            except Exception:
                kind, fatal = "embedding.error", True
                payload = WorkerError(code="embedding_internal_error").model_dump()
            write_frame_sync(
                writer, Envelope(request_id=request.request_id, message_type=kind, payload=payload)
            )
            if fatal:
                break
    finally:
        if service is not None:
            service.close()
        writer.close()


if __name__ == "__main__":
    main()
