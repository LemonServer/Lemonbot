"""Versioned bounded vector cache; never stores input text or conversation IDs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
from pathlib import Path

from .artifacts import EmbeddingError


class VectorCache:
    def __init__(self, path: Path, *, maximum_entries: int = 4096) -> None:
        if not 1 <= maximum_entries <= 100_000:
            raise EmbeddingError("embedding_cache_limit_invalid")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise EmbeddingError("embedding_cache_path_invalid")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS vectors ("
            "key TEXT PRIMARY KEY, vector TEXT NOT NULL, last_used INTEGER NOT NULL)"
        )
        self.maximum_entries = maximum_entries

    @staticmethod
    def key(version: str, purpose: str, text: str) -> str:
        raw = json.dumps([version, purpose, text], ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, key: str) -> tuple[float, ...] | None:
        row = self.db.execute("SELECT vector FROM vectors WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        try:
            vector = tuple(float(value) for value in json.loads(row[0]))
            norm = math.sqrt(sum(value * value for value in vector))
            if len(vector) != 512 or not math.isfinite(norm) or abs(norm - 1) > 1e-4:
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            self.db.execute("DELETE FROM vectors WHERE key=?", (key,))
            self.db.commit()
            return None
        self.db.execute("UPDATE vectors SET last_used=? WHERE key=?", (time.time_ns(), key))
        self.db.commit()
        return vector

    def put(self, key: str, vector: tuple[float, ...]) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO vectors VALUES (?,?,?)",
                (key, json.dumps(vector, allow_nan=False), time.time_ns()),
            )
            self.db.execute(
                "DELETE FROM vectors WHERE key IN "
                "(SELECT key FROM vectors ORDER BY last_used DESC,key LIMIT -1 OFFSET ?)",
                (self.maximum_entries,),
            )

    def close(self) -> None:
        self.db.close()
