from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol

from lemonbot.domain.models import DataClass as DataClass
from lemonbot.domain.models import ToolContext as ToolContext
from lemonbot.domain.models import ToolManifest as ToolManifest
from lemonbot.domain.models import ToolResult as ToolResult


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    PROHIBITED = "prohibited"


class SideEffect(StrEnum):
    NONE = "none"
    LOCAL_CREATE = "local_create"
    EXTERNAL = "external"


class Tool(Protocol):
    def manifest(self) -> ToolManifest: ...

    async def invoke(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult: ...
