"""Drawdown and daily-loss halts that need memory across cycles.

:class:`RiskGate` is pure, so on its own it can't remember that the R7 halt
fired once the satellite recovers a little. :class:`KillSwitch` latches R7
until a human resets it (``lumisafe reset-halt satellite``). It also counts R7
triggers (for auto-retirement, SPEC §8) and raises the R8 account alert once per
drawdown episode.

While latched, the runner keeps ``satellite_peak_equity`` frozen at the trigger
peak and does not run the SatelliteManager at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from lumisafe.config.schema import RiskConfig
from lumisafe.risk import limits
from lumisafe.types import PortfolioSnapshot

RESET_CONFIRMATION = "RESET SATELLITE"


class KillSwitch:
    def __init__(self, config: RiskConfig, state: dict[str, Any] | None = None):
        self.config = config
        state = dict(state or {})
        self.satellite_latched: bool = bool(state.get("satellite_latched", False))
        self.r7_triggers: list[str] = list(state.get("r7_triggers", []))
        self.account_alert_active: bool = bool(state.get("account_alert_active", False))

    def to_dict(self) -> dict[str, Any]:
        return {
            "satellite_latched": self.satellite_latched,
            "r7_triggers": list(self.r7_triggers),
            "account_alert_active": self.account_alert_active,
        }

    def update(self, snap: PortfolioSnapshot) -> list[str]:
        """Evaluate halts for this cycle; return new alert messages (empty if none)."""
        alerts: list[str] = []
        if not self.satellite_latched and limits.satellite_drawdown_halt(snap, self.config):
            self.satellite_latched = True
            self.r7_triggers.append(snap.as_of.isoformat())
            alerts.append(
                f"Satellite halted: down {self.config.satellite_drawdown_halt:.0%}+ from its peak. "
                "It stays off until you run `lumisafe reset-halt satellite`."
            )
        in_alert = limits.account_drawdown_alert(snap, self.config)
        if in_alert and not self.account_alert_active:
            alerts.append(
                f"Your account is down {self.config.account_drawdown_alert:.0%}+ from its peak. This is normal "
                "in bear markets. LumiSafe keeps your core invested and pauses new non-core buys."
            )
        self.account_alert_active = in_alert
        return alerts

    def r7_count_within(self, as_of: datetime, days: int = 365) -> int:
        cutoff = as_of - timedelta(days=days)
        return sum(1 for t in self.r7_triggers if datetime.fromisoformat(t) >= cutoff)

    def reset_satellite(self, typed_confirmation: str) -> bool:
        """Manual R7 reset. The human must type the exact confirmation phrase."""
        if typed_confirmation.strip() != RESET_CONFIRMATION:
            return False
        self.satellite_latched = False
        return True
