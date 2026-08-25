"""Concurrency-safe monetary reservation for cloud model calls."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Final
from uuid import uuid4
from zoneinfo import ZoneInfo

MILLION: Final = Decimal(1_000_000)


class BudgetError(RuntimeError):
    """Base error for a model spend policy failure."""


class PriceNotConfiguredError(BudgetError):
    """Cloud calls are fail-closed until an administrator supplies prices."""


class BudgetExceededError(BudgetError):
    """Raised before an API call when its worst-case reservation does not fit."""


@dataclass(frozen=True, slots=True)
class ModelPrice:
    input_per_million: Decimal
    output_per_million: Decimal
    cached_input_per_million: Decimal | None = None

    def __post_init__(self) -> None:
        if self.input_per_million < 0 or self.output_per_million < 0:
            raise ValueError("model prices cannot be negative")
        if self.cached_input_per_million is not None and self.cached_input_per_million < 0:
            raise ValueError("cached-input price cannot be negative")

    def cost(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        cached_prompt_tokens: int = 0,
    ) -> Decimal:
        if min(prompt_tokens, completion_tokens, cached_prompt_tokens) < 0:
            raise ValueError("token counts cannot be negative")
        cached = min(prompt_tokens, cached_prompt_tokens)
        uncached = prompt_tokens - cached
        cached_price = self.cached_input_per_million or self.input_per_million
        return (
            Decimal(uncached) * self.input_per_million
            + Decimal(cached) * cached_price
            + Decimal(completion_tokens) * self.output_per_million
        ) / MILLION


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    daily: Decimal
    monthly: Decimal

    def __post_init__(self) -> None:
        if self.daily <= 0 or self.monthly <= 0:
            raise ValueError("daily and monthly budgets must be positive")
        if self.daily > self.monthly:
            raise ValueError("daily budget cannot exceed monthly budget")


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    reservation_id: str
    provider: str
    model: str
    amount: Decimal
    day_key: str
    month_key: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class BudgetSettlement:
    reservation_id: str
    reserved: Decimal
    charged: Decimal
    over_reservation: bool


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    day_key: str
    month_key: str
    daily_spent: Decimal
    monthly_spent: Decimal
    daily_reserved: Decimal
    monthly_reserved: Decimal


class BudgetManager:
    """Reserve worst-case call cost, then replace it with actual provider usage.

    This implementation owns live process state.  A persistence adapter can
    snapshot settlements to the core audit repository; callers must still
    create a fresh manager from persisted totals after restart.
    """

    def __init__(
        self,
        *,
        limits: BudgetLimits,
        prices: dict[tuple[str, str], ModelPrice],
        timezone_name: str = "Asia/Shanghai",
        initial_daily_spend: Decimal = Decimal(0),
        initial_monthly_spend: Decimal = Decimal(0),
    ) -> None:
        if initial_daily_spend < 0 or initial_monthly_spend < 0:
            raise ValueError("initial spend cannot be negative")
        self._limits = limits
        self._prices = MappingProxyType(dict(prices))
        self._timezone = ZoneInfo(timezone_name)
        self._daily_spend: dict[str, Decimal] = {}
        self._monthly_spend: dict[str, Decimal] = {}
        now = self._now()
        self._daily_spend[self._day_key(now)] = initial_daily_spend
        self._monthly_spend[self._month_key(now)] = initial_monthly_spend
        self._reservations: dict[str, BudgetReservation] = {}
        self._lock = asyncio.Lock()

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def _day_key(self, at: datetime) -> str:
        return at.astimezone(self._timezone).date().isoformat()

    def _month_key(self, at: datetime) -> str:
        local = at.astimezone(self._timezone)
        return f"{local.year:04d}-{local.month:02d}"

    def price_for(self, provider: str, model: str) -> ModelPrice:
        try:
            return self._prices[(provider, model)]
        except KeyError as exc:
            raise PriceNotConfiguredError(
                f"no administrator-approved price for {provider}/{model}"
            ) from exc

    async def reserve(
        self,
        *,
        provider: str,
        model: str,
        prompt_tokens: int,
        maximum_completion_tokens: int,
        at: datetime | None = None,
    ) -> BudgetReservation:
        if prompt_tokens < 0 or maximum_completion_tokens < 0:
            raise ValueError("token counts cannot be negative")
        price = self.price_for(provider, model)
        amount = price.cost(
            prompt_tokens=prompt_tokens,
            completion_tokens=maximum_completion_tokens,
        )
        now = at or self._now()
        if now.tzinfo is None:
            raise ValueError("reservation timestamp must include a timezone")
        day_key = self._day_key(now)
        month_key = self._month_key(now)

        async with self._lock:
            daily_reserved = sum(
                item.amount for item in self._reservations.values() if item.day_key == day_key
            )
            monthly_reserved = sum(
                item.amount for item in self._reservations.values() if item.month_key == month_key
            )
            projected_day = self._daily_spend.get(day_key, Decimal(0)) + daily_reserved + amount
            projected_month = (
                self._monthly_spend.get(month_key, Decimal(0)) + monthly_reserved + amount
            )
            if projected_day > self._limits.daily:
                raise BudgetExceededError("daily model API budget would be exceeded")
            if projected_month > self._limits.monthly:
                raise BudgetExceededError("monthly model API budget would be exceeded")
            reservation = BudgetReservation(
                reservation_id=str(uuid4()),
                provider=provider,
                model=model,
                amount=amount,
                day_key=day_key,
                month_key=month_key,
                created_at=now.astimezone(UTC),
            )
            self._reservations[reservation.reservation_id] = reservation
            return reservation

    async def release(self, reservation_id: str) -> None:
        """Release a call proven not to have reached the provider."""

        async with self._lock:
            if self._reservations.pop(reservation_id, None) is None:
                raise KeyError(f"unknown budget reservation {reservation_id!r}")

    async def settle(
        self,
        reservation_id: str,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        cached_prompt_tokens: int = 0,
    ) -> BudgetSettlement:
        async with self._lock:
            try:
                reservation = self._reservations.pop(reservation_id)
            except KeyError as exc:
                raise KeyError(f"unknown budget reservation {reservation_id!r}") from exc
            charged = self.price_for(reservation.provider, reservation.model).cost(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cached_prompt_tokens=cached_prompt_tokens,
            )
            self._daily_spend[reservation.day_key] = (
                self._daily_spend.get(reservation.day_key, Decimal(0)) + charged
            )
            self._monthly_spend[reservation.month_key] = (
                self._monthly_spend.get(reservation.month_key, Decimal(0)) + charged
            )
            return BudgetSettlement(
                reservation_id=reservation_id,
                reserved=reservation.amount,
                charged=charged,
                over_reservation=charged > reservation.amount,
            )

    async def settle_unknown(self, reservation_id: str) -> BudgetSettlement:
        """Conservatively charge the entire reservation when usage is unknown."""

        async with self._lock:
            try:
                reservation = self._reservations.pop(reservation_id)
            except KeyError as exc:
                raise KeyError(f"unknown budget reservation {reservation_id!r}") from exc
            self._daily_spend[reservation.day_key] = (
                self._daily_spend.get(reservation.day_key, Decimal(0)) + reservation.amount
            )
            self._monthly_spend[reservation.month_key] = (
                self._monthly_spend.get(reservation.month_key, Decimal(0)) + reservation.amount
            )
            return BudgetSettlement(
                reservation_id=reservation_id,
                reserved=reservation.amount,
                charged=reservation.amount,
                over_reservation=False,
            )

    async def snapshot(self, *, at: datetime | None = None) -> BudgetSnapshot:
        now = at or self._now()
        day_key = self._day_key(now)
        month_key = self._month_key(now)
        async with self._lock:
            daily_reserved = sum(
                (item.amount for item in self._reservations.values() if item.day_key == day_key),
                Decimal(0),
            )
            monthly_reserved = sum(
                (
                    item.amount
                    for item in self._reservations.values()
                    if item.month_key == month_key
                ),
                Decimal(0),
            )
            return BudgetSnapshot(
                day_key=day_key,
                month_key=month_key,
                daily_spent=self._daily_spend.get(day_key, Decimal(0)),
                monthly_spent=self._monthly_spend.get(month_key, Decimal(0)),
                daily_reserved=daily_reserved,
                monthly_reserved=monthly_reserved,
            )
