#!/usr/bin/env python3
"""Offline release consistency: versions, local Markdown targets, disabled defaults."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from urllib.parse import unquote

from lemonbot import __version__
from lemonbot.config.settings import load_settings

ROOT = Path(__file__).resolve().parents[1]


def checks() -> list[str]:
    failures = []
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    versions = [project["version"], __version__]
    versions += [p["version"] for p in lock["package"] if p["name"] == "lemonbot"]
    if versions != ["1.0.0"] * 3:
        failures.append("VersionMismatch")
    for name in (
        "LICENSE",
        "AGENTS.md",
        "CONTRIBUTING.md",
        "CHANGELOG.md",
        "THIRD_PARTY_NOTICES.md",
        "docs/deployment.md",
        "docs/operations.md",
    ):
        if not (ROOT / name).is_file():
            failures.append("Missing:" + name)
    for path in [
        ROOT / "README.md",
        ROOT / "AGENTS.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "CHANGELOG.md",
        ROOT / "THIRD_PARTY_NOTICES.md",
        ROOT / "PLAN.md",
        ROOT / "PLAN_linux.md",
        *sorted((ROOT / "docs").rglob("*.md")),
    ]:
        for target in re.findall(r"\]\(([^)]+)\)", path.read_text()):
            target = target.strip("<>").split("#", 1)[0]
            if not target or ":" in target or target.startswith("/"):
                continue
            if not (path.parent / unquote(target)).exists():
                failures.append(f"BrokenLink:{path.relative_to(ROOT)}:{target}")
    config = load_settings(ROOT / "config/wechat-agent-conversation.example.toml")
    if config.runtime.connector != "fake" or config.wechat_agent.enabled:
        failures.append("PublicConfigEnabled")
    return failures


if __name__ == "__main__":
    failures = checks()
    print(json.dumps({"ok": not failures, "failures": failures}, ensure_ascii=False))
    raise SystemExit(bool(failures))
