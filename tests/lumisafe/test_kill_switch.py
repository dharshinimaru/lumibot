"""KillSwitch: R7 latches until manual reset; R8 alert raised once per episode."""

from datetime import timedelta

from lumisafe.config.schema import RiskConfig
from lumisafe.risk.kill_switch import RESET_CONFIRMATION, KillSwitch

from .helpers import T0, snapshot


def sat(eq, peak=10_000, at=T0):
    return snapshot(cash=1000, satellite_equity=eq, satellite_peak_equity=peak, satellite_day_start_equity=eq,
                    as_of=at)


def test_r7_latches_until_typed_reset():
    ks = KillSwitch(RiskConfig())
    assert ks.update(sat(9_000)) == []
    alerts = ks.update(sat(8_400))
    assert ks.satellite_latched and len(alerts) == 1
    ks.update(sat(9_900))  # recovered, still latched
    assert ks.satellite_latched
    assert ks.reset_satellite("yes") is False and ks.satellite_latched
    assert ks.reset_satellite(RESET_CONFIRMATION) is True and not ks.satellite_latched


def test_r7_trigger_count_and_state_round_trip():
    ks = KillSwitch(RiskConfig())
    ks.update(sat(8_000, at=T0))
    ks.reset_satellite(RESET_CONFIRMATION)
    ks.update(sat(8_000, at=T0 + timedelta(days=100)))
    restored = KillSwitch(RiskConfig(), ks.to_dict())
    assert restored.satellite_latched
    assert restored.r7_count_within(T0 + timedelta(days=200)) == 2
    assert restored.r7_count_within(T0 + timedelta(days=400)) == 1


def test_account_alert_raised_once_per_episode():
    ks = KillSwitch(RiskConfig())
    down = snapshot(cash=70_000, peak_equity=100_000)
    assert len(ks.update(down)) == 1
    assert ks.update(down) == []
    ks.update(snapshot(cash=100_000, peak_equity=100_000))
    assert len(ks.update(down)) == 1
