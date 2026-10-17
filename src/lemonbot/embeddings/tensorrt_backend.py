"""Build explicitly, load without rebuilding, infer natively on Jetson CUDA."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from typing import Any

from .artifacts import FILES, MODEL_REVISION, EmbeddingError, digest, verify_artifacts
from .cuda import CudaRuntime

ENGINE_FORMAT = 1
INPUTS = frozenset({"input_ids", "attention_mask", "token_type_ids"})
OUTPUT = "sentence_embedding"


def _trt() -> Any:
    try:
        module = importlib.import_module("tensorrt")
    except ImportError:
        raise EmbeddingError("embedding_tensorrt_unavailable") from None
    if not module.__version__.startswith("10."):
        raise EmbeddingError("embedding_tensorrt_version_unsupported")
    return module


def fingerprint(cuda: CudaRuntime, precision: str) -> dict[str, Any]:
    trt = _trt()
    if precision not in {"fp16", "fp32"}:
        raise EmbeddingError("embedding_precision_invalid")
    return {
        "format": ENGINE_FORMAT,
        "device": cuda.fingerprint(),
        "tensorrt": trt.__version__,
        "model_revision": MODEL_REVISION,
        "model_sha256": FILES["model.onnx"][2],
        "tokenizer_sha256": FILES["tokenizer.json"][2],
        "precision": precision,
        "minimum_shape": [1, 2],
        "optimum_shape": [1, 128],
        "maximum_shape": [16, 512],
        "pooling": "cls-l2-v1",
        "workspace_bytes": 2 * 1024**3,
    }


def _write_private(path: Path, data: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def build_engine(root: Path, precision: str) -> dict[str, Any]:
    """Explicit setup only; GPU/model drift never triggers a runtime rebuild."""
    verify_artifacts(root / "model")
    trt = _trt()
    np = importlib.import_module("numpy")
    cuda = CudaRuntime()
    try:
        identity = fingerprint(cuda, precision)
        path = root / f"engine-{precision}.plan"
        metadata = root / f"engine-{precision}.json"
        if path.exists() or metadata.exists():
            if (
                not path.is_file()
                or not metadata.is_file()
                or path.is_symlink()
                or metadata.is_symlink()
            ):
                raise EmbeddingError("embedding_engine_conflict")
            saved = json.loads(metadata.read_text())
            if saved.get("identity") != identity or saved.get("sha256") != digest(path):
                raise EmbeddingError("embedding_engine_incompatible")
            return dict(saved)
        logger = trt.Logger(trt.Logger.WARNING)
        builder = trt.Builder(logger)
        network = builder.create_network(0)
        parser = trt.OnnxParser(network, logger)
        if not parser.parse_from_file(str(root / "model/model.onnx")):
            raise EmbeddingError("embedding_onnx_parse_failed")
        if {network.get_input(i).name for i in range(network.num_inputs)} != INPUTS:
            raise EmbeddingError("embedding_onnx_inputs_invalid")
        hidden = None
        for i in reversed(range(network.num_outputs)):
            output = network.get_output(i)
            if output.name == "last_hidden_state":
                hidden = output
            network.unmark_output(output)
        if hidden is None or len(hidden.shape) != 3 or hidden.shape[-1] != 512:
            raise EmbeddingError("embedding_onnx_output_invalid")
        # Transfer only CLS vectors rather than the full token x hidden tensor.
        index = network.add_constant((1,), np.array([0], dtype=np.int32)).get_output(0)
        selected = network.add_gather(hidden, index, 1).get_output(0)
        flattened = network.add_shuffle(selected)
        flattened.reshape_dims = (0, 512)
        output = flattened.get_output(0)
        output.name = OUTPUT
        network.mark_output(output)
        output.dtype = trt.float32
        config = builder.create_builder_config()
        config.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, identity["workspace_bytes"])
        config.clear_flag(trt.BuilderFlag.TF32)
        if precision == "fp16":
            if not builder.platform_has_fast_fp16:
                raise EmbeddingError("embedding_fp16_unavailable")
            config.set_flag(trt.BuilderFlag.FP16)
        profile = builder.create_optimization_profile()
        for name in sorted(INPUTS):
            profile.set_shape(name, (1, 2), (1, 128), (16, 512))
        config.add_optimization_profile(profile)
        config.profiling_verbosity = trt.ProfilingVerbosity.DETAILED
        plan = builder.build_serialized_network(network, config)
        if plan is None:
            raise EmbeddingError("embedding_engine_build_failed")
        _write_private(path, bytes(plan))
        saved = {"identity": identity, "sha256": digest(path)}
        _write_private(metadata, json.dumps(saved, sort_keys=True, indent=2).encode())
        return saved
    finally:
        cuda.close()


class TensorRTBackend:
    def __init__(self, root: Path, precision: str) -> None:
        verify_artifacts(root / "model")
        self.trt = _trt()
        self.np = importlib.import_module("numpy")
        self.cuda = CudaRuntime()
        self._closed = False
        try:
            self.identity = fingerprint(self.cuda, precision)
            path = root / f"engine-{precision}.plan"
            metadata = root / f"engine-{precision}.json"
            if not path.is_file() or not metadata.is_file():
                raise EmbeddingError("embedding_engine_missing")
            if path.is_symlink() or metadata.is_symlink():
                raise EmbeddingError("embedding_engine_invalid")
            saved = json.loads(metadata.read_text())
            if saved.get("identity") != self.identity:
                raise EmbeddingError("embedding_engine_incompatible")
            if saved.get("sha256") != digest(path):
                raise EmbeddingError("embedding_engine_hash_mismatch")
            self.runtime = self.trt.Runtime(self.trt.Logger(self.trt.Logger.WARNING))
            self.engine = self.runtime.deserialize_cuda_engine(path.read_bytes())
            if self.engine is None:
                raise EmbeddingError("embedding_engine_load_failed")
            self.context = self.engine.create_execution_context()
            if self.context is None:
                raise EmbeddingError("embedding_engine_context_failed")
            names = {self.engine.get_tensor_name(i) for i in range(self.engine.num_io_tensors)}
            if names != INPUTS | {OUTPUT}:
                raise EmbeddingError("embedding_engine_tensors_invalid")
            self.engine_sha256 = saved["sha256"]
        except BaseException:
            self.close()
            raise

    def infer(self, inputs: dict[str, Any]) -> Any:
        if self._closed or set(inputs) != INPUTS:
            raise EmbeddingError("embedding_engine_input_invalid")
        shape = tuple(inputs["input_ids"].shape)
        if len(shape) != 2 or not 1 <= shape[0] <= 16 or not 2 <= shape[1] <= 512:
            raise EmbeddingError("embedding_engine_shape_invalid")
        buffers = []
        arrays = {}
        try:
            for name in sorted(INPUTS):
                if tuple(inputs[name].shape) != shape:
                    raise EmbeddingError("embedding_engine_shape_invalid")
                dtype = self.trt.nptype(self.engine.get_tensor_dtype(name))
                array = self.np.ascontiguousarray(inputs[name], dtype=dtype)
                if not self.context.set_input_shape(name, shape):
                    raise EmbeddingError("embedding_engine_shape_invalid")
                pointer = self.cuda.allocate(array.nbytes)
                buffers.append(pointer)
                arrays[name] = array
                self.cuda.copy(pointer, array.ctypes.data, array.nbytes, 1)
                self.context.set_tensor_address(name, pointer.value)
            output_shape = tuple(self.context.get_tensor_shape(OUTPUT))
            if output_shape != (shape[0], 512):
                raise EmbeddingError("embedding_engine_shape_invalid")
            output = self.np.empty(
                output_shape, dtype=self.trt.nptype(self.engine.get_tensor_dtype(OUTPUT))
            )
            pointer = self.cuda.allocate(output.nbytes)
            buffers.append(pointer)
            self.context.set_tensor_address(OUTPUT, pointer.value)
            if not self.context.execute_async_v3(stream_handle=self.cuda.stream.value):
                raise EmbeddingError("embedding_cuda_execution_failed")
            self.cuda.synchronize()
            self.cuda.copy(output.ctypes.data, pointer, output.nbytes, 2)
            return output
        finally:
            for pointer in reversed(buffers):
                self.cuda.free(pointer)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.context = None
            self.engine = None
            self.runtime = None
            self.cuda.close()
