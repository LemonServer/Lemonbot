from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from lemonbot.models.account import AccountBalance
from lemonbot.models.budget import BudgetLimits, BudgetManager
from lemonbot.models.config import ProviderConfig
from lemonbot.models.gateway import ModelProtocolError, ModelTransportError, OpenAICompatibleBackend
from lemonbot.models.secrets import MappingSecretStore
from lemonbot.models.worker_protocol import ModelBalanceResult, validate_payload


async def query(payload: object, *, status: int = 200) -> AccountBalance:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "https://api.deepseek.com/user/balance"
        assert request.headers["authorization"] == "Bearer test-credential"
        return httpx.Response(status, json=payload)

    budget = BudgetManager(limits=BudgetLimits(Decimal("5"), Decimal("100")), prices={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        backend = OpenAICompatibleBackend(
            config=ProviderConfig.deepseek(),
            secret_store=MappingSecretStore({"deepseek_api_key": "test-credential"}),
            budget=budget,
            client=client,
        )
        result = await backend.account_balance()
        assert (await budget.snapshot()).daily_spent == 0
        return result


def payload() -> dict[str, object]:
    return {
        "is_available": True,
        "balance_infos": [
            {
                "currency": "CNY",
                "total_balance": "12.34",
                "granted_balance": "2.34",
                "topped_up_balance": "10.00",
            }
        ],
    }


async def test_balance_query_is_exact_decimal_and_does_not_spend_model_budget() -> None:
    result = await query(payload())
    assert result.balance_infos[0].total_balance == Decimal("12.34")
    assert result.fetched_at.tzinfo is not None
    wire = ModelBalanceResult(balance=result).model_dump(mode="json")
    assert validate_payload(ModelBalanceResult, wire).balance == result
    assert "test-credential" not in str(wire)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {"is_available": "yes", "balance_infos": []},
        {
            "is_available": True,
            "balance_infos": [
                {
                    "currency": "CNY",
                    "total_balance": "NaN",
                    "granted_balance": "0",
                    "topped_up_balance": "0",
                }
            ],
        },
    ],
)
async def test_invalid_balance_is_not_treated_as_zero_or_available(bad: object) -> None:
    with pytest.raises(ModelProtocolError, match="response was invalid"):
        await query(bad)


@pytest.mark.parametrize("status", [401, 402, 503])
async def test_failed_balance_query_does_not_expose_provider_body(status: int) -> None:
    with pytest.raises(ModelTransportError) as error:
        await query({"error": "secret provider text"}, status=status)
    assert "secret provider text" not in str(error.value)


def test_balance_snapshot_requires_finite_nonnegative_amounts() -> None:
    result = AccountBalance.model_validate({**payload(), "fetched_at": datetime.now(UTC)})
    assert result.is_available
