from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

import pytest
from test_two_stage_context import MeteredFake, ModelResponse, related, standalone

from lemonbot.connectors.wechat_agent import CHANNEL
from lemonbot.memory.unified.preflight import MARKER, claim, probe
from lemonbot.models.budget import BudgetLimits, ModelPrice
from lemonbot.models.persistent_budget import PersistentBudgetManager
from lemonbot.storage.migrate import upgrade_database


def test_claim_requires_pause_and_is_durable_once(tmp_path):
    path = tmp_path / "profile.db"
    upgrade_database(path)
    with pytest.raises(ValueError, match="PausedDrained"):
        claim(path)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO runtime_state VALUES (?,?,?)",
            ("pause:channel:" + CHANNEL, '{"paused":true}', "2026-10-15"),
        )
    claim(path)
    with pytest.raises(ValueError, match="AlreadyAttempted"):
        claim(path)
    with sqlite3.connect(path) as db:
        assert json.loads(
            db.execute("SELECT value_json FROM runtime_state WHERE key=?", (MARKER,)).fetchone()[0]
        ) == {"state": "started"}


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid,wrong_model", [(False, False), (True, False), (False, True)])
async def test_preflight_finite_scoped_and_shared_budget(tmp_path, invalid, wrong_model):
    ledger = tmp_path / "real-budget.db"
    budget = await PersistentBudgetManager.create(
        database_path=ledger,
        limits=BudgetLimits(daily=Decimal("5"), monthly=Decimal("100")),
        prices={("deepseek", "deepseek-flash"): ModelPrice(Decimal("1"), Decimal("1"))},
    )
    model = MeteredFake(budget)
    model.classify = lambda request: (
        related(request) if len(model.requests) == 1 else standalone(request)
    )
    model.answer = lambda request: ModelResponse(
        content="wrong" if invalid else ("LIME742" if len(model.requests) == 2 else "15"),
        model="unexpected-model" if wrong_model else "deepseek-flash",
    )
    report = await probe(model, budget, tmp_path)
    assert report["passed"] is (not invalid and not wrong_model)
    assert len(model.requests) == (2 if invalid or wrong_model else 4)
    assert len(report["stage_usage"]) == len(model.requests)
    assert Decimal(report["cost_estimate_cny"]) > 0
    assert (await budget.snapshot()).daily_reserved == 0
    assert all(not request.tools for request in model.requests)
    with sqlite3.connect(ledger) as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='outbox'").fetchone()
    assert all("synthetic-private" in request.messages[-1].content for request in model.requests)
