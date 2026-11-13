"""Pydantic config models (SPEC §5.1). These are binding contracts.

Invariant C1: leverage, shorting and options cannot be enabled. The field type
is ``Literal[False]``, and ``validate_assignment`` stops anyone from flipping
it after construction.
Invariant C2: ``mode`` can never be ``"live"``. Live only exists through
:mod:`lumisafe.live_unlock` (SPEC §10).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

_STRICT = ConfigDict(extra="forbid", validate_assignment=True)


class CoreConfig(BaseModel):
    model_config = _STRICT

    etfs: dict[str, float]  # ticker -> target weight within core; sums to 1.0
    monthly_contribution_usd: float = Field(ge=0)
    rebalance_band: float = Field(0.05, gt=0, le=0.20)  # rebalance when drift > band
    rebalance_check: Literal["monthly", "quarterly"] = "monthly"


class SatelliteConfig(BaseModel):
    model_config = _STRICT

    enabled: bool = False  # OFF by default for beginners
    max_weight: float = Field(0.10, ge=0, le=0.20)
    allowed_states: list[str] = ["LIVE_ELIGIBLE"]


class RiskConfig(BaseModel):
    model_config = _STRICT

    max_single_stock_weight: float = Field(0.05, gt=0, le=0.10)
    max_single_etf_weight: float = Field(0.60, gt=0, le=1.0)
    max_trades_per_week: int = Field(5, ge=1, le=20)
    satellite_daily_loss_halt: float = Field(0.03, gt=0, le=0.10)
    satellite_drawdown_halt: float = Field(0.15, gt=0, le=0.30)
    account_drawdown_alert: float = Field(0.25, gt=0, le=0.50)
    min_holding_days: int = Field(5, ge=1)
    allow_leverage: Literal[False] = False
    allow_short: Literal[False] = False
    allow_options: Literal[False] = False


class DayTradeConfig(BaseModel):
    """Paper-only intraday lab (owner-approved amendment to SPEC §2, 2026-11-13; see DECISIONS.md).

    Long only, no leverage (inherits C1), every position closed before the
    close. Its own limits replace the 5-trades-a-week cap and the 5-day minimum
    hold for this slice only; the rest of the account keeps the SPEC rules.
    """

    model_config = _STRICT

    enabled: bool = False
    slice_fraction: float = Field(0.10, gt=0, le=0.25)  # share of account equity the lab may use
    daily_loss_halt: float = Field(0.01, gt=0, le=0.03)  # stop for the day after losing this much of the slice
    risk_per_trade: float = Field(0.0025, gt=0, le=0.01)  # loss at the stop, as a share of the slice
    max_position_fraction: float = Field(0.34, gt=0, le=0.5)  # one position's value, share of the slice
    max_positions: int = Field(3, ge=1, le=5)
    max_trades_per_day: int = Field(6, ge=1, le=20)
    account_type: Literal["margin", "cash"] = "margin"  # Alpaca paper accounts are margin accounts
    short_term_tax_rate: float = Field(0.30, ge=0, le=0.60)  # your marginal rate incl. state; for after-tax figures
    long_term_tax_rate: float = Field(0.15, ge=0, le=0.40)
    allowed_states: list[str] = ["PAPER", "LIVE_ELIGIBLE"]


class LumiSafeConfig(BaseModel):
    model_config = _STRICT

    mode: Literal["backtest", "paper"] = "paper"  # "live" is NOT a valid value here (§10)
    core: CoreConfig
    satellite: SatelliteConfig = SatelliteConfig()
    risk: RiskConfig = RiskConfig()
    daytrade: DayTradeConfig = DayTradeConfig()
    benchmark: str = "SPY"
    ai_enabled: bool = True

    @model_validator(mode="after")
    def weights_sum(self):
        # SPEC uses ``assert``; a raised ValueError gives the same result but
        # still runs under ``python -O``, where asserts are stripped.
        weights = self.core.etfs.values()
        if not self.core.etfs or abs(sum(weights) - 1.0) >= 1e-6 or any(w < 0 for w in weights):
            raise ValueError("core.etfs weights must be non-negative and sum to 1.0")
        return self


def load_config(path: str | Path) -> LumiSafeConfig:
    data = yaml.safe_load(Path(path).read_text()) or {}
    return LumiSafeConfig.model_validate(data)


def save_config(config: LumiSafeConfig, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False))
