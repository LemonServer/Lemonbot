"""Bounded embedding-only messages over the existing 1 MiB framed protocol."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .text import TextChunk

NONFATAL_CODES = frozenset(
    {
        "embedding_text_invalid",
        "embedding_batch_invalid",
        "embedding_text_requires_chunking",
        "embedding_chunk_limit",
        "embedding_chunk_boundary_invalid",
        "embedding_invalid_request",
    }
)


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EmbeddingConfig(Payload):
    assets_root: str
    backend: Literal["tensorrt", "cpu"] = "tensorrt"
    precision: Literal["fp16", "fp32"] = "fp16"
    cache_entries: int = Field(default=4096, ge=1, le=100_000)

    @field_validator("assets_root")
    @classmethod
    def absolute_root(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or path.resolve() != path or not path.is_dir():
            raise ValueError("embedding assets must be an existing absolute normalized directory")
        return value


class ChunkRequest(Payload):
    text: str = Field(min_length=1, max_length=100_000)
    purpose: Literal["query", "document"]


class ChunkResult(Payload):
    chunks: tuple[TextChunk, ...] = Field(min_length=1, max_length=256)


class Ready(Payload):
    model_version: str = Field(min_length=1, max_length=512)
    backend: Literal["tensorrt", "cpu"]
    precision: Literal["fp16", "fp32"]
    engine_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class WorkerError(Payload):
    code: str = Field(pattern=r"^embedding_[a-z0-9_]{1,100}$")
    fatal: bool = True


class WorkerStats(Payload):
    requests: int = Field(ge=0)
    cache_hits: int = Field(ge=0)
    cache_misses: int = Field(ge=0)
    inference_batches: int = Field(ge=0)
    peak_process_rss_bytes: int = Field(default=0, ge=0)
