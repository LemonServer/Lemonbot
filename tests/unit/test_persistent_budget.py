from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from lemonbot.models import BudgetLimits, ModelPrice, PersistentBudgetManager


async def test_crashed_reservation_is_conservatively_charged(tmp_path: Path) -> None:
    database = tmp_path / "budget.db"
    price = ModelPrice(input_per_million=Decimal("2"), output_per_million=Decimal("4"))
    kwargs = {
        "database_path": database,
        "limits": BudgetLimits(daily=Decimal("1"), monthly=Decimal("10")),
        "prices": {("deepseek", "deepseek-v4-flash"): price},
    }
    first = await PersistentBudgetManager.create(**kwargs)  # type: ignore[arg-type]
    reservation = await first.reserve(
        provider="deepseek",
        model="deepseek-v4-flash",
        prompt_tokens=1000,
        maximum_completion_tokens=1000,
    )

    recovered = await PersistentBudgetManager.create(**kwargs)  # type: ignore[arg-type]
    snapshot = await recovered.snapshot()
    assert snapshot.daily_spent == reservation.amount
    assert snapshot.monthly_spent == reservation.amount


async def test_settled_usage_survives_restart_exactly(tmp_path: Path) -> None:
    database = tmp_path / "budget.db"
    price = ModelPrice(input_per_million=Decimal("1.25"), output_per_million=Decimal("3.5"))
    prices = {("deepseek", "deepseek-v4-flash"): price}
    manager = await PersistentBudgetManager.create(
        database_path=database,
        limits=BudgetLimits(daily=Decimal("1"), monthly=Decimal("10")),
        prices=prices,
    )
    reservation = await manager.reserve(
        provider="deepseek",
        model="deepseek-v4-flash",
        prompt_tokens=1234,
        maximum_completion_tokens=500,
    )
    settlement = await manager.settle(
        reservation.reservation_id,
        prompt_tokens=1000,
        completion_tokens=250,
    )
    recovered = await PersistentBudgetManager.create(
        database_path=database,
        limits=BudgetLimits(daily=Decimal("1"), monthly=Decimal("10")),
        prices=prices,
    )
    assert (await recovered.snapshot()).daily_spent == settlement.charged
