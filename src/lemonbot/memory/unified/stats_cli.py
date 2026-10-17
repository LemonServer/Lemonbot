"""Local aggregate maintenance accounting; read-only, no private text or cloud calls."""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from typing import Any


def statistics(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve(strict=True)
    stages: dict[str, dict[str, Any]] = {}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        for stage, elapsed, prompt, completion, cached, cost, outcome in db.execute(
            "SELECT stage,elapsed_ms,prompt_tokens,completion_tokens,cached_tokens,"
            "estimated_cny,outcome FROM unified_stage_usage"
        ):
            item = stages.setdefault(
                stage,
                {
                    "calls": 0,
                    "elapsed_ms": 0.0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "cached_tokens": 0,
                    "uncached_tokens": 0,
                    "estimated_cny": Decimal(0),
                    "uncertain_calls": 0,
                },
            )
            item["calls"] += 1
            item["elapsed_ms"] += elapsed
            item["prompt_tokens"] += prompt
            item["completion_tokens"] += completion
            item["cached_tokens"] += cached
            item["uncached_tokens"] += prompt - cached
            item["estimated_cny"] += Decimal(cost)
            item["uncertain_calls"] += outcome == "unknown"
        jobs = dict(db.execute("SELECT state,count(*) FROM unified_jobs GROUP BY state"))
    for item in stages.values():
        item["estimated_cny"] = str(item["estimated_cny"])
    return {"stages": stages, "jobs": jobs, "accounting": "local_estimate_not_provider_bill"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = statistics(args.database)
    except (OSError, sqlite3.Error):
        parser.exit(2, "Unified statistics unavailable; existing migrated database required.\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
