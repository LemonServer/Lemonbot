from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from jsonschema import (  # type: ignore[import-untyped]
    Draft202012Validator,
    FormatChecker,
    SchemaError,
    ValidationError,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lemonbot.domain import DataClass, ToolContext, ToolManifest, ToolResult
from lemonbot.supervisor import WorkerProcess, WorkerSupervisor


class MCPError(RuntimeError):
    pass


_RESERVED_ARGUMENT_KEYS = frozenset(
    {
        "approved",
        "action_kind",
        "data_class",
        "enrolled",
        "granted_scopes",
        "permission",
        "permissions",
        "read_only",
        "required_scopes",
        "scope",
        "scopes",
        "side_effect",
    }
)
_UNTRUSTED_PREFIX = "[UNTRUSTED MCP TOOL OUTPUT]\n"


def _contains_remote_reference(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {"$ref", "$dynamicRef"} and isinstance(item, str):
                if "://" in item or item.startswith("//"):
                    return True
            if _contains_remote_reference(item):
                return True
    elif isinstance(value, list):
        return any(_contains_remote_reference(item) for item in value)
    return False


def _contains_reserved_argument(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in _RESERVED_ARGUMENT_KEYS:
                return True
            if _contains_reserved_argument(item):
                return True
    elif isinstance(value, list):
        return any(_contains_reserved_argument(item) for item in value)
    return False


def _truncate_utf8(value: str, maximum_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value, False
    marker = "\n[TRUNCATED]"
    marker_bytes = marker.encode("utf-8")
    available = max(0, maximum_bytes - len(marker_bytes))
    prefix = encoded[:available]
    while prefix:
        try:
            return prefix.decode("utf-8") + marker, True
        except UnicodeDecodeError:
            prefix = prefix[:-1]
    return marker_bytes[:maximum_bytes].decode("ascii", errors="ignore"), True


class PinnedMCPTool(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    remote_name: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=2_000)
    input_schema: dict[str, Any]
    read_only: bool = True
    enabled: bool = False
    timeout_seconds: float = Field(default=30, gt=0, le=120)
    max_output_bytes: int = Field(default=64 * 1024, ge=256, le=256 * 1024)

    @field_validator("remote_name")
    @classmethod
    def validate_remote_name(cls, value: str) -> str:
        if value.strip() != value or "\x00" in value:
            raise ValueError("MCP remote tool name contains unsafe whitespace or NUL")
        return value

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("MCP tool description is empty or contains NUL")
        return value

    @field_validator("input_schema")
    @classmethod
    def validate_input_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            encoded = json.dumps(value, allow_nan=False, separators=(",", ":")).encode(
                "utf-8"
            )
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("MCP tool input schema must be finite JSON") from exc
        if len(encoded) > 128 * 1024:
            raise ValueError("MCP tool input schema exceeds the local size limit")
        if value.get("type") != "object":
            raise ValueError("MCP tool input schema must have object as its root type")
        if value.get("additionalProperties") is not False:
            raise ValueError("MCP tool input schema must deny additional properties")
        if _contains_remote_reference(value):
            raise ValueError("remote JSON Schema references are not allowed")
        properties = value.get("properties", {})
        if not isinstance(properties, dict):
            raise ValueError("MCP tool schema properties must be an object")
        if any(str(key).casefold() in _RESERVED_ARGUMENT_KEYS for key in properties):
            raise ValueError("MCP tool schema cannot expose broker-owned permission fields")
        try:
            Draft202012Validator.check_schema(value)
        except SchemaError as exc:
            raise ValueError("MCP tool input schema is invalid") from exc
        return value


class PinnedMCPServer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    enabled: bool = False
    executable: Path
    arguments: tuple[str, ...] = ()
    executable_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    working_directory: Path
    expected_server_name: str = Field(default="", max_length=128)
    expected_server_version: str = Field(default="", max_length=128)
    protocol_version: str = Field(default="2025-03-26", min_length=1, max_length=32)
    tools: dict[str, PinnedMCPTool] = Field(default_factory=dict)
    startup_timeout_seconds: float = Field(default=30, gt=0, le=120)
    max_message_bytes: int = Field(default=1024 * 1024, ge=1024, le=4 * 1024 * 1024)
    memory_limit_bytes: int = Field(
        default=256 * 1024 * 1024,
        ge=32 * 1024 * 1024,
        le=2 * 1024 * 1024 * 1024,
    )
    max_processes: int = Field(default=1, ge=1, le=4)

    @field_validator("executable", "working_directory")
    @classmethod
    def require_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("MCP executable and working directory must be absolute")
        return value

    @field_validator("arguments")
    @classmethod
    def validate_arguments(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 64 or any("\x00" in item or len(item) > 4096 for item in value):
            raise ValueError("MCP fixed command arguments are invalid")
        return value

    @field_validator("expected_server_name", "expected_server_version", "protocol_version")
    @classmethod
    def validate_identity_part(cls, value: str) -> str:
        if "\x00" in value or value.strip() != value:
            raise ValueError("MCP identity fields contain unsafe whitespace or NUL")
        return value

    @field_validator("tools")
    @classmethod
    def validate_local_names(cls, value: dict[str, PinnedMCPTool]) -> dict[str, PinnedMCPTool]:
        for name in value:
            if re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", name) is None:
                raise ValueError("MCP local tool names must be lowercase identifiers")
        return value

    @model_validator(mode="after")
    def validate_enabled_identity(self) -> PinnedMCPServer:
        if self.enabled and (
            not self.expected_server_name
            or not self.expected_server_version
            or not any(tool.enabled for tool in self.tools.values())
        ):
            raise ValueError(
                "enabled MCP server requires pinned identity/version and an enabled tool"
            )
        enabled_remote_names = [
            tool.remote_name for tool in self.tools.values() if tool.enabled
        ]
        if len(enabled_remote_names) != len(set(enabled_remote_names)):
            raise ValueError("enabled MCP remote tool names must be unique per server")
        return self


class MCPStdioClient:
    """Pinned MCP stdio client supervised by a Windows Job Object.

    The command and manifest are administrator-owned immutable configuration.
    Chat/model content can only supply arguments validated by that manifest.
    """

    def __init__(
        self,
        server: PinnedMCPServer,
        *,
        supervisor: WorkerSupervisor | None = None,
    ) -> None:
        self._server = server.model_copy(deep=True)
        self._supervisor = supervisor or WorkerSupervisor()
        self._worker: WorkerProcess | None = None
        # Kept separately to make the byte protocol directly testable without spawning.
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._next_id = 1
        self._request_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()

    @property
    def server(self) -> PinnedMCPServer:
        return self._server

    def _verify_and_resolve(self) -> tuple[Path, Path]:
        configured_executable = self._server.executable
        configured_working_directory = self._server.working_directory
        if configured_executable.is_symlink() or configured_executable.is_junction():
            raise MCPError("pinned MCP executable cannot be a link or junction")
        if (
            configured_working_directory.is_symlink()
            or configured_working_directory.is_junction()
        ):
            raise MCPError("MCP working directory cannot be a link or junction")
        executable = configured_executable.resolve(strict=True)
        if not executable.is_file():
            raise MCPError("pinned MCP executable is not a regular file")
        if executable.stat().st_size > 512 * 1024 * 1024:
            raise MCPError("pinned MCP executable exceeds the verification size limit")
        hasher = hashlib.sha256()
        with executable.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                hasher.update(chunk)
        if hasher.hexdigest() != self._server.executable_sha256:
            raise MCPError("pinned MCP executable hash does not match")
        working_directory = configured_working_directory.resolve(strict=True)
        if not working_directory.is_dir():
            raise MCPError("MCP working directory does not exist")
        return executable, working_directory

    def verify_pin(self) -> None:
        self._verify_and_resolve()

    async def start(self) -> None:
        async with self._start_lock:
            if not self._server.enabled:
                raise MCPError("MCP server is not explicitly enabled")
            if self._process is not None and getattr(self._process, "returncode", None) is None:
                return
            if self._process is not None:
                await self.close()
            executable, working_directory = await asyncio.to_thread(
                self._verify_and_resolve
            )
            worker_name = f"mcp-{self._server.name}-{uuid4().hex}"
            worker = await self._supervisor.spawn(
                worker_name,
                executable,
                *self._server.arguments,
                cwd=working_directory,
                memory_limit_bytes=self._server.memory_limit_bytes,
                max_processes=self._server.max_processes,
                stream_limit_bytes=self._server.max_message_bytes + 1,
            )
            self._worker = worker
            self._process = worker.process
            self._stderr_task = asyncio.create_task(
                self._discard_stderr(), name=f"{worker_name}-stderr"
            )
            try:
                loaded_executable, loaded_working_directory = await asyncio.to_thread(
                    self._verify_and_resolve
                )
                if (
                    loaded_executable != executable
                    or loaded_working_directory != working_directory
                ):
                    raise MCPError("MCP pinned paths changed during process launch")
                result = await self._request(
                    "initialize",
                    {
                        "protocolVersion": self._server.protocol_version,
                        "capabilities": {},
                        "clientInfo": {"name": "lemonbot", "version": "0.1.0"},
                    },
                    timeout_seconds=self._server.startup_timeout_seconds,
                )
                self._validate_server_identity(result)
                await self._notify("notifications/initialized", {})
            except BaseException:
                await asyncio.shield(self.close())
                raise

    def _validate_server_identity(self, result: Any) -> None:
        if not isinstance(result, dict):
            raise MCPError("MCP initialize result is invalid")
        if result.get("protocolVersion") != self._server.protocol_version:
            raise MCPError("MCP protocol version does not match the pinned version")
        server_info = result.get("serverInfo")
        if not isinstance(server_info, dict):
            raise MCPError("MCP initialize result has no server identity")
        if (
            server_info.get("name") != self._server.expected_server_name
            or server_info.get("version") != self._server.expected_server_version
        ):
            raise MCPError("MCP server identity or version does not match its pin")

    async def _discard_stderr(self) -> None:
        process = self._process
        stderr = process.stderr if process is not None else None
        if stderr is None:
            return
        try:
            while await stderr.read(64 * 1024):
                pass
        except Exception:
            return

    async def call_tool(self, local_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self._server.enabled:
            raise MCPError("MCP server is not explicitly enabled")
        tool = self._server.tools.get(local_name)
        if tool is None or not tool.enabled:
            raise MCPError("MCP tool is not pinned and enabled")
        if _contains_reserved_argument(arguments):
            raise MCPError("MCP arguments contain broker-owned permission fields")
        try:
            Draft202012Validator(
                tool.input_schema,
                format_checker=FormatChecker(),
            ).validate(arguments)
        except ValidationError as exc:
            raise MCPError("MCP tool arguments do not match the pinned schema") from exc
        if self._process is None or getattr(self._process, "returncode", None) is not None:
            await self.start()
        try:
            result = await self._request(
                "tools/call",
                {"name": tool.remote_name, "arguments": arguments},
                timeout_seconds=tool.timeout_seconds,
            )
        except asyncio.CancelledError:
            # Cancellation can happen after the request was committed to the
            # pipe. Drop the child so a late response cannot satisfy a later call.
            await asyncio.shield(self.close())
            raise
        except MCPError:
            # Timeout, oversized data, ID mismatch or malformed output makes stdio
            # synchronization unknowable. Never reuse that child process.
            await asyncio.shield(self.close())
            raise
        if not isinstance(result, dict):
            await asyncio.shield(self.close())
            raise MCPError("MCP server returned an invalid tool result")
        if "isError" in result and type(result["isError"]) is not bool:
            await asyncio.shield(self.close())
            raise MCPError("MCP server returned an invalid error indicator")
        return result

    async def close(self) -> None:
        process, self._process = self._process, None
        worker, self._worker = self._worker, None
        stderr_task, self._stderr_task = self._stderr_task, None
        try:
            if process is not None and process.stdin is not None:
                try:
                    process.stdin.close()
                except (BrokenPipeError, ConnectionError, OSError):
                    pass
            if worker is not None:
                await self._supervisor.stop(worker.name, grace_period_seconds=2)
            elif process is not None:
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
        finally:
            if stderr_task is not None and stderr_task is not asyncio.current_task():
                try:
                    await asyncio.wait_for(stderr_task, timeout=1)
                except (TimeoutError, asyncio.CancelledError):
                    stderr_task.cancel()
                    await asyncio.gather(stderr_task, return_exceptions=True)

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        async with self._request_lock:
            request_id = self._next_id
            self._next_id += 1
            await self._send(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            response = await self._receive(
                timeout_seconds or self._server.startup_timeout_seconds
            )
            response_id = response.get("id")
            if type(response_id) is not int or response_id != request_id:
                raise MCPError("MCP response id mismatch")
            if response.get("error") is not None:
                # Server error data is untrusted and may contain credentials or
                # prompt injection. Never reflect it into logs or exceptions.
                raise MCPError("MCP request failed with a server error")
            return response.get("result")

    async def _send(self, message: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise MCPError("MCP process is not running")
        try:
            encoded = (
                json.dumps(
                    message,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
                + b"\n"
            )
        except (TypeError, ValueError, RecursionError) as exc:
            raise MCPError("MCP request is not finite JSON") from exc
        if len(encoded) > self._server.max_message_bytes:
            raise MCPError("MCP request exceeds configured limit")
        try:
            self._process.stdin.write(encoded)
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            raise MCPError("MCP process pipe failed") from exc

    async def _receive(self, timeout_seconds: float) -> dict[str, Any]:
        if self._process is None or self._process.stdout is None:
            raise MCPError("MCP process is not running")
        try:
            raw = await asyncio.wait_for(
                self._process.stdout.readline(), timeout=timeout_seconds
            )
        except TimeoutError as exc:
            raise MCPError("MCP response timed out") from exc
        except ValueError as exc:
            raise MCPError("MCP response exceeds the configured line limit") from exc
        if not raw or len(raw) > self._server.max_message_bytes or not raw.endswith(b"\n"):
            raise MCPError("MCP response is empty or exceeds configured limit")

        def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate object key")
                result[key] = value
            return result

        def reject_non_finite(_: str) -> None:
            raise ValueError("non-finite JSON number")

        try:
            response = json.loads(
                raw,
                object_pairs_hook=reject_duplicate_keys,
                parse_constant=reject_non_finite,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise MCPError("MCP response is not valid UTF-8 JSON") from exc
        if not isinstance(response, dict) or response.get("jsonrpc") != "2.0":
            raise MCPError("MCP response is not a JSON-RPC 2.0 object")
        return response


class MCPToolAdapter:
    """Expose one pinned MCP capability as a normal Lemonbot tool."""

    def __init__(self, client: MCPStdioClient, local_name: str) -> None:
        tool = client.server.tools.get(local_name)
        if not client.server.enabled or tool is None or not tool.enabled:
            raise ValueError("MCP tool must be pinned and enabled before registration")
        action = "mcp_read" if tool.read_only else "mcp_write"
        scope = f"mcp.{ 'read' if tool.read_only else 'write' }.{client.server.name}.{local_name}"
        self._client = client
        self._local_name = local_name
        self._read_only = tool.read_only
        self._manifest = ToolManifest(
            name=f"mcp.{client.server.name}.{local_name}",
            description=tool.description,
            input_schema=tool.input_schema,
            action_kind=action,
            side_effect=not tool.read_only,
            risk_level="low" if tool.read_only else "high",
            idempotent=tool.read_only,
            required_scopes=frozenset({scope}),
            allowed_data=frozenset({DataClass.PUBLIC, DataClass.CONVERSATION}),
            timeout_seconds=tool.timeout_seconds,
            max_output_bytes=tool.max_output_bytes,
        )

    def manifest(self) -> ToolManifest:
        return self._manifest

    async def invoke(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        manifest = self._manifest
        if not manifest.required_scopes.issubset(context.granted_scopes):
            return ToolResult(
                ok=False,
                content="MCP tool scope was not granted by the broker.",
                error_code="scope_denied",
            )
        if context.data_class is DataClass.SECRET:
            return ToolResult(
                ok=False,
                content="SECRET data cannot be passed to an MCP tool.",
                error_code="secret_boundary",
            )
        try:
            result = await self._client.call_tool(self._local_name, arguments)
        except MCPError:
            return ToolResult(
                ok=False,
                content="MCP tool execution failed; untrusted server details were suppressed.",
                error_code="mcp_failure",
                state_unknown=not self._read_only,
            )
        try:
            raw = json.dumps(
                result,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError, RecursionError):
            return ToolResult(
                ok=False,
                content="MCP tool returned an invalid result.",
                error_code="mcp_invalid_result",
                state_unknown=not self._read_only,
            )
        content, truncated = _truncate_utf8(
            _UNTRUSTED_PREFIX + raw,
            manifest.max_output_bytes,
        )
        is_error = result.get("isError") is True
        return ToolResult(
            ok=not is_error,
            content=content,
            metadata={"untrusted": True, "source": "mcp"},
            error_code="mcp_tool_error" if is_error else None,
            truncated=truncated,
            side_effect_committed=not self._read_only and not is_error,
            state_unknown=not self._read_only and is_error,
        )
