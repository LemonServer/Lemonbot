from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Any


class JobObjectError(RuntimeError):
    pass


if os.name == "nt":

    class _SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BASIC_LIMITS(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _EXTENDED_LIMITS(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC_LIMITS),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


class WindowsJobObject:
    _KILL_ON_CLOSE = 0x00002000
    _PROCESS_MEMORY = 0x00000100
    _ACTIVE_PROCESS = 0x00000008
    _EXTENDED_LIMIT_INFORMATION = 9
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001
    _DWORD_MAX = (1 << 32) - 1

    def __init__(self, *, max_processes: int = 1, process_memory_bytes: int | None = None) -> None:
        if os.name != "nt":
            raise JobObjectError("Windows Job Objects are only available on Windows")
        self._validate_limits(max_processes, process_memory_bytes)
        kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
        self._configure_api(kernel32)
        self._kernel32 = kernel32
        self._handle: int | None = None
        self._handle = kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise JobObjectError(f"CreateJobObjectW failed: {ctypes.get_last_error()}")
        limits = _EXTENDED_LIMITS()
        limits.BasicLimitInformation.LimitFlags = self._KILL_ON_CLOSE | self._ACTIVE_PROCESS
        limits.BasicLimitInformation.ActiveProcessLimit = max_processes
        if process_memory_bytes is not None:
            limits.BasicLimitInformation.LimitFlags |= self._PROCESS_MEMORY
            limits.ProcessMemoryLimit = process_memory_bytes
        ok = kernel32.SetInformationJobObject(
            self._handle,
            self._EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        )
        if not ok:
            error = ctypes.get_last_error()
            try:
                self.close()
            except JobObjectError as close_error:
                raise JobObjectError(
                    f"SetInformationJobObject failed: {error}; {close_error}"
                ) from close_error
            raise JobObjectError(f"SetInformationJobObject failed: {error}")

    @classmethod
    def _validate_limits(cls, max_processes: int, process_memory_bytes: int | None) -> None:
        if (
            isinstance(max_processes, bool)
            or not isinstance(max_processes, int)
            or not 1 <= max_processes <= cls._DWORD_MAX
        ):
            raise ValueError("max_processes must be a positive DWORD value")
        if process_memory_bytes is None:
            return
        size_t_max = (1 << (ctypes.sizeof(ctypes.c_size_t) * 8)) - 1
        if (
            isinstance(process_memory_bytes, bool)
            or not isinstance(process_memory_bytes, int)
            or not 1 <= process_memory_bytes <= size_t_max
        ):
            raise ValueError("process_memory_bytes must be a positive SIZE_T value")

    @staticmethod
    def _configure_api(kernel32: Any) -> None:
        # ctypes otherwise assumes c_int return values. That silently truncates HANDLEs on Win64.
        kernel32.CreateJobObjectW.argtypes = [
            ctypes.POINTER(_SECURITY_ATTRIBUTES),
            wintypes.LPCWSTR,
        ]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

    def assign_pid(self, pid: int) -> None:
        if isinstance(pid, bool) or not isinstance(pid, int) or not 1 <= pid <= self._DWORD_MAX:
            raise ValueError("pid must be a positive DWORD value")
        if self._handle is None:
            raise JobObjectError("cannot assign a process to a closed Job Object")
        process = self._kernel32.OpenProcess(
            self._PROCESS_SET_QUOTA | self._PROCESS_TERMINATE, False, pid
        )
        if not process:
            raise JobObjectError(f"OpenProcess failed: {ctypes.get_last_error()}")
        assigned = bool(self._kernel32.AssignProcessToJobObject(self._handle, process))
        assign_error = ctypes.get_last_error() if not assigned else None
        closed = bool(self._kernel32.CloseHandle(process))
        close_error = ctypes.get_last_error() if not closed else None
        if not assigned:
            detail = f"AssignProcessToJobObject failed: {assign_error}"
            if close_error is not None:
                detail += f"; CloseHandle(process) failed: {close_error}"
            raise JobObjectError(detail)
        if close_error is not None:
            raise JobObjectError(f"CloseHandle(process) failed: {close_error}")

    def close(self) -> None:
        handle, self._handle = getattr(self, "_handle", None), None
        if handle and not self._kernel32.CloseHandle(handle):
            raise JobObjectError(f"CloseHandle(job) failed: {ctypes.get_last_error()}")

    def __enter__(self) -> WindowsJobObject:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:  # type: ignore[no-untyped-def]
        self.close()
