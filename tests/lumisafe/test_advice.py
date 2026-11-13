"""Read-only "what to buy next" advice (portfolio/advice.py, `lumisafe next`, dashboard panel)."""

import ast
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import lumisafe
from lumisafe import DISCLAIMER, paths
from lumisafe.config.schema import CoreConfig, LumiSafeConfig, save_config
from lumisafe.portfolio import advice as adv
from lumisafe.reporting import cli
from lumisafe.runner import StateStore

PLAN = {"VTI": 0.7, "VXUS": 0.25, "BND": 0.05}
PX = {"VTI": 300.0, "VXUS": 60.0, "BND": 70.0}


def core(monthly=100.0, band=0.05):
    return CoreConfig.model_validate({"etfs": PLAN, "monthly_contribution_usd": monthly, "rebalance_band": band})


def test_parse_holdings():
    assert adv.parse_holdings("vti=10, VXUS=4.5;BND=0") == {"VTI": 10.0, "VXUS": 4.5, "BND": 0.0}
    assert adv.parse_holdings(None) == {} and adv.parse_holdings("") == {}
    for bad in ("VTI", "VTI=-1", "=3", "VTI=abc"):
        with pytest.raises(ValueError):
            adv.parse_holdings(bad)


def test_from_zero_buys_toward_targets_and_never_spends_more_than_the_amount():
    a = adv.what_to_buy(core(), {}, PX, 1000.0)
    assert {l.symbol for l in a.buys} == set(PLAN) and not a.sells
    spent = sum(l.dollars for l in a.buys)
    assert spent <= 1000.0 and a.leftover == pytest.approx(1000.0 - spent)
    for s, w in PLAN.items():
        assert a.weights_after[s] == pytest.approx(w, abs=0.01)


def test_contribution_goes_to_the_most_underweight_fund_first():
    a = adv.what_to_buy(core(), {"VTI": 3, "VXUS": 4, "BND": 1}, PX, 100.0)  # VXUS 20% vs 25%
    assert [l.symbol for l in a.buys][0] == "VXUS"
    assert all(l.side == "buy" for l in a.lines)  # within band after buying → no sells


def test_far_off_target_sells_the_overweight_fund_and_reinvests_the_proceeds():
    a = adv.what_to_buy(core(), {"BND": 20}, PX, 50.0)  # 100% bonds vs a 5% target
    assert [l.symbol for l in a.sells] == ["BND"]
    assert "BND" not in {l.symbol for l in a.buys}  # never sell and buy the same fund
    proceeds = sum(l.dollars for l in a.sells)
    assert sum(l.dollars for l in a.buys) == pytest.approx(50.0 + proceeds, abs=1.0)
    assert any("tax bill" in n for n in a.notes)
    for s, w in PLAN.items():
        assert a.weights_after[s] == pytest.approx(w, abs=0.01)


def test_whole_shares_only_buys_whole_shares_and_explains_leftover():
    a = adv.what_to_buy(core(), {}, PX, 1000.0, fractional=False)
    assert all(float(l.shares).is_integer() for l in a.lines)
    assert a.leftover > 0 and any("Whole shares" in n for n in a.notes)
    tiny = adv.what_to_buy(core(), {}, PX, 50.0, fractional=False)
    assert not tiny.lines and any("whole share" in n for n in tiny.notes)


def test_tiny_amount_and_missing_price():
    a = adv.what_to_buy(core(), {}, PX, 5.0)
    assert not a.lines and any("too small" in n for n in a.notes)
    with pytest.raises(ValueError):
        adv.what_to_buy(core(), {}, {"VTI": 300.0}, 100.0)


def test_buy_day_is_the_first_market_day_of_the_month():
    assert adv.first_market_day(2026, 11) == date(2026, 11, 2)  # Nov 1 2026 is a Sunday
    assert adv.first_market_day(2027, 1) == date(2027, 1, 4)  # Jan 1 holiday, then a weekend
    assert adv.buy_dates(date(2026, 11, 13)) == (date(2026, 11, 2), date(2026, 12, 1))
    assert "Buy on" in adv.when_text(date(2027, 1, 1))
    assert "next buy is" in adv.when_text(date(2026, 11, 13))


