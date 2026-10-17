"""Task-local two-call preallocation using the existing durable reservation ledger."""

from __future__ import annotations

import asyncio
from collections import deque
from contextvars import ContextVar, Token
from datetime import datetime
from typing import Any

from .budget import BudgetError, BudgetManager, BudgetReservation

_ACTIVE: ContextVar[PrepaidPair | None] = ContextVar("lemonbot_prepaid_pair", default=None)


async def claim_prepaid(
    budget: BudgetManager,
    *,
    provider: str,
    model: str,
    prompt_tokens: int,
    maximum_completion_tokens: int,
    at: datetime | None = None,
) -> BudgetReservation | None:
    pair = _ACTIVE.get()
    if pair is None or pair.budget is not budget or pair.owner is not asyncio.current_task():
        return None
    if not pair.pending or (pair.provider, pair.model) != (provider, model):
        return None
    reservation = pair.pending[0]
    async with budget._lock:
        now = at or budget._now()
        if (
            min(prompt_tokens, maximum_completion_tokens) < 0
            or prompt_tokens > pair.input_tokens
            or maximum_completion_tokens > pair.output_tokens
            or budget._reservations.get(reservation.reservation_id) != reservation
            or reservation.day_key != budget._day_key(now)
            or reservation.month_key != budget._month_key(now)
            or budget.price_for(provider, model).cost(
                prompt_tokens=prompt_tokens,
                completion_tokens=maximum_completion_tokens,
            )
            > reservation.amount
        ):
            raise BudgetError("prepaid model call exceeds its bound reservation")
        pair.pending.popleft()
        return reservation


class PrepaidPair:
    """Reserve both stages before I/O; existing backends consume and settle once.

    Only this task and this manager can consume the allowance. Tool continuations
    beyond the first answer request use ordinary per-call reservations. No IDs or
    monetary capability are serialized into prompts or IPC.
    """

    def __init__(
        self,
        budget: BudgetManager,
        *,
        provider: str = "deepseek",
        model: str = "deepseek-flash",
        input_tokens: int = 8192,
        output_tokens: int = 512,
    ) -> None:
        self.budget, self.provider, self.model = budget, provider, model
        self.input_tokens, self.output_tokens = input_tokens, output_tokens
        self.pending: deque[BudgetReservation] = deque()
        self.owner: asyncio.Task[Any] | None = None
        self.token: Token[PrepaidPair | None] | None = None

    async def _release_unused(self) -> None:
        while self.pending:
            reservation = self.pending[0]
            await self.budget.release(reservation.reservation_id)
            self.pending.popleft()

    async def __aenter__(self) -> PrepaidPair:
        if _ACTIVE.get() is not None:
            raise BudgetError("nested prepaid model operations are forbidden")
        self.owner = asyncio.current_task()
        try:
            for _ in range(2):
                self.pending.append(
                    await self.budget.reserve(
                        provider=self.provider,
                        model=self.model,
                        prompt_tokens=self.input_tokens,
                        maximum_completion_tokens=self.output_tokens,
                    )
                )
        except BaseException:
            await asyncio.shield(self._release_unused())
            raise
        self.token = _ACTIVE.set(self)
        return self

    async def __aexit__(self, *_: object) -> None:
        assert self.token is not None
        _ACTIVE.reset(self.token)
        await asyncio.shield(self._release_unused())
