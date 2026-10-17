"""Finite paid stage-8 preflight: synthetic archive, real shared budget, no connector."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from lemonbot.config import RuntimePaths
from lemonbot.config.settings import load_settings
from lemonbot.connectors.wechat_agent import CHANNEL
from lemonbot.domain import InboundEvent, ModelBackend, ModelMessage, ModelRequest
from lemonbot.models.budget import BudgetManager
from lemonbot.runtime import LemonbotRuntime
from lemonbot.runtime_lock import RuntimeLock
from lemonbot.storage.migrate import upgrade_database

from .archive import SQLiteMessageArchive
from .assembly import VerifiedContextAssembler
from .flow import TwoStageContextFlow
from .index import SQLiteRetrievalIndex
from .maintenance import UnifiedMemoryStore
from .models import ContextBundle, ConversationScope, Message, MessagePart
from .retrieval import HybridContextRetriever

MARKER = "context:stage8-preflight:v1"


def claim(path: Path) -> None:
    """Durable at-most-once guard, after RuntimeLock, before initializing the provider."""
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("PreflightRequiresExistingIndependentDatabase")
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        paused = any(
            json.loads(r[0]).get("paused")
            for r in db.execute(
                "SELECT value_json FROM runtime_state WHERE key IN (?,?)",
                ("pause:global", "pause:channel:" + CHANNEL),
            )
        )
        busy = db.execute(
            "SELECT (SELECT count(*) FROM inbox WHERE state IN ('pending','processing')) + "
            "(SELECT count(*) FROM outbox WHERE state IN ('pending','reserved','dispatching'))"
        ).fetchone()[0]
        if not paused or busy:
            raise ValueError("PreflightRequiresPausedDrainedProfile")
        if db.execute("SELECT 1 FROM runtime_state WHERE key=?", (MARKER,)).fetchone():
            raise ValueError("PreflightAlreadyAttemptedNoAutomaticRetry")
        db.execute(
            "INSERT INTO runtime_state(key,value_json,updated_at) VALUES (?,?,?)",
            (MARKER, '{"state":"started"}', datetime.now(UTC).isoformat()),
        )
        db.commit()


async def probe(model: ModelBackend, budget: BudgetManager, root: Path) -> dict[str, Any]:
    """Exactly two turns / at most four generations; never dispatch or use private evidence."""
    path = root / "synthetic.db"
    upgrade_database(path)
    archive = SQLiteMessageArchive(path)
    scope = ConversationScope(
        channel="synthetic",
        account_id="preflight",
        conversation_id="synthetic-private",
        kind="private",
    )
    now = datetime.now(UTC)
    history = Message(
        scope=scope,
        message_id="history",
        event_id="history",
        sender_id="alice",
        direction="inbound",
        occurred_at=now - timedelta(minutes=1),
        parts=(MessagePart(part_id="text", kind="text", text="合成验收项目的代号是 LIME742。"),),
    )
    await archive.put_message(history)
    retriever = HybridContextRetriever(archive, SQLiteRetrievalIndex(root / "index.db", path))
    flow = TwoStageContextFlow(
        archive=archive,
        retriever=retriever,
        assembler=VerifiedContextAssembler(archive),
        model=model,
        budget=budget,
        scopes={scope.conversation_id: scope},
        system_prompt="简短准确地回答用户问题，遵守用户的输出格式。",
        memories=UnifiedMemoryStore(archive),
    )
    results: list[dict[str, Any]] = []

    async def before_call() -> None:
        return None  # Profile lock held; this synthetic run has no sending/receiving route.

    async def answer(messages: list[ModelMessage], bundle: ContextBundle) -> Any:
        return await flow.generate(
            ModelRequest(messages=tuple(messages), max_tokens=512, temperature=0, deep=False)
        )

    before = await budget.snapshot()
    try:
        for identifier, question, expected, expected_mode in (
            ("related", "刚才说的合成验收项目代号是什么？只输出代号。", "LIME742", "related"),
            ("independent", "计算 7+8，只输出阿拉伯数字。", "15", "standalone"),
        ):
            current = Message(
                scope=scope,
                message_id=identifier,
                event_id=identifier,
                sender_id="alice",
                direction="inbound",
                occurred_at=now,
                parts=(MessagePart(part_id="text", kind="text", text=question),),
            )
            await archive.put_message(current)
            event = InboundEvent(
                channel=scope.channel,
                chat_id=scope.conversation_id,
                event_id=identifier,
                sender_id="alice",
                text=question,
                occurred_at=now,
            )
            started = time.perf_counter()
            response = await flow.run(event, answer=answer, before_call=before_call)
            bundle = flow.last_bundle
            results.append(
                {
                    "case": identifier,
                    "mode": None if bundle is None else bundle.decision.mode,
                    "fallback": flow.last_fallback,
                    "model": response.model,
                    "answer_matches": (response.content or "").strip() == expected,
                    "classification_budget_tokens": flow.classification_usage,
                    "answer_prompt_tokens": response.prompt_tokens,
                    "answer_completion_tokens": response.completion_tokens,
                    "answer_cached_prompt_tokens": response.cached_prompt_tokens,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                }
            )
            if (
                bundle is None
                or bundle.decision.mode != expected_mode
                or flow.last_fallback
                or not results[-1]["answer_matches"]
                or response.model != "deepseek-flash"
            ):
                break
        after = await budget.snapshot()
        with closing(sqlite3.connect(path)) as db:
            db.row_factory = sqlite3.Row
            usage = [
                dict(row)
                for row in db.execute(
                    "SELECT stage,elapsed_ms,prompt_tokens,completion_tokens,cached_tokens,"
                    "estimated_cny,outcome FROM unified_stage_usage ORDER BY rowid"
                )
            ]
        return {
            "mode": "real_api_synthetic_context_no_wechat",
            "cases": results,
            "passed": len(results) == 2
            and all(
                r["answer_matches"]
                and r["model"] == "deepseek-flash"
                and r["fallback"] is None
                and r["mode"] == ("related" if r["case"] == "related" else "standalone")
                for r in results
            ),
            "cost_estimate_cny": str(after.daily_spent - before.daily_spent),
            "daily_spent_cny": str(after.daily_spent),
            "requests_maximum": 4,
            "stage_usage": usage,
        }
    finally:
        await retriever.close()


async def execute(config: Path) -> dict[str, Any]:
    settings = load_settings(config)
    if (
        settings.models.provider != "deepseek"
        or settings.models.flash_model != "deepseek-flash"
        or not settings.models.budget.enabled
        or settings.models.budget.daily_limit_cny > 5
        or settings.models.budget.monthly_limit_cny > 100
    ):
        raise ValueError("PreflightRequiresBoundedDeepSeekFlash")
    paths = RuntimePaths.from_settings(settings)
    with RuntimeLock(paths.lock_file):
        claim(paths.database)
        runtime = LemonbotRuntime(settings)
        report: dict[str, Any] = {"passed": False, "state": "unknown_no_retry"}
        try:
            # Only the existing model factory: no initialize(), migration, connector or tool.
            model = await runtime._build_model()
            assert runtime._budget is not None
            with TemporaryDirectory(prefix="lemonbot-preflight-") as directory:
                report = await probe(model, runtime._budget, Path(directory))
            return report
        finally:
            try:
                await runtime.close()
            finally:
                with closing(sqlite3.connect(paths.database)) as db:
                    db.execute("PRAGMA synchronous=FULL")
                    db.execute(
                        "UPDATE runtime_state SET value_json=?,updated_at=? WHERE key=?",
                        (json.dumps(report), datetime.now(UTC).isoformat(), MARKER),
                    )
                    db.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--confirm-paid-preflight", action="store_true", required=True)
    args = parser.parse_args()
    try:
        report = asyncio.run(execute(args.config))
    except Exception as error:
        print(json.dumps({"passed": False, "error": type(error).__name__, "retry": False}))
        raise SystemExit(1) from None
    print(json.dumps(report, ensure_ascii=False))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
