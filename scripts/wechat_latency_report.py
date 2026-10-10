#!/usr/bin/env python3
"""Report synthetic performance trials without bodies or identity fields."""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from datetime import datetime
from pathlib import Path


def seconds(first: str | None, last: str | None) -> float | None:
    if first is None or last is None:
        return None
    return round((datetime.fromisoformat(last) - datetime.fromisoformat(first)).total_seconds(), 3)


def report(database: Path) -> dict[str, object]:
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT i.id,i.occurred_at,i.created_at AS ingested,i.model_started_at,"
            "json_extract(i.metadata_json,'$.is_group') AS is_group,"
            "CASE WHEN instr(i.text,'性能验收扩容')>0 THEN 'hardware' ELSE 'code' END AS phase,"
            "o.state,o.attempts,o.created_at AS generated,o.dispatch_started_at,o.acknowledged_at "
            "FROM inbox i LEFT JOIN outbox o ON o.channel=i.channel AND o.chat_id=i.chat_id "
            "AND o.reply_to_event_id=i.event_id WHERE i.channel='wechat_agent_lab' "
            "AND instr(i.text,'性能验收')>0 ORDER BY i.id"
        ).fetchall()
        unknown = connection.execute(
            "SELECT count(*) FROM outbox WHERE state='unknown'"
        ).fetchone()[0]
    samples = []
    for row in rows:
        samples.append(
            {
                "sample": len(samples) + 1,
                "phase": row["phase"],
                "conversation_kind": "group" if row["is_group"] else "private",
                "state": row["state"],
                "attempts": row["attempts"],
                "received_at_utc": row["occurred_at"],
                "receive_seconds": seconds(row["occurred_at"], row["ingested"]),
                "before_model_seconds": seconds(row["ingested"], row["model_started_at"]),
                "model_and_commit_seconds": seconds(row["model_started_at"], row["generated"]),
                "dispatch_seconds": seconds(row["dispatch_started_at"], row["acknowledged_at"]),
                "total_seconds": seconds(row["occurred_at"], row["acknowledged_at"]),
            }
        )
    durations = [
        s["total_seconds"]
        for s in samples
        if s["state"] == "acknowledged" and s["total_seconds"] is not None
    ]
    return {
        "evidence": "live_synthetic_trials_only",
        "samples": samples,
        "confirmed_samples": len(durations),
        "median_total_seconds": statistics.median(durations) if durations else None,
        "unknown_outbox": unknown,
        "medians_by_phase": {
            phase: statistics.median(
                [
                    s["total_seconds"]
                    for s in samples
                    if s["phase"] == phase
                    and s["state"] == "acknowledged"
                    and s["total_seconds"] is not None
                ]
            )
            for phase in {
                s["phase"]
                for s in samples
                if s["state"] == "acknowledged" and s["total_seconds"] is not None
            }
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path.home() / ".local/share/Lemonbot-wechat-conversation/lab.db",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    data = report(args.database.resolve())
    rendered = json.dumps(data, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n")
    print(rendered)


if __name__ == "__main__":
    main()
