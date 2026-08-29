from __future__ import annotations

import os
from importlib import import_module
from pathlib import Path
from typing import BinaryIO, Protocol, cast


class AlreadyRunningError(RuntimeError):
    pass


class _FcntlModule(Protocol):
    LOCK_EX: int
    LOCK_NB: int
    LOCK_UN: int

    def flock(self, fd: int, operation: int) -> None: ...


def _linux_fcntl() -> _FcntlModule:
    try:
        return cast(_FcntlModule, import_module("fcntl"))
    except ModuleNotFoundError:
        raise RuntimeError("Lemonbot runtime locking is Linux-only") from None


class RuntimeLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._stream: BinaryIO | None = None

    def __enter__(self) -> RuntimeLock:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        stream = self._path.open("a+b")
        try:
            fcntl = _linux_fcntl()
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise AlreadyRunningError(
                    "this Lemonbot profile is already running"
                ) from exc
            stream.seek(0)
            stream.truncate()
            stream.write(str(os.getpid()).encode("ascii"))
            stream.flush()
            self._stream = stream
            return self
        except BaseException:
            stream.close()
            raise

    def __exit__(self, exc_type, exc, traceback) -> None:  # type: ignore[no-untyped-def]
        if self._stream is None:
            return
        stream, self._stream = self._stream, None
        try:
            fcntl = _linux_fcntl()
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
            self._path.unlink(missing_ok=True)
