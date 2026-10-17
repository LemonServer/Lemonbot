"""Synthetic framed worker for subprocess fault tests; never imports a GPU runtime."""

from __future__ import annotations

import math
import struct
import sys
import time
from uuid import uuid4

from lemonbot.ipc import Envelope, read_frame_sync, write_frame_sync

mode = sys.argv[1]
calls = 0
while True:
    request = read_frame_sync(sys.stdin.buffer)
    kind, payload = (
        "embedding.ready",
        {
            "model_version": "synthetic-v1",
            "backend": "tensorrt",
            "precision": "fp16",
        },
    )
    if request.message_type == "embedding.init":
        if mode == "gpu_unavailable":
            kind, payload = "embedding.error", {"code": "embedding_cuda_unavailable", "fatal": True}
        if mode == "wrong_backend":
            payload["backend"] = "cpu"
    elif request.message_type == "embedding.shutdown":
        write_frame_sync(
            sys.stdout.buffer,
            Envelope(
                request_id=request.request_id,
                message_type="embedding.stopped",
                payload={},
            ),
        )
        break
    elif request.message_type == "embedding.stats":
        kind, payload = (
            "embedding.stats",
            {
                "requests": calls,
                "cache_hits": 0,
                "cache_misses": calls,
                "inference_batches": calls,
            },
        )
    elif request.message_type == "embedding.chunk":
        text = request.payload["text"]
        kind, payload = (
            "embedding.chunks",
            {
                "chunks": [
                    {
                        "start": 0,
                        "end": len(text),
                        "text": text if mode != "wrong_chunk" else "x" * len(text),
                        "tokens": 5,
                    }
                ]
            },
        )
    else:
        calls += 1
        identifier = request.payload["request_id"]
        if mode == "exit":
            break
        if mode == "timeout":
            time.sleep(5)
        if mode == "priority" and identifier == "hold":
            time.sleep(0.2)
        if mode == "oversized_frame":
            sys.stdout.buffer.write(struct.pack("!I", 1024 * 1024 + 1))
            sys.stdout.buffer.flush()
            break
        vector = [1.0] + [0.0] * 511
        if mode == "priority":
            vector[:2] = [math.cos(calls * 0.1), math.sin(calls * 0.1)]
        if mode == "wrong_dimensions":
            vector.pop()
        if mode == "not_normalized":
            vector = [1.0] * 512
        count = len(request.payload["texts"])
        kind, payload = (
            "embedding.result",
            {
                "request_id": identifier,
                "model_version": "foreign" if mode == "wrong_version" else "synthetic-v1",
                "backend": "tensorrt",
                "precision": "fp16",
                "vectors": [vector] * (1 if mode == "wrong_count" else count),
            },
        )
        if identifier == "reject":
            kind, payload = (
                "embedding.error",
                {
                    "code": "embedding_text_requires_chunking",
                    "fatal": False,
                },
            )
    write_frame_sync(
        sys.stdout.buffer,
        Envelope(
            request_id=uuid4() if mode == "wrong_id" else request.request_id,
            message_type=kind,
            payload=payload,
        ),
    )
