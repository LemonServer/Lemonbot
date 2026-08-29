"""Core-side streaming proxy for the isolated AT-SPI worker."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from lemonbot.ipc import Envelope, IPCError, read_frame, write_frame
from lemonbot.supervisor import (
    SandboxedAtspiWorker,
    WorkerProcess,
    WorkerSupervisor,
    spawn_sandboxed_atspi_worker,
)

from .atspi_protocol import (
    AtspiHealth,
    AtspiInit,
    AtspiReady,
    AtspiShutdown,
    AtspiSnapshot,
    AtspiTargetSpec,
    AtspiWorkerError,
)
from .atspi_worker import ERROR, HEALTH, INIT, READY, SHUTDOWN, SNAPSHOT
from .wechat_atspi import AtspiEnrollment, AtspiSnapshotSource


class AtspiWorkerUnavailable(RuntimeError):
    pass


class AtspiWorkerSource(AtspiSnapshotSource):
    def __init__(
        self,
        supervisor: WorkerSupervisor,
        worker: WorkerProcess,
        *,
        initial_health: AtspiHealth,
        sandbox: SandboxedAtspiWorker,
    ) -> None:
        self._supervisor = supervisor
        self._worker = worker
        self._health = initial_health
        self._sandbox = sandbox
        self._closed = False
        self._stderr_task = asyncio.create_task(self._discard_stderr())
        self._proxy_output_task = asyncio.create_task(self._discard_proxy_output())

    @classmethod
    async def create(
        cls,
        *,
        enrollment: AtspiEnrollment,
        expected_pids: tuple[int, ...],
        allow_target_refs: frozenset[str],
        debounce_ms: int,
        reconcile_seconds: float,
        python_executable: Path,
        supervisor: WorkerSupervisor | None = None,
    ) -> AtspiWorkerSource:
        selected = supervisor or WorkerSupervisor()
        sandbox = await spawn_sandboxed_atspi_worker(
            selected,
            worker_python=python_executable,
            expected_pids=expected_pids,
        )
        worker = sandbox.worker
        source = cls(
            selected,
            worker,
            initial_health=AtspiHealth(healthy=True, detail_code="initializing"),
            sandbox=sandbox,
        )
        targets = tuple(
            AtspiTargetSpec.model_validate(target.model_dump())
            for target in enrollment.targets
            if target.target_ref in allow_target_refs
        )
        request = Envelope(
            message_type=INIT,
            payload=AtspiInit(
                expected_pids=expected_pids,
                account_fingerprint=enrollment.account_fingerprint,
                ui_signature=enrollment.ui_signature,
                targets=targets,
                debounce_ms=debounce_ms,
                reconcile_seconds=reconcile_seconds,
            ).model_dump(mode="json"),
        )
        try:
            stdin, stdout = worker.process.stdin, worker.process.stdout
            if stdin is None or stdout is None:
                raise AtspiWorkerUnavailable("worker pipes are unavailable")
            async with asyncio.timeout(15):
                await write_frame(stdin, request)
                response = await read_frame(stdout)
            if response.request_id != request.request_id or response.message_type != READY:
                raise AtspiWorkerUnavailable("worker initialization protocol failed")
            ready = AtspiReady.model_validate(response.payload)
            source._health = AtspiHealth(
                healthy=True,
                detail_code="ready",
                active_target_ref=None,
            )
            if not frozenset(ready.matched_pids) <= frozenset(expected_pids):
                raise AtspiWorkerUnavailable("worker process identity mismatch")
            return source
        except BaseException:
            await source.close()
            raise

    async def _discard_stderr(self) -> None:
        stderr = self._worker.process.stderr
        if stderr is None:
            return
        try:
            while await stderr.read(64 * 1024):
                pass
        except Exception:
            return

    async def _discard_proxy_output(self) -> None:
        streams = (self._sandbox.proxy.process.stdout, self._sandbox.proxy.process.stderr)

        async def discard(stream: asyncio.StreamReader | None) -> None:
            if stream is None:
                return
            try:
                while await stream.read(64 * 1024):
                    pass
            except Exception:
                return

        await asyncio.gather(*(discard(stream) for stream in streams))

    async def snapshots(self) -> AsyncIterator[AtspiSnapshot]:
        stdout = self._worker.process.stdout
        if stdout is None:
            raise AtspiWorkerUnavailable("worker stdout is unavailable")
        while not self._closed:
            try:
                envelope = await read_frame(stdout)
            except (IPCError, OSError):
                self._health = AtspiHealth(healthy=False, detail_code="ipc_failed")
                raise AtspiWorkerUnavailable("AT-SPI worker IPC failed closed") from None
            if envelope.message_type == SNAPSHOT:
                snapshot = AtspiSnapshot.model_validate(envelope.payload)
                self._health = AtspiHealth(
                    healthy=True,
                    detail_code="observing",
                    active_target_ref=snapshot.target_ref,
                )
                yield snapshot
            elif envelope.message_type == HEALTH:
                self._health = AtspiHealth.model_validate(envelope.payload)
            elif envelope.message_type == ERROR:
                error = AtspiWorkerError.model_validate(envelope.payload)
                self._health = AtspiHealth(healthy=False, detail_code=error.code)
                if error.fatal:
                    raise AtspiWorkerUnavailable("AT-SPI worker failed closed")
            else:
                self._health = AtspiHealth(healthy=False, detail_code="invalid_message")
                raise AtspiWorkerUnavailable("AT-SPI worker sent an unknown message")

    async def health(self) -> AtspiHealth:
        if self._worker.process.returncode is not None:
            return AtspiHealth(healthy=False, detail_code="worker_exited")
        return self._health

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        stdin = self._worker.process.stdin
        if stdin is not None and self._worker.process.returncode is None:
            try:
                await write_frame(
                    stdin,
                    Envelope(
                        message_type=SHUTDOWN,
                        payload=AtspiShutdown().model_dump(mode="json"),
                    ),
                )
            except (IPCError, OSError, BrokenPipeError):
                pass
        await self._supervisor.stop(self._worker.name, grace_period_seconds=2)
        await self._sandbox.close_proxy()
        if not self._stderr_task.done():
            self._stderr_task.cancel()
        if not self._proxy_output_task.done():
            self._proxy_output_task.cancel()


__all__ = ["AtspiWorkerSource", "AtspiWorkerUnavailable"]
