"""Validated, secret-free provider account balance snapshots."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CurrencyBalance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    currency: Literal["CNY", "USD"]
    total_balance: Decimal = Field(ge=0, allow_inf_nan=False)
    granted_balance: Decimal = Field(ge=0, allow_inf_nan=False)
    topped_up_balance: Decimal = Field(ge=0, allow_inf_nan=False)


class AccountBalance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    is_available: bool
    balance_infos: tuple[CurrencyBalance, ...] = Field(max_length=2)
    fetched_at: datetime
