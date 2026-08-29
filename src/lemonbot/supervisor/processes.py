from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path


def _absolute_executable(path: Path) -> Path:
    """Validate an executable without resolving a virtualenv symlink.

    POSIX virtual environments normally expose ``bin/python`` as a symlink.
    Resolving it changes Python's executable location to ``/usr/bin`` and
    silently disables the virtual environment for the child process.
    """

    absolute = Path(os.path.abspath(path.expanduser()))
    if not absolute.is_file():
        raise ValueError("worker executable is invalid")
    return absolute


@dataclass(slots=True)
class WorkerProcess:
    name: str
    process: asyncio.subprocess.Process

    async def stop(self, grace_period_seconds: float = 5) -> None:
        if self.process.returncode is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), grace_period_seconds)
            except TimeoutError:
                try:
                    self.process.kill()
                except ProcessLookupError:
                    pass
                await self.process.wait()


class WorkerSupervisor:
    def __init__(self) -> None:
        self._workers: dict[str, WorkerProcess] = {}

    async def spawn(
        self,
        name: str,
        executable: Path,
        *arguments: str,
        cwd: Path,
        memory_limit_bytes: int | None = None,
        max_processes: int = 1,
        stream_limit_bytes: int = 64 * 1024,
    ) -> WorkerProcess:
        if name in self._workers:
            raise ValueError(f"worker already exists: {name}")
        executable = _absolute_executable(executable)
        cwd = cwd.resolve(strict=True)
        if not cwd.is_dir():
            raise ValueError("worker executable or working directory is invalid")
        if stream_limit_bytes < 1024 or stream_limit_bytes > 8 * 1024 * 1024:
            raise ValueError("worker stream limit is outside the safe range")
        environment = {
            key: value
            for key, value in os.environ.items()
            if key
            in {
                "DBUS_SESSION_BUS_ADDRESS",
                "LANG",
                "LC_ALL",
                "SYSTEMROOT",
                "TEMP",
                "TMP",
                "WINDIR",
                "PATH",
                "XDG_RUNTIME_DIR",
            }
        }
        process = await asyncio.create_subprocess_exec(
            str(executable),
            *arguments,
            cwd=cwd,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=stream_limit_bytes,
        )
        try:
            del memory_limit_bytes, max_processes
            worker = WorkerProcess(name=name, process=process)
            self._workers[name] = worker
            return worker
        except BaseException:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()
            raise

    async def stop_all(self) -> None:
        workers, self._workers = list(self._workers.values()), {}
        await asyncio.gather(*(worker.stop() for worker in workers), return_exceptions=True)

    async def stop(self, name: str, *, grace_period_seconds: float = 5) -> None:
        worker = self._workers.pop(name, None)
        if worker is not None:
            await worker.stop(grace_period_seconds=grace_period_seconds)
