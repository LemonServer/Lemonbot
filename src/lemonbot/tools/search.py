"""Bounded local SearXNG searches with a durable request quota."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from lemonbot.domain import DataClass, ToolContext, ToolManifest, ToolResult


class SearchQuota:
    def __init__(
        self, path: Path, *, daily: int = 20, monthly: int = 600, timezone: str = "Asia/Shanghai"
    ) -> None:
        self.path, self.daily, self.monthly = path, daily, monthly
        self.timezone = ZoneInfo(timezone)

    def claim(self) -> str | None:
        now = datetime.now(UTC).astimezone(self.timezone)
        day, month = now.date().isoformat(), now.strftime("%Y-%m")
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS search_quota "
                "(id TEXT PRIMARY KEY, day TEXT NOT NULL, month TEXT NOT NULL)"
            )
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            daily = db.execute("SELECT count(*) FROM search_quota WHERE day=?", (day,)).fetchone()[
                0
            ]
            monthly = db.execute(
                "SELECT count(*) FROM search_quota WHERE month=?", (month,)
            ).fetchone()[0]
            if daily >= self.daily or monthly >= self.monthly:
                db.rollback()
                return None
            identifier = uuid4().hex
            db.execute("INSERT INTO search_quota VALUES (?,?,?)", (identifier, day, month))
            db.commit()
            return identifier

    def release(self, identifier: str) -> None:
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.execute("DELETE FROM search_quota WHERE id=?", (identifier,))
            db.commit()


class SearchTool:
    def __init__(
        self, *, endpoint: str, quota: SearchQuota, client: httpx.AsyncClient | None = None
    ) -> None:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.path != "/search"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("search requires the configured loopback SearXNG endpoint")
        self.endpoint, self.quota = endpoint, quota
        self.client = client or httpx.AsyncClient(trust_env=False, follow_redirects=False)
        self.owns_client = client is None

    def manifest(self) -> ToolManifest:
        return ToolManifest(
            name="web.search",
            description="Search public sources for current or uncertain facts. Cite result URLs.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 500}},
                "required": ["query"],
            },
            action_kind="search_public_web",
            required_scopes=frozenset({"web.search"}),
            allowed_data=frozenset({DataClass.PUBLIC, DataClass.CONVERSATION}),
            timeout_seconds=30,
        )

    async def invoke(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        if "web.search" not in context.granted_scopes:
            return ToolResult(ok=False, error_code="missing_scope")
        ticket = await asyncio.to_thread(self.quota.claim)
        if ticket is None:
            return ToolResult(
                ok=False,
                error_code="search_quota_exceeded",
                content="The configured search quota has been reached.",
            )
        try:
            async with self.client.stream(
                "GET",
                self.endpoint,
                params={
                    "q": arguments["query"],
                    "format": "json",
                    "pageno": 1,
                    "categories": "general",
                    "language": "all",
                    "safesearch": 1,
                },
                timeout=20,
            ) as response:
                response.raise_for_status()
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 256 * 1024:
                        raise ValueError("search response too large")
                body = json.loads(raw)
                rows = body.get("results")
                if not isinstance(rows, list):
                    raise ValueError("invalid search results")
                results = []
                for row in rows[:5]:
                    url = row.get("url", "")
                    if (
                        not isinstance(url, str)
                        or len(url) > 4096
                        or urlsplit(url).scheme != "https"
                    ):
                        continue
                    results.append(
                        {
                            "url": url,
                            "title": str(row.get("title", ""))[:500],
                            "content": str(row.get("content", ""))[:2000],
                        }
                    )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            await asyncio.to_thread(self.quota.release, ticket)
            return ToolResult(ok=False, error_code="search_connect_failed")
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                raise
            return ToolResult(
                ok=False,
                error_code="search_failed",
                content="Search failed; no results were obtained and it was not retried.",
            )
        if not results:
            return ToolResult(ok=False, error_code="search_no_results",
                              content="The configured search engines returned no usable results.")
        return ToolResult(
            ok=True,
            content=json.dumps(results, ensure_ascii=False),
            metadata={
                "result_urls": [r["url"] for r in results],
                "estimated_cost_cny": "0",
                "provider": "local_searxng",
            },
        )

    async def aclose(self) -> None:
        if self.owns_client:
            await self.client.aclose()
