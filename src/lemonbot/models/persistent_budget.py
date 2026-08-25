from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from lemonbot.models.budget import (
    BudgetLimits,
    BudgetManager,
    BudgetReservation,
    BudgetSettlement,
    ModelPrice,
)


class PersistentBudgetManager(BudgetManager):
    """Budget manager with a conservative SQLite write-ahead ledger.

    Reservations left open by a crash are charged at their full reserved value
    during the next startup. This can overcount, but can never silently exceed
    the administrator's hard limit.
    """

    def __init__(
        self,
        *,
        database_path: Path,
        limits: BudgetLimits,
        prices: dict[tuple[str, str], ModelPrice],
        timezone_name: str = "Asia/Shanghai",
        initial_daily_spend: Decimal = Decimal(0),
        initial_monthly_spend: Decimal = Decimal(0),
    ) -> None:
        super().__init__(
            limits=limits,
            prices=prices,
            timezone_name=timezone_name,
            initial_daily_spend=initial_daily_spend,
            initial_monthly_spend=initial_monthly_spend,
        )
        self._database_path = database_path.resolve()
        self._ledger_lock = asyncio.Lock()

    @classmethod
    async def create(
        cls,
        *,
        database_path: Path,
        limits: BudgetLimits,
        prices: dict[tuple[str, str], ModelPrice],
        timezone_name: str = "Asia/Shanghai",
    ) -> PersistentBudgetManager:
        database_path = await asyncio.to_thread(database_path.resolve)
        await asyncio.to_thread(database_path.parent.mkdir, parents=True, exist_ok=True)
        daily, monthly = await asyncio.to_thread(
            cls._initialize_and_recover,
            database_path,
            timezone_name,
        )
        return cls(
            database_path=database_path,
            limits=limits,
            prices=prices,
            timezone_name=timezone_name,
            initial_daily_spend=daily,
            initial_monthly_spend=monthly,
        )

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @classmethod
    def _initialize_and_recover(cls, path: Path, timezone_name: str) -> tuple[Decimal, Decimal]:
        from zoneinfo import ZoneInfo

        now = datetime.now(UTC).astimezone(ZoneInfo(timezone_name))
        day_key = now.date().isoformat()
        month_key = f"{now.year:04d}-{now.month:02d}"
        with closing(cls._connect(path)) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS model_budget_ledger (
                    reservation_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    day_key TEXT NOT NULL,
                    month_key TEXT NOT NULL,
                    reserved_cny TEXT NOT NULL,
                    charged_cny TEXT,
                    state TEXT NOT NULL CHECK(state IN ('reserved','released','settled','unknown')),
                    created_at TEXT NOT NULL,
                    settled_at TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_model_budget_day
                    ON model_budget_ledger(day_key, state);
                CREATE INDEX IF NOT EXISTS ix_model_budget_month
                    ON model_budget_ledger(month_key, state);
                """
            )
            recovery_time = datetime.now(UTC).isoformat()
            connection.execute(
                """
                UPDATE model_budget_ledger
                SET state='unknown', charged_cny=reserved_cny, settled_at=?
                WHERE state='reserved'
                """,
                (recovery_time,),
            )
            daily_rows = connection.execute(
                """
                SELECT charged_cny
                FROM model_budget_ledger
                WHERE day_key=? AND state IN ('settled','unknown')
                """,
                (day_key,),
            ).fetchall()
            monthly_rows = connection.execute(
                """
                SELECT charged_cny
                FROM model_budget_ledger
                WHERE month_key=? AND state IN ('settled','unknown')
                """,
                (month_key,),
            ).fetchall()
            connection.commit()
        daily = sum((Decimal(row[0]) for row in daily_rows), start=Decimal(0))
        monthly = sum((Decimal(row[0]) for row in monthly_rows), start=Decimal(0))
        return daily, monthly

    async def reserve(self, **kwargs) -> BudgetReservation:  # type: ignore[no-untyped-def]
        reservation = await super().reserve(**kwargs)
        try:
            async with self._ledger_lock:
                await asyncio.to_thread(self._insert_reservation, reservation)
        except BaseException:
            await super().release(reservation.reservation_id)
            raise
        return reservation

    def _insert_reservation(self, reservation: BudgetReservation) -> None:
        with closing(self._connect(self._database_path)) as connection:
            connection.execute(
                """
                INSERT INTO model_budget_ledger(
                    reservation_id, provider, model, day_key, month_key,
                    reserved_cny, state, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?)
                """,
                (
                    reservation.reservation_id,
                    reservation.provider,
                    reservation.model,
                    reservation.day_key,
                    reservation.month_key,
                    str(reservation.amount),
                    reservation.created_at.isoformat(),
                ),
            )
            connection.commit()

    async def release(self, reservation_id: str) -> None:
        await super().release(reservation_id)
        await self._record_settlement(reservation_id, "released", Decimal(0))

    async def settle(self, reservation_id: str, **kwargs) -> BudgetSettlement:  # type: ignore[no-untyped-def]
        settlement = await super().settle(reservation_id, **kwargs)
        await self._record_settlement(reservation_id, "settled", settlement.charged)
        return settlement

    async def settle_unknown(self, reservation_id: str) -> BudgetSettlement:
        settlement = await super().settle_unknown(reservation_id)
        await self._record_settlement(reservation_id, "unknown", settlement.charged)
        return settlement

    async def _record_settlement(self, reservation_id: str, state: str, charged: Decimal) -> None:
        async with self._ledger_lock:
            await asyncio.to_thread(
                self._update_settlement,
                reservation_id,
                state,
                charged,
            )

    def _update_settlement(self, reservation_id: str, state: str, charged: Decimal) -> None:
        with closing(self._connect(self._database_path)) as connection:
            cursor = connection.execute(
                """
                UPDATE model_budget_ledger
                SET state=?, charged_cny=?, settled_at=?
                WHERE reservation_id=? AND state='reserved'
                """,
                (state, str(charged), datetime.now(UTC).isoformat(), reservation_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("budget ledger transition was not applied exactly once")
            connection.commit()
