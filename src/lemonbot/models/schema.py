"""Fail-closed validation for model-proposed tool calls."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from jsonschema import (  # type: ignore[import-untyped]
    Draft202012Validator,
    FormatChecker,
    SchemaError,
)

from lemonbot.domain.models import ToolCall, ToolManifest


class ToolSchemaError(ValueError):
    """An administrator-supplied tool schema is unsafe or invalid."""


class ToolCallValidationError(ValueError):
    """A model-proposed tool call does not match an enabled manifest."""


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


class ToolSchemaRegistry:
    """Immutable registry built only from tools enabled by the administrator."""

    def __init__(self, manifests: Iterable[ToolManifest]) -> None:
        self._manifests: dict[str, ToolManifest] = {}
        self._validators: dict[str, Draft202012Validator] = {}
        for manifest in manifests:
            if manifest.name in self._manifests:
                raise ToolSchemaError(f"duplicate tool manifest {manifest.name!r}")
            if _contains_remote_reference(manifest.input_schema):
                raise ToolSchemaError("remote JSON Schema references are not allowed")
            try:
                Draft202012Validator.check_schema(manifest.input_schema)
            except SchemaError as exc:
                raise ToolSchemaError(f"invalid schema for tool {manifest.name!r}") from exc
            self._manifests[manifest.name] = manifest
            self._validators[manifest.name] = Draft202012Validator(
                manifest.input_schema,
                format_checker=FormatChecker(),
            )

    def validate(self, call: ToolCall) -> ToolCall:
        validator = self._validators.get(call.name)
        if validator is None:
            raise ToolCallValidationError(f"tool {call.name!r} is not enabled")
        errors = sorted(validator.iter_errors(call.arguments), key=lambda item: list(item.path))
        if errors:
            first = errors[0]
            path = ".".join(str(part) for part in first.absolute_path) or "<root>"
            raise ToolCallValidationError(
                f"arguments for tool {call.name!r} are invalid at {path}: {first.message}"
            )
        return call

    def validate_many(self, calls: Iterable[ToolCall]) -> tuple[ToolCall, ...]:
        return tuple(self.validate(call) for call in calls)

    @staticmethod
    def parse_arguments(raw: str, *, maximum_bytes: int = 64 * 1024) -> dict[str, Any]:
        if len(raw.encode("utf-8")) > maximum_bytes:
            raise ToolCallValidationError("tool arguments exceed the local size limit")

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
            value = json.loads(
                raw,
                object_pairs_hook=reject_duplicate_keys,
                parse_constant=reject_non_finite,
            )
        except (json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise ToolCallValidationError("tool arguments are not valid JSON") from exc
        if not isinstance(value, dict):
            raise ToolCallValidationError("tool arguments must be a JSON object")
        return value
