"""Config invariants C1 and C2 (SPEC §5.1)."""

from pathlib import Path

import pytest
from pydantic import ValidationError

import lumisafe
from lumisafe.config.schema import (
    CoreConfig, LumiSafeConfig, RiskConfig, SatelliteConfig, load_config, save_config,
)

CORE = {"etfs": {"VTI": 0.6, "VXUS": 0.2, "BND": 0.2}, "monthly_contribution_usd": 100}


def test_defaults_are_beginner_safe():
    cfg = LumiSafeConfig(core=CORE)
    assert cfg.mode == "paper"
    assert cfg.satellite.enabled is False
    assert cfg.risk.allow_leverage is False and cfg.risk.allow_short is False and cfg.risk.allow_options is False


@pytest.mark.parametrize("flag", ["allow_leverage", "allow_short", "allow_options"])
@pytest.mark.parametrize("value", [True, 1, "true", "True", "yes"])
def test_c1_dangerous_flags_cannot_be_enabled(flag, value):
    with pytest.raises(ValidationError):
        RiskConfig(**{flag: value})
    with pytest.raises(ValidationError):
        LumiSafeConfig(core=CORE, risk={flag: value})


@pytest.mark.parametrize("flag", ["allow_leverage", "allow_short", "allow_options"])
def test_c1_dangerous_flags_cannot_be_flipped_after_construction(flag):
    cfg = LumiSafeConfig(core=CORE)
    with pytest.raises(ValidationError):
        setattr(cfg.risk, flag, True)
    assert getattr(cfg.risk, flag) is False


def test_c1_unknown_risk_keys_rejected():
    with pytest.raises(ValidationError):
        RiskConfig(allow_margin=True)


@pytest.mark.parametrize("mode", ["live", "LIVE", "Live", "real", ""])
def test_c2_mode_can_never_be_live(mode):
    with pytest.raises(ValidationError):
        LumiSafeConfig(core=CORE, mode=mode)


def test_c2_mode_cannot_be_switched_to_live_after_construction():
    cfg = LumiSafeConfig(core=CORE)
    with pytest.raises(ValidationError):
        cfg.mode = "live"
    assert cfg.mode == "paper"


def test_c2_live_in_yaml_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("mode: live\ncore:\n  etfs: {VTI: 1.0}\n  monthly_contribution_usd: 10\n")
    with pytest.raises(ValidationError):
        load_config(p)


@pytest.mark.parametrize("mode", ["paper", "backtest"])
def test_valid_modes(mode):
    assert LumiSafeConfig(core=CORE, mode=mode).mode == mode


def test_core_weights_must_sum_to_one():
    with pytest.raises(ValidationError):
        LumiSafeConfig(core={"etfs": {"VTI": 0.5, "BND": 0.4}, "monthly_contribution_usd": 0})
    with pytest.raises(ValidationError):
        LumiSafeConfig(core={"etfs": {}, "monthly_contribution_usd": 0})
    with pytest.raises(ValidationError):
        LumiSafeConfig(core={"etfs": {"VTI": 1.5, "BND": -0.5}, "monthly_contribution_usd": 0})


def test_field_bounds():
    with pytest.raises(ValidationError):
        CoreConfig(etfs={"VTI": 1.0}, monthly_contribution_usd=-1)
    with pytest.raises(ValidationError):
        SatelliteConfig(max_weight=0.5)
    with pytest.raises(ValidationError):
        RiskConfig(max_single_stock_weight=0.2)


def test_round_trip_and_shipped_defaults(tmp_path):
    defaults = load_config(Path(lumisafe.__file__).parent / "config" / "defaults.yaml")
    p = tmp_path / "c.yaml"
    save_config(defaults, p)
    assert load_config(p) == defaults
