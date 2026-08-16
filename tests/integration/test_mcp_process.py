from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from lemonbot.domain import ToolContext
from lemonbot.tools.mcp import MCPStdioClient, MCPToolAdapter, PinnedMCPServer, PinnedMCPTool


async def test_pinned_mcp_process_round_trip_and_shutdown(tmp_path: Path) -> None:
    server_script = tmp_path / "fake_mcp_server.py"
    server_script.write_text(
        """
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    if request["method"] == "initialize":
        result = {
            "protocolVersion": "2025-03-26",
            "serverInfo": {"name": "fixture-mcp", "version": "1.0.0"},
        }
    elif request["method"] == "tools/call":
        result = {"content": [{"type": "text", "text": "fixture result"}]}
    else:
        result = {"isError": True}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
""".lstrip(),
        encoding="utf-8",
    )
    executable = Path(sys.executable).resolve(strict=True)
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    server = PinnedMCPServer(
        name="fixture",
        enabled=True,
        executable=executable,
        arguments=("-I", "-u", str(server_script)),
        executable_sha256=digest,
        working_directory=tmp_path,
        expected_server_name="fixture-mcp",
        expected_server_version="1.0.0",
        max_processes=2,
        tools={
            "lookup": PinnedMCPTool(
                remote_name="fixture.lookup",
                description="Return a fixed integration-test result.",
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
                enabled=True,
            )
        },
    )
    client = MCPStdioClient(server)
    await client.start()
    adapter = MCPToolAdapter(client, "lookup")
    manifest = adapter.manifest()
    try:
        result = await adapter.invoke(
            ToolContext(
                profile="prod",
                channel="wecom",
                chat_id="chat-1",
                event_id="event-1",
                granted_scopes=manifest.required_scopes,
            ),
            {"query": "safe"},
        )
        assert result.ok
        assert "fixture result" in result.content
        assert result.metadata["untrusted"] is True
    finally:
        worker_process = client._process
        await client.close()

    assert worker_process is not None
    assert worker_process.returncode is not None
