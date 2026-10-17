from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from lemonbot.domain.models import MessageRole, ModelImage, ModelMessage, ModelRequest
from lemonbot.models.budget import BudgetError, BudgetLimits, ModelPrice
from lemonbot.models.config import ProviderConfig
from lemonbot.models.gateway import OpenAICompatibleBackend
from lemonbot.models.persistent_budget import PersistentBudgetManager
from lemonbot.models.prepaid_budget import PrepaidPair
from lemonbot.models.secrets import MappingSecretStore


async def manager(path: Path) -> PersistentBudgetManager:
    return await PersistentBudgetManager.create(
        database_path=path,
        limits=BudgetLimits(daily=Decimal("5"), monthly=Decimal("100")),
        prices={("deepseek", "deepseek-flash"): ModelPrice(Decimal("1"), Decimal("1"))},
    )


async def consume(budget, *, prompt: int = 100):
    return await budget.reserve(
        provider="deepseek",
        model="deepseek-flash",
        prompt_tokens=prompt,
        maximum_completion_tokens=512,
    )


async def test_prepaid_calls_settle_once_with_only_two_durable_rows_and_real_gateway(
    tmp_path,
) -> None:
    budget = await manager(tmp_path / "ledger.db")
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "prompt_cache_hit_tokens": 80,
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    config = ProviderConfig(
        provider="deepseek",
        base_url="https://api.deepseek.com",
        secret_name=None,
        flash_model="deepseek-flash",
        pro_model="deepseek-flash",
    )
    backend = OpenAICompatibleBackend(
        config=config, secret_store=MappingSecretStore({}), budget=budget, client=client
    )
    request = ModelRequest(
        messages=(ModelMessage(role=MessageRole.USER, content="synthetic"),), max_tokens=512
    )
    try:
        async with PrepaidPair(budget):
            assert (await budget.snapshot()).daily_reserved == Decimal("0.017408")
            await backend.generate(request)
            assert (await budget.snapshot()).daily_reserved == Decimal("0.008704")
            await backend.generate(request)
    finally:
        await client.aclose()
    assert len(calls) == 2
    with sqlite3.connect(tmp_path / "ledger.db") as db:
        assert db.execute("SELECT state FROM model_budget_ledger").fetchall() == [
            ("settled",),
            ("settled",),
        ]
    assert (await budget.snapshot()).daily_spent == Decimal("0.000240")
    assert (await budget.snapshot()).daily_reserved == 0
    assert (await (await manager(tmp_path / "ledger.db")).snapshot()).daily_spent == Decimal(
        "0.000240"
    )


async def test_unused_pair_released_on_classification_failure_or_cancel(tmp_path) -> None:
    budget = await manager(tmp_path / "ledger.db")
    with pytest.raises(asyncio.CancelledError):
        async with PrepaidPair(budget):
            raise asyncio.CancelledError
    assert (await budget.snapshot()).daily_reserved == 0
    assert (await budget.snapshot()).daily_spent == 0


async def test_claim_cannot_change_provider_day_or_capacity(tmp_path, monkeypatch) -> None:
    budget = await manager(tmp_path / "ledger.db")
    async with PrepaidPair(budget):
        with pytest.raises(BudgetError, match="bound reservation"):
            await consume(budget, prompt=20000)
        now = budget._now()
        monkeypatch.setattr(budget, "_now", lambda: now + timedelta(days=1))
        with pytest.raises(BudgetError, match="bound reservation"):
            await consume(budget)
    assert (await budget.snapshot()).daily_spent == 0


async def test_child_task_cannot_consume_parent_preallocation(tmp_path) -> None:
    budget = await manager(tmp_path / "ledger.db")
    async with PrepaidPair(budget):
        child = await asyncio.create_task(consume(budget))
        assert child.amount == Decimal("0.000612")
        assert (await budget.snapshot()).daily_reserved == Decimal("0.018020")
        await budget.release(child.reservation_id)
    assert (await budget.snapshot()).daily_reserved == 0


async def test_crash_recovery_conservatively_charges_open_pair_once(tmp_path) -> None:
    budget = await manager(tmp_path / "ledger.db")
    pair = PrepaidPair(budget)
    await pair.__aenter__()
    try:
        recovered = await manager(tmp_path / "ledger.db")
        assert (await recovered.snapshot()).daily_spent == Decimal("0.017408")
        again = await manager(tmp_path / "ledger.db")
        assert (await again.snapshot()).daily_spent == Decimal("0.017408")
    finally:
        # Simulated process loss: restore only this test's task context, not old ledger state.
        from lemonbot.models.prepaid_budget import _ACTIVE

        _ACTIVE.reset(pair.token)


@pytest.mark.parametrize("accepted", [True, False])
async def test_worker_attestation_reports_actual_image_encoding_not_model_claim(
    tmp_path, accepted
) -> None:
    budget = await manager(tmp_path / "ledger.db")

    class Reader:
        def data_url(self, image):
            if not accepted:
                raise ValueError("synthetic corrupt image")
            return "data:image/jpeg;base64,c3ludGhldGlj"

    def respond(request):
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "description"}}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "prompt_cache_hit_tokens": 80,
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    config = ProviderConfig(
        provider="deepseek",
        base_url="https://api.deepseek.com",
        secret_name=None,
        flash_model="deepseek-flash",
        pro_model="deepseek-flash",
    )
    backend = OpenAICompatibleBackend(
        config=config,
        secret_store=MappingSecretStore({}),
        budget=budget,
        client=client,
        image_reader=Reader(),
    )
    try:
        response = await backend.generate(
            ModelRequest(
                messages=(
                    ModelMessage(
                        role=MessageRole.USER,
                        content="synthetic",
                        images=(ModelImage(sha256="a" * 64, size=10),),
                    ),
                ),
                max_tokens=512,
            )
        )
        assert response.image_input_accepted is accepted
        assert response.cached_prompt_tokens == 80
    finally:
        await client.aclose()
