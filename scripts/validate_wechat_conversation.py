#!/usr/bin/env python3
"""Run repository checks once, clearly separating simulation from live acceptance."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> None:
    repo = Path(__file__).resolve().parents[1]
    commands = [
        ("自动化测试（包含模拟 HTTP；不代表实机通过）", [sys.executable, "-m", "pytest", "-q"]),
        (
            "Ruff",
            [
                str(repo / ".venv/bin/ruff"),
                "check",
                "src",
                "tests",
                "scripts/agent_wechat_metadata.py",
                "scripts/agent_wechat_conversation_env.py",
                "scripts/wechat_conversation_control.py",
                "scripts/validate_wechat_conversation.py",
            ],
        ),
        ("mypy", [str(repo / ".venv/bin/mypy"), "src/lemonbot"]),
        ("锁文件", ["uv", "lock", "--check"]),
    ]
    print("| 检查 | 结果 |\n|---|---|", flush=True)
    failed = False
    for name, command in commands:
        result = subprocess.run(  # noqa: S603 -- fixed repository validation commands
            command, cwd=repo, text=True, capture_output=True, timeout=600
        )
        failed = failed or result.returncode != 0
        print(f"| {name} | {'通过' if result.returncode == 0 else '失败'} |", flush=True)
        if result.returncode:
            print(result.stdout + result.stderr, flush=True)
        elif name.startswith("自动化"):
            print(result.stdout.strip().splitlines()[-1], flush=True)
    print("实机群/私聊、真实 @ 和模型上下文验收另行记录。", flush=True)
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
