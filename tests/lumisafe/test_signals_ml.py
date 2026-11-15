"""ML stock pickers: walk-forward never sees the future; news timing; learns a planted signal (synthetic, no network)."""

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("sklearn")

from lumisafe.signals import ml, zoo  # noqa: E402

N = 1100
IDX = pd.bdate_range("2016-01-04", periods=N)  # through ~mid 2020: test years 2018-2020
STOCKS = [f"S{i:02d}" for i in range(30)]


def bars(seed=1, drift_strength=0.0006):
    rng = np.random.default_rng(seed)
    out = {}
    drifts = np.linspace(-drift_strength, drift_strength, len(STOCKS))
    for s, mu in zip(STOCKS, drifts):
        c = 50 * np.cumprod(1 + rng.normal(mu, 0.015, N))
        o = c * (1 + rng.normal(0, 0.002, N))
        out[s] = pd.DataFrame({"open": o, "high": c, "low": c, "close": c, "volume": 1e6 + rng.integers(0, 1e5, N)},
                              index=IDX)
    for s in ("SPY", "BIL"):
        c = 100 * np.cumprod(1 + rng.normal(0.0003 if s == "SPY" else 0.00005, 0.01 if s == "SPY" else 0.0001, N))
        out[s] = pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1e7}, index=IDX)
    return out


def test_embargo_keeps_every_training_label_before_the_test_starts():
    rows = ml.train_rows(N, 700, 21)
    assert rows.max() + 21 + 1 <= 700 - 1 + 1 and rows.min() >= ml.WARMUP
    assert all(r % ml.SAMPLE_EVERY == 0 for r in rows)


def test_headline_after_the_close_counts_for_the_next_session():
    days = pd.DatetimeIndex(["2021-03-03", "2021-03-04", "2021-03-05"])
    ny = ml.NY
    assert ml.news_day_index(datetime(2021, 3, 3, 15, 59, tzinfo=ny), days) == 0
    assert ml.news_day_index(datetime(2021, 3, 3, 16, 1, tzinfo=ny), days) == 1
    assert ml.news_day_index(datetime(2021, 3, 4, 4, 9, tzinfo=ml.ZoneInfo("UTC")), days) == 1  # 23:09 ET Mar 3


def test_walk_forward_picks_never_change_when_the_future_changes():
    """Cut the data at day T: every pick made on or before T (minus the unfinished last week) must be identical."""
    v = ml.Variant("t", 5, "weekly", False, "")
    full = zoo.build(bars())
    T = IDX[900]
    cut = zoo.build({k: x[x.index <= T] for k, x in bars().items()})
    W_full = ml.walk_forward(full, v, None, progress=lambda m: None)
    W_cut = ml.walk_forward(cut, v, None, progress=lambda m: None)
    a = W_full.loc[:IDX[890]]
    b = W_cut.reindex(columns=W_full.columns).fillna(0.0).loc[:IDX[890]]
    assert (a.sum(axis=1) > 0).sum() > 100  # the model actually traded in the test years
    pd.testing.assert_frame_equal(a, b, check_dtype=False)


def test_model_learns_a_planted_signal():
    data = zoo.build(bars(drift_strength=0.002))  # high-index stocks drift up: r252 / mom ranks predict
    v = ml.Variant("t", 21, "monthly", False, "")
    W = ml.walk_forward(data, v, None, progress=lambda m: None)
    held = W.loc["2019":].sum()
    top_half = held[[s for s in STOCKS[15:]]].sum()
    assert top_half > 0.8 * held[STOCKS].sum()  # mostly holds the drifting-up half


def test_news_features_count_parsed_events_without_lookahead():
    data = zoo.build(bars())
    t = IDX[500]
    items = [{"created_at": f"{t.date()}T14:00:00-05:00", "symbols": ["S03"],
              "headline": "S03 Q3 EPS $1.20 Beats $1.00 Estimate, Sales $5B Beat $4.8B Estimate"},
             {"created_at": f"{t.date()}T14:00:00-05:00", "symbols": ["S03", "S04"], "headline": "Roundup"}]
    f = ml.news_features(items, data)
    assert f["earnings_beat_5"].loc[t, "S03"] == 1 and f["earnings_beat_5"].loc[IDX[499], "S03"] == 0
    assert f["heads5"].loc[t, "S04"] == 0  # multi-ticker items never count
    assert f["last_surprise"].loc[IDX[510], "S03"] == pytest.approx(0.2)
