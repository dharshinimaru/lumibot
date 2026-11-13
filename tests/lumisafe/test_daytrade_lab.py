"""Day-trading lab: walk-forward, verdict, report text and CLI (synthetic fixtures, no network)."""

import json
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from click.testing import CliRunner

from lumisafe import DISCLAIMER, paths
from lumisafe.config.schema import DayTradeConfig
from lumisafe.daytrade import report as rpt
from lumisafe.daytrade.report import format_report, walk_forward
from lumisafe.daytrade.sim import Params
from lumisafe.promotion import apply_backtest
from lumisafe.promotion.registry import Registry
from lumisafe.reporting import cli

from .test_daytrade_sim import NY, bars, cand, opening_range


def fixtures(win_every=2, years=(2016, 2017, 2018, 2019)):
    days = [d.date() for d in pd.bdate_range(f"{years[0]}-01-04", f"{years[-1]}-12-31")]
    cands, data = {}, {}
    for i, d in enumerate(days):
        if i % 5:
            continue
        o = datetime(d.year, d.month, d.day, 9, 30, tzinfo=NY)
        c = datetime(d.year, d.month, d.day, 16, 0, tzinfo=NY)
        win = (i // 5) % win_every == 0
        move = (101.4, 104.0, 101.4, 103.9) if win else (101.4, 101.4, 98.0, 98.2)
        cands[d.isoformat()] = [cand(open_=o, close=c)]
        data[d] = {"ABC": bars(opening_range() | {20: (101.0, 101.5, 101.0, 101.4), 30: move}, open_=o, close=c)}
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in days])
    spy = pd.Series(100 * np.cumprod(np.full(len(idx), 1.0003)), index=idx)
    return days, cands, (lambda d, syms: data[d]), spy


def run_wf(win_every=2):
    days, cands, load, spy = fixtures(win_every)
    grid = [Params(min_gap=0.02, min_rel_volume=0.05, target_r=1.0),
            Params(min_gap=0.02, min_rel_volume=0.05, target_r=2.0)]
    return walk_forward(cands, load, spy, spy, days, DayTradeConfig(enabled=True), grid=grid,
                        first_train_start="2016-01-01", train_years=1, test_years=1, progress=lambda m: None)


def test_walk_forward_tunes_on_training_years_only_and_reports_costs():
    r = run_wf()
    wf = r.walk_forward
    assert [f.test_start for f in wf.folds] == ["2017-01-01", "2018-01-01", "2019-01-01"]
    assert all(f.train_end < f.test_start for f in wf.folds)
    s = r.summary
    assert s["trades"] > 0 and s["years"] > 2.5
    m = s["money"]
    assert m["bid_ask_spread"] < 0 and m["slippage"] < 0 and m["regulatory_fees"] < 0
    assert m["net_after_tax"] <= m["net_before_tax"] or m["income_tax"] >= 0
    # only 3 folds over < 8 years: never promoted, whatever the returns
    assert r.passed is False and any("folds" in x for x in r.reasons)


def test_losing_setup_is_rejected_and_the_registry_says_so(tmp_path):
    r = run_wf(win_every=4)  # one winner in four
    assert r.summary["money"]["net_before_tax"] < 0
    out = r.save(tmp_path / "rep.json")
    reg = Registry(tmp_path / "registry.json")
    state = apply_backtest(rpt.STRATEGY_NAME, r.walk_forward, out, reg, datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert state.value == "DRAFT"  # not enough folds/years to count as a valid backtest at all
    text = format_report(json.loads(out.read_text()))
    assert "did NOT pass" in text and "Holding SPY" in text and "Bid/ask spread" in text
    assert "AI veto is NOT in this test" in text


def test_cli_daytrade_report_and_missing_data(monkeypatch, tmp_path):
    r = CliRunner()
    res = r.invoke(cli.main, ["daytrade", "report"])
    assert res.exit_code == 1 and "lumisafe daytrade fetch" in res.output
    run_wf().save(paths.reports_dir() / f"daytrade_{rpt.STRATEGY_NAME}_20260101_000000.json")
    res = r.invoke(cli.main, ["daytrade", "report"])
    assert res.exit_code == 0 and "Where the money went" in res.output and DISCLAIMER in res.output

    class FakeMD:
        def daily_bars(self, *a, **k):
            raise AssertionError("not reached")

    monkeypatch.setattr(cli, "_market_data", lambda: FakeMD())
    res = r.invoke(cli.main, ["daytrade", "backtest"])
    assert res.exit_code == 1 and "Run `lumisafe daytrade fetch` first" in res.output


def test_dataset_ignores_news_after_the_decision_time(monkeypatch):
    from lumisafe.daytrade import dataset
    from lumisafe.daytrade.universe import Session

    seen = {}

    class FakeMD:
        def daily_bars(self, symbols, start, end, adjustment="split"):
            idx = pd.bdate_range("2024-01-02", "2024-02-02")
            close = np.full(len(idx), 100.0)
            opn = close.copy()
            opn[-1] = 104.0  # gaps up on the last day
            return {"ABC": pd.DataFrame({"open": opn, "high": opn, "low": opn, "close": close,
                                         "volume": 1e6}, index=idx)}

        def news(self, symbols, day, start, end):
            seen["window"] = (start, end)
            return [{"created_at": "2024-02-02T14:00:00+00:00", "headline": "ABC news", "source": "x",
                     "symbols": ["ABC"]}]

        def minute_bars(self, symbols, day, o, c):
            return {}

        def spread(self, s, at):
            raise AssertionError("no bars, so no quote request")

        def save_spreads(self):
            pass

    monkeypatch.setattr(dataset, "sessions", lambda a, b: [Session(
        date(2024, 2, 2), datetime(2024, 2, 2, 9, 30, tzinfo=NY), datetime(2024, 2, 2, 16, 0, tzinfo=NY))])
    cands = dataset.build(FakeMD(), date(2024, 2, 2), date(2024, 2, 2), progress=lambda m: None)
    start, end = seen["window"]
    assert end == datetime(2024, 2, 2, 9, 44, tzinfo=NY)  # 09:45 decision minus the 60 s reading delay
    assert start < datetime(2024, 2, 2, 9, 30, tzinfo=NY)
    assert len(cands) == 1 and cands[0].has_bars is False and cands[0].spread is None


def test_minute_bars_are_converted_to_the_prices_that_actually_traded():
    from lumisafe.daytrade.lab import to_actual_prices

    d = date(2016, 2, 2)
    split = {"GOOGL": pd.DataFrame({"close": [40.0]}, index=[pd.Timestamp(d)])}  # adjusted for the 2022 20:1 split
    raw = {"GOOGL": pd.DataFrame({"close": [800.0]}, index=[pd.Timestamp(d)])}
    b = pd.DataFrame({"open": [40.0], "high": [41.0], "low": [39.0], "close": [40.5], "volume": [20_000.0]},
                     index=[pd.Timestamp("2016-02-02 09:30", tz=NY)])
    out = to_actual_prices(b, "GOOGL", d, split, raw)
    assert out["open"].iloc[0] == 800.0 and out["close"].iloc[0] == 810.0
    assert out["volume"].iloc[0] == 1_000.0  # same dollar volume
    assert to_actual_prices(b, "MSFT", d, split, raw) is b  # unknown factor: unchanged, never guessed
