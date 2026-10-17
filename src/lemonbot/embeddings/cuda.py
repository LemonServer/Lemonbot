"""Small typed ctypes bridge to the installed CUDA runtime; no new GPU wheel."""

from __future__ import annotations

import ctypes
from typing import Any

from .artifacts import EmbeddingError


class CudaRuntime:
    def __init__(self) -> None:
        try:
            self.lib = ctypes.CDLL("libcudart.so.13")
            self.driver = ctypes.CDLL("libcuda.so.1")
        except OSError:
            raise EmbeddingError("embedding_cuda_unavailable") from None
        for name, arguments in {
            "cudaGetDeviceCount": [ctypes.POINTER(ctypes.c_int)],
            "cudaSetDevice": [ctypes.c_int],
            "cudaRuntimeGetVersion": [ctypes.POINTER(ctypes.c_int)],
            "cudaDriverGetVersion": [ctypes.POINTER(ctypes.c_int)],
            "cudaMalloc": [ctypes.POINTER(ctypes.c_void_p), ctypes.c_size_t],
            "cudaFree": [ctypes.c_void_p],
            "cudaMemcpy": [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int],
            "cudaStreamCreate": [ctypes.POINTER(ctypes.c_void_p)],
            "cudaStreamSynchronize": [ctypes.c_void_p],
            "cudaStreamDestroy": [ctypes.c_void_p],
            "cudaMemGetInfo": [ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)],
        }.items():
            function = getattr(self.lib, name)
            function.argtypes = arguments
            function.restype = ctypes.c_int
        count = ctypes.c_int()
        self.check(self.lib.cudaGetDeviceCount(ctypes.byref(count)))
        if count.value < 1:
            raise EmbeddingError("embedding_cuda_unavailable")
        self.check(self.lib.cudaSetDevice(0))
        self.stream = ctypes.c_void_p()
        self.check(self.lib.cudaStreamCreate(ctypes.byref(self.stream)))
        self._closed = False

    @staticmethod
    def check(code: int) -> None:
        if code != 0:
            raise EmbeddingError(f"embedding_cuda_error_{code}")

    def fingerprint(self) -> dict[str, Any]:
        name = ctypes.create_string_buffer(256)
        major, minor = ctypes.c_int(), ctypes.c_int()
        driver, runtime = ctypes.c_int(), ctypes.c_int()
        self.check(self.driver.cuDeviceGetName(name, len(name), 0))
        self.check(
            self.driver.cuDeviceComputeCapability(ctypes.byref(major), ctypes.byref(minor), 0)
        )
        self.check(self.lib.cudaDriverGetVersion(ctypes.byref(driver)))
        self.check(self.lib.cudaRuntimeGetVersion(ctypes.byref(runtime)))
        uuid = (ctypes.c_ubyte * 16)()
        self.check(self.driver.cuDeviceGetUuid(ctypes.byref(uuid), 0))
        return {
            "gpu": name.value.decode("utf-8"),
            "compute": [major.value, minor.value],
            "uuid": bytes(uuid).hex(),
            "driver": driver.value,
            "cuda_runtime": runtime.value,
        }

    def memory(self) -> dict[str, int]:
        free, total = ctypes.c_size_t(), ctypes.c_size_t()
        self.check(self.lib.cudaMemGetInfo(ctypes.byref(free), ctypes.byref(total)))
        return {"free_bytes": free.value, "total_bytes": total.value}

    def allocate(self, size: int) -> ctypes.c_void_p:
        pointer = ctypes.c_void_p()
        self.check(self.lib.cudaMalloc(ctypes.byref(pointer), size))
        return pointer

    def free(self, pointer: ctypes.c_void_p) -> None:
        self.check(self.lib.cudaFree(pointer))

    def copy(
        self,
        destination: int | ctypes.c_void_p,
        source: int | ctypes.c_void_p,
        size: int,
        kind: int,
    ) -> None:
        self.check(self.lib.cudaMemcpy(destination, source, size, kind))

    def synchronize(self) -> None:
        self.check(self.lib.cudaStreamSynchronize(self.stream))

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.check(self.lib.cudaStreamDestroy(self.stream))
