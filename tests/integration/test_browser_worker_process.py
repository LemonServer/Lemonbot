from __future__ import annotations

import sys
from pathlib import Path

from lemonbot.domain import DataClass, ToolContext
from lemonbot.supervisor import WorkerSupervisor
from lemonbot.tools.browser_worker_protocol import BrowserWorkerConfig
from lemonbot.tools.browser_worker_proxy import IsolatedBrowserReadTool


async def test_isolated_browser_worker_rejects_private_target_without_playwright(
    tmp_path: Path,
) -> None:
    supervisor = WorkerSupervisor()
    tool = await IsolatedBrowserReadTool.create(
        config=BrowserWorkerConfig(max_text_chars=10_000, timeout_seconds=5),
        supervisor=supervisor,
        python_executable=Path(sys.executable),
        cwd=tmp_path,
    )
    try:
        result = await tool.invoke(
            ToolContext(
                profile="lab",
                channel="fake",
                chat_id="chat-1",
                event_id="event-1",
                granted_scopes=frozenset({"browser.read_public"}),
                data_class=DataClass.PUBLIC,
            ),
            {"url": "https://127.0.0.1/private"},
        )
        assert not result.ok
        assert result.error_code == "unsafe_url"
    finally:
        await tool.aclose()
        await supervisor.stop_all()
