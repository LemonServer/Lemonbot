#!/usr/bin/env python3
"""Pause, wait for idle, restart the local bot and print this startup's whitelist report.

Does not restart WeChat or automatically resume a persistent pause.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from lemonbot.config import RuntimePaths
from lemonbot.config.paths import default_wechat_runtime_config
from lemonbot.config.settings import load_settings, resolve_config_path

UNIT = "lemonbot-wechat-conversation.service"


def command(*args: str, empty_allowed: bool = False) -> str:
    result = subprocess.run(  # noqa: S603 -- fixed local commands, no shell
        list(args), check=False, capture_output=True, text=True, timeout=100
    )
    if result.returncode and not (empty_allowed and result.returncode == 1):
        raise RuntimeError("LocalCommandFailed")
    return result.stdout


def startup_report(invocation: str) -> str:
    # Some desktop journals contain the user unit only in the system journal.
    # Scope by this user's UID, exact unit and this invocation, never old startup.
    return command(
        "journalctl",
        "_UID=" + str(os.getuid()),
        "_SYSTEMD_USER_UNIT=" + UNIT,
        "_SYSTEMD_INVOCATION_ID=" + invocation,
        "--no-pager",
        "-o",
        "cat",
        "-g",
        "^wechat_startup ",
        empty_allowed=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=default_wechat_runtime_config(),
    )
    parser.add_argument(
        "--stop", action="store_true", help="Pause, drain and stop without restarting."
    )
    args = parser.parse_args()
    args.config = resolve_config_path(args.config)
    if not args.config.is_file():
        print(
            f"RuntimeConfigMissing: {args.config}; use --config or migrate to config/lemonbot.toml."
        )
        raise SystemExit(1)
    settings = load_settings(args.config)
    paths = RuntimePaths.from_settings(settings)
    state = command(
        "systemctl", "--user", "show", UNIT, "--property=ActiveState", "--value"
    ).strip()
    if state not in {"inactive", "failed"}:
        command(
            sys.executable,
            str(Path(__file__).with_name("wechat_conversation_control.py")),
            "pause",
            "--config",
            str(args.config),
        )
    deadline = time.monotonic() + 90
    while True:
        if not paths.database.exists():
            break
        with sqlite3.connect(paths.database.as_uri() + "?mode=ro", uri=True) as db:
            active = db.execute("SELECT count(*) FROM inbox WHERE state='processing'").fetchone()[0]
            active += db.execute(
                "SELECT count(*) FROM outbox WHERE state NOT IN "
                "('acknowledged','unknown','failed','cancelled')"
            ).fetchone()[0]
        if not active:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("InFlightTasksRestartCancelled")
        time.sleep(1)
    if args.stop:
        command("systemctl", "--user", "stop", UNIT)
        print("Bot stopped after draining; persistent pause and all data preserved.")
        return
    loaded = command("systemctl", "--user", "show", UNIT, "--property=LoadState", "--value").strip()
    if loaded == "not-found":
        repo = Path(__file__).resolve().parents[1]
        command(
            "systemd-run",
            "--user",
            "--unit=" + UNIT,
            "--property=UMask=0077",
            "--property=Restart=no",
            "--working-directory=" + str(repo),
            str(repo / ".venv/bin/python"),
            "-m",
            "lemonbot",
            "run",
            "--config",
            str(args.config),
        )
    else:
        command("systemctl", "--user", "restart", UNIT)
    invocation = command(
        "systemctl", "--user", "show", UNIT, "--property=InvocationID", "--value"
    ).strip()
    if (
        not invocation
        or len(invocation) != 32
        or any(c not in "0123456789abcdef" for c in invocation)
    ):
        raise RuntimeError("StartupInvocationUnavailable")
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        report = startup_report(invocation)
        if '"summary": true' in report:
            print(report, end="", flush=True)
            if '"startup_failed": true' in report:
                print("启动失败；修正白名单后重新启动，resume 不能代替配置同步。")
                raise SystemExit(1)
            print("暂停仍保留；确认报告后执行 wechat_conversation_control.py resume。")
            return
        if (
            command(
                "systemctl", "--user", "show", UNIT, "--property=ActiveState", "--value"
            ).strip()
            == "failed"
        ):
            raise RuntimeError("StartupFailedCheckServiceJournal")
        time.sleep(1)
    raise RuntimeError("StartupReportTimeoutCheckServiceJournal")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("重启未完成：" + type(error).__name__ + "; 请检查服务状态及日志。", file=sys.stderr)
        raise SystemExit(1) from None
