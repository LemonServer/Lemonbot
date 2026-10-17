from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lemonbot.domain import DataClass, ToolContext, ToolManifest, ToolResult


class ClockTool:
    def __init__(self, timezone: str = "Asia/Shanghai") -> None:
        self.timezone = timezone
        ZoneInfo(timezone)

    def manifest(self) -> ToolManifest:
        return ToolManifest(
            name="clock.now",
            description="Read the current time in an IANA timezone.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"timezone": {"type": "string", "maxLength": 128}},
            },
            action_kind="read_time",
            required_scopes=frozenset({"clock.read"}),
            allowed_data=frozenset({DataClass.PUBLIC, DataClass.CONVERSATION}),
        )

    async def invoke(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        if "clock.read" not in context.granted_scopes:
            return ToolResult(ok=False, error_code="missing_scope")
        try:
            zone = arguments.get("timezone", self.timezone)
            now = datetime.now(UTC).astimezone(ZoneInfo(zone))
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            return ToolResult(ok=False, error_code="invalid_timezone", content="Unknown timezone.")
        return ToolResult(ok=True, content=json.dumps({"time": now.isoformat(), "timezone": zone}))
