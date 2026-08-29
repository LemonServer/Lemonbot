from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


class AlreadyRunningError(RuntimeError):
    pass


class RuntimeLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._stream: BinaryIO | None = None

    def __enter__(self) -> RuntimeLock:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        stream = self._path.open("a+b")
        try:
            stream.seek(0)
            if os.name == "nt":  # developer-test portability; Windows is not a runtime target
                import msvcrt

                if stream.tell() == stream.seek(0, os.SEEK_END):
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise AlreadyRunningError(
                        "this Lemonbot profile is already running"
                    ) from exc
            else:
                import fcntl

                try:
                    fcntl.flock(  # type: ignore[attr-defined]
                        stream.fileno(),
                        fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
                    )
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
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(  # type: ignore[attr-defined]
                    stream.fileno(),
                    fcntl.LOCK_UN,  # type: ignore[attr-defined]
                )
        finally:
            stream.close()
            self._path.unlink(missing_ok=True)
