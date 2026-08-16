from __future__ import annotations

import asyncio
import ctypes
import os
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path

import pytest

from lemonbot.supervisor import processes
from lemonbot.supervisor.processes import WorkerProcess, WorkerSupervisor
from lemonbot.supervisor.windows_job import JobObjectError, WindowsJobObject

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects require Windows")


class _FakeFunction:
    def __init__(self, implementation: Callable[..., object]) -> None:
        self.implementation = implementation
        self.argtypes: list[object] | None = None
        self.restype: object | None = None

    def __call__(self, *arguments: object) -> object:
        return self.implementation(*arguments)


class _FakeKernel32:
    def __init__(
        self,
        *,
        job_handle: int = 0x1234_5678_8765_4321,
        process_handle: int = 0x7ABC_DEF0_1234_5678,
        set_information: Callable[..., object] | None = None,
        assign: Callable[..., object] | None = None,
        close: Callable[..., object] | None = None,
    ) -> None:
        self.job_handle = job_handle
        self.process_handle = process_handle
        self.set_calls: list[tuple[object, ...]] = []
        self.assign_calls: list[tuple[object, ...]] = []
        self.close_calls: list[object] = []
        self.CreateJobObjectW = _FakeFunction(lambda *_args: self.job_handle)
        self.SetInformationJobObject = _FakeFunction(
            set_information or self._set_information
        )
        self.OpenProcess = _FakeFunction(lambda *_args: self.process_handle)
        self.AssignProcessToJobObject = _FakeFunction(assign or self._assign)
        self.CloseHandle = _FakeFunction(close or self._close)

    def _set_information(self, *arguments: object) -> int:
        self.set_calls.append(arguments)
        return 1

    def _assign(self, *arguments: object) -> int:
        self.assign_calls.append(arguments)
        return 1

    def _close(self, handle: object) -> int:
        self.close_calls.append(handle)
        return 1


def _install_kernel32(monkeypatch: pytest.MonkeyPatch, kernel32: _FakeKernel32) -> None:
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: kernel32)


def test_job_object_declares_pointer_width_signatures_and_preserves_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        pytest.skip("regression specifically covers Win64 HANDLE width")
    kernel32 = _FakeKernel32()
    _install_kernel32(monkeypatch, kernel32)

    job = WindowsJobObject(process_memory_bytes=64 * 1024 * 1024)
    job.assign_pid(1234)
    job.close()
    job.close()

    assert kernel32.CreateJobObjectW.restype is wintypes.HANDLE
    assert kernel32.OpenProcess.restype is wintypes.HANDLE
    assert kernel32.SetInformationJobObject.restype is wintypes.BOOL
    assert kernel32.AssignProcessToJobObject.restype is wintypes.BOOL
    assert kernel32.CloseHandle.restype is wintypes.BOOL
    assert kernel32.set_calls[0][0] == kernel32.job_handle
    assert kernel32.assign_calls == [(kernel32.job_handle, kernel32.process_handle)]
    assert kernel32.close_calls == [kernel32.process_handle, kernel32.job_handle]


def test_job_object_preserves_set_information_error_across_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_set_information(*_arguments: object) -> int:
        ctypes.set_last_error(87)
        return 0

    def close_with_different_last_error(_handle: object) -> int:
        ctypes.set_last_error(6)
        return 1

    kernel32 = _FakeKernel32(
        set_information=fail_set_information,
        close=close_with_different_last_error,
    )
    _install_kernel32(monkeypatch, kernel32)

    with pytest.raises(JobObjectError, match=r"SetInformationJobObject failed: 87$"):
        WindowsJobObject()


def test_job_object_closes_process_handle_and_reports_both_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_assign(*_arguments: object) -> int:
        ctypes.set_last_error(5)
        return 0

    def fail_process_close(_handle: object) -> int:
        ctypes.set_last_error(6)
        return 0

    kernel32 = _FakeKernel32(assign=fail_assign, close=fail_process_close)
    _install_kernel32(monkeypatch, kernel32)
    job = WindowsJobObject()

    with pytest.raises(
        JobObjectError,
        match=r"AssignProcessToJobObject failed: 5; CloseHandle\(process\) failed: 6",
    ):
        job.assign_pid(44)

    assert job._handle == kernel32.job_handle


@pytest.mark.parametrize(
    ("keyword", "value"),
    [
        ("max_processes", 0),
        ("max_processes", True),
        ("max_processes", 1 << 32),
        ("process_memory_bytes", 0),
        ("process_memory_bytes", True),
    ],
)
def test_job_object_rejects_values_that_would_wrap_native_fields(
    monkeypatch: pytest.MonkeyPatch,
    keyword: str,
    value: object,
) -> None:
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: pytest.fail("invalid values must fail before loading Win32"),
    )

    with pytest.raises(ValueError):
        WindowsJobObject(**{keyword: value})  # type: ignore[arg-type]


class _FakeProcess:
    def __init__(self, *, terminate_error: bool = False) -> None:
        self.returncode: int | None = None
        self.pid = 100
        self.terminate_error = terminate_error
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self.terminate_error:
            raise ProcessLookupError

    def kill(self) -> None:
        self.kill_calls += 1

    async def wait(self) -> int:
        self.wait_calls += 1
        self.returncode = 0
        return 0


class _FakeJob:
    def __init__(self, *, assign_error: Exception | None = None) -> None:
        self.assign_error = assign_error
        self.assigned_pids: list[int] = []
        self.close_calls = 0

    def assign_pid(self, pid: int) -> None:
        self.assigned_pids.append(pid)
        if self.assign_error is not None:
            raise self.assign_error

    def close(self) -> None:
        self.close_calls += 1


async def test_worker_stop_closes_job_when_process_disappears_during_terminate() -> None:
    process = _FakeProcess(terminate_error=True)
    job = _FakeJob()
    worker = WorkerProcess(name="worker", process=process, job=job)  # type: ignore[arg-type]

    await worker.stop()
    await worker.stop()

    assert process.wait_calls == 1
    assert job.close_calls == 1
    assert worker.job is None


async def test_supervisor_spawn_cleans_up_process_and_job_when_assignment_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "worker.exe"
    executable.touch()
    process = _FakeProcess()
    job = _FakeJob(assign_error=JobObjectError("assignment failed"))

    async def fake_spawn(*_args: object, **_kwargs: object) -> _FakeProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(processes, "WindowsJobObject", lambda **_kwargs: job)

    supervisor = WorkerSupervisor()
    with pytest.raises(JobObjectError, match="assignment failed"):
        await supervisor.spawn("worker", executable, cwd=tmp_path)

    assert job.assigned_pids == [process.pid]
    assert job.close_calls == 1
    assert process.kill_calls == 1
    assert process.wait_calls == 1
