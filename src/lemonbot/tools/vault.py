from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from jsonschema import validate  # type: ignore[import-untyped]

from lemonbot.tools.base import (
    DataClass,
    RiskLevel,
    ToolContext,
    ToolManifest,
    ToolResult,
)


class VaultError(ValueError):
    pass


_WINDOWS_FORBIDDEN = re.compile(r'[\x00-\x1f<>:"|?*]')
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)


@dataclass(frozen=True, slots=True)
class VaultRoot:
    name: str
    path: Path
    writable: bool = False


class FileVault:
    def __init__(self, roots: list[VaultRoot], *, max_read_bytes: int = 1024 * 1024) -> None:
        if len({root.name for root in roots}) != len(roots):
            raise ValueError("vault root names must be unique")
        self._roots = {
            root.name: VaultRoot(root.name, root.path.expanduser().resolve(), root.writable)
            for root in roots
        }
        self._max_read_bytes = max_read_bytes

    @staticmethod
    def _is_link(path: Path) -> bool:
        return path.is_symlink() or path.is_junction()

    def _resolve(self, root_name: str, relative_path: str, *, write: bool) -> Path:
        root = self._roots.get(root_name)
        if root is None:
            raise VaultError("unknown vault root")
        if write and not root.writable:
            raise VaultError("vault root is read-only")
        if not relative_path or len(relative_path) > 512:
            raise VaultError("path must be a normalized relative path")
        # Validate Windows semantics explicitly even when tests run elsewhere.
        # In particular, ``C:name`` is drive-relative and ``name:stream`` is an
        # NTFS alternate data stream; neither is a child file in the intended
        # sense even though some pathlib operations treat it as relative.
        windows_path = PureWindowsPath(relative_path)
        raw_parts = relative_path.replace("/", "\\").split("\\")
        if windows_path.drive or windows_path.root or any(
            part in {"", ".", ".."}
            or _WINDOWS_FORBIDDEN.search(part)
            or part.endswith((" ", "."))
            or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED
            for part in raw_parts
        ):
            raise VaultError("path must be a normalized relative path")
        candidate = root.path.joinpath(*raw_parts)
        parent = candidate.parent.resolve()
        try:
            parent.relative_to(root.path)
        except ValueError as exc:
            raise VaultError("path escapes the vault root") from exc
        current = root.path
        for part in raw_parts[:-1]:
            current = current / part
            if current.exists() and self._is_link(current):
                raise VaultError("links and junctions are forbidden inside the vault")
        return parent / candidate.name

    def read_text(self, root_name: str, relative_path: str) -> str:
        path = self._resolve(root_name, relative_path, write=False)
        if self._is_link(path):
            raise VaultError("links and junctions are forbidden inside the vault")
        resolved = path.resolve(strict=True)
        root = self._roots[root_name].path
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise VaultError("resolved path escapes the vault root") from exc
        if not resolved.is_file():
            raise VaultError("only regular files can be read")
        if resolved.stat().st_size > self._max_read_bytes:
            raise VaultError("file exceeds the vault read limit")
        raw = resolved.read_bytes()
        if b"\x00" in raw:
            raise VaultError("binary files are not exposed as text")
        return raw.decode("utf-8")

    def create_text(self, root_name: str, relative_path: str, content: str) -> Path:
        requested = self._resolve(root_name, relative_path, write=True)
        requested.parent.mkdir(parents=True, exist_ok=True)
        encoded = content.encode("utf-8")
        candidates = [requested]
        candidates.extend(
            requested.with_name(f"{requested.stem}.v{index}{requested.suffix}")
            for index in range(1, 1000)
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        for candidate in candidates:
            try:
                descriptor = os.open(candidate, flags, 0o600)
            except FileExistsError:
                continue
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                # This path was created by the O_EXCL operation above, so it
                # cannot refer to pre-existing user data. Best-effort cleanup
                # leaves a crash as an explicitly unknown approval outcome.
                candidate.unlink(missing_ok=True)
                raise
            return candidate
        raise VaultError("version limit exhausted")


class VaultReadTool:
    def __init__(self, vault: FileVault) -> None:
        self._vault = vault

    def manifest(self) -> ToolManifest:
        return ToolManifest(
            name="vault.read_text",
            description="Read one UTF-8 text file from an administrator allowlisted root.",
            input_schema=_vault_schema(include_content=False),
            action_kind="read_file",
            risk_level=RiskLevel.MEDIUM.value,
            side_effect=False,
            idempotent=True,
            required_scopes=frozenset({"vault.read"}),
            allowed_data=frozenset({DataClass.CONVERSATION, DataClass.PRIVATE_LOCAL}),
        )

    async def invoke(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        validate(arguments, self.manifest().input_schema)
        if "vault.read" not in context.granted_scopes:
            return ToolResult(ok=False, error_code="missing_scope")
        try:
            return ToolResult(
                ok=True,
                content=self._vault.read_text(arguments["root"], arguments["path"]),
            )
        except (VaultError, OSError, UnicodeDecodeError) as exc:
            return ToolResult(ok=False, error_code="vault_read_failed", content=str(exc))


class VaultCreateTool:
    def __init__(self, vault: FileVault) -> None:
        self._vault = vault

    def manifest(self) -> ToolManifest:
        return ToolManifest(
            name="vault.create_text",
            description="Create a new versioned UTF-8 file in an administrator allowlisted root.",
            input_schema=_vault_schema(include_content=True),
            action_kind="write_file",
            risk_level=RiskLevel.HIGH.value,
            side_effect=True,
            idempotent=False,
            required_scopes=frozenset({"vault.create"}),
            allowed_data=frozenset({DataClass.CONVERSATION, DataClass.PRIVATE_LOCAL}),
        )

    async def invoke(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        validate(arguments, self.manifest().input_schema)
        if "vault.create" not in context.granted_scopes:
            return ToolResult(ok=False, error_code="missing_scope")
        try:
            path = self._vault.create_text(
                arguments["root"], arguments["path"], arguments["content"]
            )
            return ToolResult(
                ok=True,
                content="file created",
                artifacts=(str(path),),
                side_effect_committed=True,
            )
        except (VaultError, OSError) as exc:
            return ToolResult(ok=False, error_code="vault_create_failed", content=str(exc))


def _vault_schema(*, include_content: bool) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "root": {"type": "string", "pattern": r"^[a-z][a-z0-9_-]{0,31}$"},
        "path": {"type": "string", "minLength": 1, "maxLength": 512},
    }
    required = ["root", "path"]
    if include_content:
        properties["content"] = {"type": "string", "maxLength": 1_000_000}
        required.append("content")
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }
