"""Local tokenization, lossless character spans and deterministic CLS normalization."""

from __future__ import annotations

import importlib
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .artifacts import EmbeddingError

QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："
TEXT_VERSION = "bge-cls-l2-chunks-v1"


class TextChunk(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=4000)
    tokens: int = Field(ge=2, le=512)

    @model_validator(mode="after")
    def exact_span(self) -> TextChunk:
        if self.end - self.start != len(self.text):
            raise ValueError("embedding chunk span mismatch")
        return self


class TokenProcessor:
    def __init__(self, tokenizer_path: Path) -> None:
        try:
            module = importlib.import_module("tokenizers")
        except ImportError:
            raise EmbeddingError("embedding_tokenizer_unavailable") from None
        self.tokenizer = module.Tokenizer.from_file(str(tokenizer_path))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        if self.tokenizer.token_to_id("[PAD]") != 0:
            raise EmbeddingError("embedding_tokenizer_invalid")

    def encoding(self, text: str, purpose: Literal["query", "document"]) -> Any:
        prefix = QUERY_INSTRUCTION if purpose == "query" else ""
        return self.tokenizer.encode(prefix + text, add_special_tokens=True)

    def inputs(self, texts: Sequence[str], purpose: Literal["query", "document"]) -> dict[str, Any]:
        if not 1 <= len(texts) <= 16:
            raise EmbeddingError("embedding_batch_invalid")
        encodings = [self.encoding(text, purpose) for text in texts]
        if any(not text or len(text) > 4000 for text in texts):
            raise EmbeddingError("embedding_text_invalid")
        if any(len(item.ids) > 512 for item in encodings):
            raise EmbeddingError("embedding_text_requires_chunking")
        np = importlib.import_module("numpy")
        width = max(2, max(len(item.ids) for item in encodings))
        ids = np.zeros((len(texts), width), dtype=np.int64)
        mask = np.zeros_like(ids)
        types = np.zeros_like(ids)
        for i, encoding in enumerate(encodings):
            size = len(encoding.ids)
            ids[i, :size] = encoding.ids
            mask[i, :size] = 1
            types[i, :size] = encoding.type_ids
        return {"input_ids": ids, "attention_mask": mask, "token_type_ids": types}

    def chunks(self, text: str, purpose: Literal["query", "document"]) -> tuple[TextChunk, ...]:
        if not text or len(text) > 100_000:
            raise EmbeddingError("embedding_text_invalid")
        result: list[TextChunk] = []
        start = 0
        while start < len(text):
            if len(result) >= 256:
                raise EmbeddingError("embedding_chunk_limit")
            end = min(len(text), start + 4000)
            # Retokenize the final span: WordPiece can change at a cut boundary.
            for _ in range(32):
                encoding = self.encoding(text[start:end], purpose)
                if len(encoding.ids) <= 512:
                    break
                prefix_size = len(QUERY_INSTRUCTION) if purpose == "query" else 0
                boundary = encoding.offsets[511][0] - prefix_size
                if boundary <= 0 or boundary >= end - start:
                    raise EmbeddingError("embedding_chunk_boundary_invalid")
                end = start + boundary
            else:
                raise EmbeddingError("embedding_chunk_boundary_invalid")
            result.append(
                TextChunk(start=start, end=end, text=text[start:end], tokens=len(encoding.ids))
            )
            start = end
        return tuple(result)


def normalize_vectors(vectors: Sequence[Sequence[float]]) -> tuple[tuple[float, ...], ...]:
    result = []
    for vector in vectors:
        values = tuple(float(value) for value in vector)
        if len(values) != 512 or not all(math.isfinite(value) for value in values):
            raise EmbeddingError("embedding_vector_invalid")
        norm = math.sqrt(sum(value * value for value in values))
        if not math.isfinite(norm) or norm < 1e-12:
            raise EmbeddingError("embedding_vector_invalid")
        result.append(tuple(value / norm for value in values))
    return tuple(result)
