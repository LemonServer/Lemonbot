"""Read-only stability observation for the foreground canary lab and runtime."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lemonbot.connectors.wechat_agent import (
    CHANNEL,
    AgentWechatConnector,
    AgentWechatHTTP,
    LabPaused,
)
from lemonbot.connectors.wechat_agent_state import (
    DEFAULT_STATE,
    load_binding,
    private_read,
    private_write,
)


def snapshot(database: Path) -> dict[str, Any]:
    # mode=ro prevents a missing database from silently becoming an empty experiment.
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        pauses = [
            json.loads(row[0]).get("paused", False)
            for row in db.execute(
                "SELECT value_json FROM runtime_state WHERE key IN (?, ?)",
                ("pause:global", "pause:channel:" + CHANNEL),
            )
        ]
        return {
            "inbox": dict(db.execute("SELECT state,count(*) FROM inbox GROUP BY state")),
            "outbox": dict(db.execute("SELECT state,count(*) FROM outbox GROUP BY state")),
            "unknown_attempts": [
                row[0]
                for row in db.execute(
                    "SELECT attempts FROM outbox WHERE state='unknown' ORDER BY id",
                )
            ],
            "runtime_paused": any(pauses),
            "model_io_started_count": db.execute(
                "SELECT count(*) FROM inbox WHERE model_started_at IS NOT NULL",
            ).fetchone()[0],
            "tool_execution_rows": db.execute("SELECT count(*) FROM tool_executions").fetchone()[0],
        }


def service_active(unit: str) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.service", unit):
        raise ValueError("InvalidObservationUnit")
    result = subprocess.run(  # noqa: S603 -- fixed executable, validated unit, no shell
        ["/usr/bin/systemctl", "--user", "is-active", unit],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.returncode == 0 and result.stdout.strip() == "active"


async def observe(
    connector: AgentWechatConnector,
    database: Path,
    *,
    pause_file: Path,
    seconds: float,
    interval: float = 30,
    active: Callable[[], bool],
    checkpoint: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    start = asyncio.get_running_loop().time()
    baseline = snapshot(database)
    result: dict[str, Any] = {
        "mode": "live_testing_stability_observation",
        "observer_version": 2,
        "started_at_utc": datetime.now(UTC).isoformat(),
        "required_seconds": seconds,
        "baseline": baseline,
        "samples": 0,
        "elapsed_seconds": 0,
        "passed": False,
        "scope": "foreground single-group canary; no new commands during observation",
    }
    try:
        while True:
            await connector.guard()
            if pause_file.exists() or snapshot(database)["runtime_paused"]:
                raise LabPaused("ObservedChannelPaused")
            if not active():
                raise LabPaused("ObservedServiceNotActive")
            current = snapshot(database)
            if current != baseline:
                raise LabPaused("ObservationSnapshotChanged")
            if current["model_io_started_count"] or current["tool_execution_rows"]:
                raise LabPaused("ObservedModelOrToolActivity")
            if any(
                current["outbox"].get(state, 0) for state in ("pending", "reserved", "dispatching")
            ):
                raise LabPaused("ObservedUnsettledOutbox")
            elapsed = asyncio.get_running_loop().time() - start
            result.update(
                samples=result["samples"] + 1,
                elapsed_seconds=elapsed,
                last_snapshot=current,
                guard_passed=True,
            )
            if elapsed >= seconds:
                result.update(passed=True, completed_at_utc=datetime.now(UTC).isoformat())
            checkpoint(result)
            if result["passed"]:
                return result
            await asyncio.sleep(min(interval, max(0, seconds - elapsed)))
    except LabPaused as error:
        result.update(
            failure_code=str(error), elapsed_seconds=asyncio.get_running_loop().time() - start
        )
        checkpoint(result)
        return result


async def run(args: argparse.Namespace) -> bool:
    state = args.state_dir.expanduser()
    key = private_read(state / "comparison-key")
    if len(key) != 32:
        raise LabPaused("InvalidComparisonKey")
    connector = AgentWechatConnector(
        AgentWechatHTTP(private_read(args.token_file.expanduser()).decode().strip()),
        load_binding(state / "binding.json"),
        key,
    )
    output = state / args.output_name
    minute = -1

    def checkpoint(result: dict[str, Any]) -> None:
        nonlocal minute
        private_write(output, json.dumps(result).encode())
        current_minute = int(result["elapsed_seconds"] // 60)
        if current_minute != minute or result["passed"] or result.get("failure_code"):
            print(
                json.dumps(
                    {
                        key: result.get(key)
                        for key in (
                            "elapsed_seconds",
                            "samples",
                            "passed",
                            "failure_code",
                        )
                    }
                ),
                flush=True,
            )
            minute = current_minute

    try:
        result = await observe(
            connector,
            args.database,
            pause_file=state / "paused.json",
            seconds=args.seconds,
            active=lambda: service_active(args.unit),
            checkpoint=checkpoint,
        )
        return bool(result["passed"])
    finally:
        await connector.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit", required=True)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--output-name", default="stability-observation.json")
    parser.add_argument("--seconds", type=int, default=1800)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.service", args.unit):
        parser.error("InvalidObservationUnit")
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.json", args.output_name):
        parser.error("InvalidObservationFilename")
    if not 1800 <= args.seconds <= 3600:
        parser.error("ObservationMustLastAtLeastThirtyMinutes")
    try:
        passed = asyncio.run(run(args))
    except (LabPaused, OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
        print(json.dumps({"passed": False, "failure_code": "ObservationReadFailed"}))
        raise SystemExit(2) from None
    raise SystemExit(0 if passed else 2)


if __name__ == "__main__":
    main()