def test_advice_module_is_read_only():
    tree = ast.parse((Path(lumisafe.__file__).parent / "portfolio" / "advice.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            assert name not in {"submit", "submit_order", "record", "record_event", "save", "push"}, name
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = getattr(node, "module", None) or ""
            assert not mod.startswith(("lumisafe.runner", "lumisafe.adapters", "lumibot")), mod


# --- CLI ---------------------------------------------------------------------------------------------


def fake_history(symbols, start=None, end=None):
    idx = pd.bdate_range("2026-11-02", periods=9)
    return {s: pd.DataFrame({"close": np.full(9, PX[s])}, index=idx) for s in symbols}


@pytest.fixture
def plan():
    save_config(LumiSafeConfig.model_validate({"core": {"etfs": PLAN, "monthly_contribution_usd": 100}}),
                paths.config_path())


def test_cli_next_prints_buys_when_and_disclaimer(plan, monkeypatch):
    from click.testing import CliRunner

    monkeypatch.setattr(cli, "history_provider", fake_history)
    res = CliRunner().invoke(cli.main, ["next", "--holdings", "VTI=1"])
    assert res.exit_code == 0, res.output
    assert "Adding $100.00" in res.output and "BUY  VXUS" in res.output
    assert "When:" in res.output and DISCLAIMER in res.output
    assert "Nov 12 close" in res.output


def test_cli_next_paper_holdings_and_errors(plan, monkeypatch):
    from click.testing import CliRunner

    monkeypatch.setattr(cli, "history_provider", fake_history)
    r = CliRunner()
    assert "No paper account snapshot yet" in r.invoke(cli.main, ["next", "--paper"]).output
    st = StateStore(paths.state_path())
    st.set("last_snapshot", {"as_of": "2026-11-12T15:00:00+00:00", "cash": 0, "equity": 100,
                             "positions": {"BND": 20}, "prices": {}})
    st.save()
    out = r.invoke(cli.main, ["next", "--paper"]).output
    assert "your paper account" in out and "SELL BND" in out
    bad = r.invoke(cli.main, ["next", "--holdings", "VTI"])
    assert bad.exit_code == 1 and "SYMBOL=SHARES" in bad.output

    def offline(*a, **k):
        raise ConnectionError("no network")

    monkeypatch.setattr(cli, "history_provider", offline)
    down = r.invoke(cli.main, ["next"])
    assert down.exit_code == 1 and "Couldn't get prices" in down.output


def test_cli_next_lump_sum_plan_needs_an_amount(monkeypatch):
    from click.testing import CliRunner

    save_config(LumiSafeConfig.model_validate({"core": {"etfs": PLAN, "monthly_contribution_usd": 0}}),
                paths.config_path())
    monkeypatch.setattr(cli, "history_provider", fake_history)
    r = CliRunner()
    assert r.invoke(cli.main, ["next"]).exit_code == 1
    assert "BUY" in r.invoke(cli.main, ["next", "--amount", "500"]).output


# --- dashboard ---------------------------------------------------------------------------------------


def test_dashboard_what_to_buy_panel(plan, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from lumisafe.reporting import dashboard

    monkeypatch.setattr(dashboard, "default_history", fake_history)
    at = AppTest.from_string(
        "from lumisafe.reporting import dashboard\n"
        "import streamlit as st\n"
        "dashboard.render(st, dashboard.load_data(dashboard.default_history))\n"
    )
    at.run(timeout=30)
    assert not at.exception
    assert at.header[0].value == "What to buy next"
    table = at.dataframe[0].value
    assert set(table["fund"]) <= set(PLAN) and set(table["action"]) == {"BUY"}
    assert any("When:" in m.value for m in at.markdown)
    at.number_input[0].set_value(0.0).run()
    assert any("Nothing to buy" in m.value for m in at.markdown)


def test_dashboard_panel_without_prices(plan):
    from streamlit.testing.v1 import AppTest

    AppTest_src = (
        "from lumisafe.reporting import dashboard\n"
        "import streamlit as st\n"
        "dashboard.render(st, dashboard.load_data(lambda *a, **k: {}))\n"
    )
    at = AppTest.from_string(AppTest_src)
    at.run(timeout=30)
    assert not at.exception
    assert any("Couldn't load recent prices" in w.value for w in at.warning)
