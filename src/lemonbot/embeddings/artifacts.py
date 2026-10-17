"""Pinned public model artifacts. Download is an explicit setup operation only."""

from __future__ import annotations

import hashlib
import os
import urllib.request
from pathlib import Path

MODEL_REPOSITORY = "Xenova/bge-small-zh-v1.5"
MODEL_REVISION = "75c43b069aac4d136ba6bc1122f995fedcfd2781"
ORIGINAL_REPOSITORY = "BAAI/bge-small-zh-v1.5"
ORIGINAL_REVISION = "7999e1d3359715c523056ef9478215996d62a620"
FILES = {
    "model.onnx": (
        "onnx/model.onnx",
        94_851_877,
        "69a0b846f4f116b5e6aabf9546ea6754d02264f3211a13a1bd69b31b8040749a",
    ),
    "tokenizer.json": (
        "tokenizer.json",
        439_125,
        "48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26",
    ),
    "config.json": (
        "config.json",
        716,
        "d4193ead3a810fd694fa8a31d7fc72fbaebc0668b603e398734bf2f6538ff42f",
    ),
    "README.md": (
        "README.md",
        1057,
        "cb22054df1a98f7ee9d5b281d023e67c27cc316e1733baf03fc994d95c3048ab",
    ),
}


class EmbeddingError(RuntimeError):
    """Sanitized embedding failure code, never including input text."""


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def verify_artifacts(root: Path) -> None:
    if root.is_symlink():
        raise EmbeddingError("embedding_artifact_symlink_forbidden")
    for name, (_, size, expected) in FILES.items():
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
            raise EmbeddingError("embedding_artifact_missing_or_invalid")
        if digest(path) != expected:
            raise EmbeddingError("embedding_artifact_hash_mismatch")


def download_artifacts(root: Path) -> None:
    if root.is_symlink():
        raise EmbeddingError("embedding_artifact_symlink_forbidden")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    for name, (remote, size, expected) in FILES.items():
        destination = root / name
        if destination.is_symlink():
            raise EmbeddingError("embedding_artifact_symlink_forbidden")
        if destination.is_file():
            if destination.stat().st_size == size and digest(destination) == expected:
                continue
            raise EmbeddingError("embedding_artifact_conflict")
        temporary = destination.with_suffix(destination.suffix + ".download")
        # Exclusive creation avoids following a preexisting partial/symlink.
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            url = f"https://huggingface.co/{MODEL_REPOSITORY}/resolve/{MODEL_REVISION}/{remote}"
            with os.fdopen(fd, "wb") as output, urllib.request.urlopen(url, timeout=60) as source:
                received = 0
                while chunk := source.read(1024 * 1024):
                    received += len(chunk)
                    if received > size:
                        raise EmbeddingError("embedding_artifact_too_large")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if received != size or digest(temporary) != expected:
                raise EmbeddingError("embedding_artifact_hash_mismatch")
            temporary.replace(destination)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    verify_artifacts(root)
