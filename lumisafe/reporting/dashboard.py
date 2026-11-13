"""Read-only local dashboard (SPEC §9). Run: ``streamlit run lumisafe/reporting/dashboard.py``.

It never places orders and never writes anything. It only reads the journal,
state, config and registry, plus benchmark prices. There is deliberately no
win-rate metric anywhere.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pandas as pd

from lumisafe import DISCLAIMER, paths
from lumisafe.config.schema import load_config
from lumisafe.journal.journal import Journal
from lumisafe.reporting import benchmark as bm
from lumisafe.types import Side, Verdict


def _read_json(path) -> dict:
    import json

    return json.loads(path.read_text() or "{}") if path.is_file() else {}


def default_history(symbols, start=None, end=None):
    from lumisafe.adapters import lumibot_bridge as bridge

    return bridge.fetch_daily_history(symbols, start, end)


def veto_quality(journal: Journal) -> pd.DataFrame:
    """Average 20-day price move after held-back trades, by who held them back and the side."""
    rows = []
    for e in journal.query(final=[Verdict.VETO, Verdict.BLOCK]):
        if e.shadow_return_20d is None:
            continue
        who = "AI advisor" if e.final is Verdict.VETO else "risk/tax rules"
        rows.append({"held back by": who, "side": Side(e.intent.side).value,
                     "move_20d": e.shadow_return_20d})
    if not rows:
        return pd.DataFrame(columns=["held back by", "side", "trades", "avg 20-day price move after"])
    df = pd.DataFrame(rows)
    out = df.groupby(["held back by", "side"])["move_20d"].agg(["count", "mean"]).reset_index()
    out.columns = ["held back by", "side", "trades", "avg 20-day price move after"]
    return out


def load_data(history: Callable[..., dict] = default_history) -> dict[str, Any]:
    data: dict[str, Any] = {"disclaimer": DISCLAIMER}
    cfg_path = paths.config_path()
    data["config"] = load_config(cfg_path) if cfg_path.is_file() else None
    journal = Journal()
    state = _read_json(paths.state_path())
    data["snapshot"] = state.get("last_snapshot")
    data["halts"] = state.get("kill_switch", {})
    data["registry"] = _read_json(paths.registry_path())

    data["headline"] = None
    equity = bm.equity_from_journal(journal, mode="paper")
    data["equity"] = equity
    if data["config"] is not None and len(equity) >= 2:
        bench = data["config"].benchmark
        try:
            prices = history([bench], equity.index[0] - timedelta(days=7), None)[bench]["close"]
            cmp = bm.compare(equity, prices, bench)
            data["headline"] = cmp.headline() if cmp else None
        except Exception:
            data["headline"] = None

    data["latest_prices"], data["prices_asof"] = {}, None
    if data["config"] is not None:
        try:
            hist = history(sorted(data["config"].core.etfs), datetime.now(timezone.utc) - timedelta(days=14), None)
            for sym, df in hist.items():
                if df is not None and len(df):
                    data["latest_prices"][sym] = float(df["close"].iloc[-1])
                    data["prices_asof"] = max(filter(None, [data["prices_asof"], df.index[-1]]))
        except Exception:
            data["latest_prices"] = {}

    alloc = []
    snap, cfg = data["snapshot"], data["config"]
    if snap and cfg:
        total = snap["equity"] or 1
        for sym, target in cfg.core.etfs.items():
            value = snap["positions"].get(sym, 0.0) * snap["prices"].get(sym, 0.0)
            alloc.append({"fund": sym, "now (share of account)": value / total, "target (share of core)": target})
    data["allocation"] = pd.DataFrame(alloc)

    recent = journal.query(limit=25)
    data["journal"] = pd.DataFrame([
        {"date": e.ts.strftime("%Y-%m-%d"), "from": e.intent.source.value, "trade":
         f"{e.intent.side.value.upper()} {e.intent.quantity:g} {e.intent.symbol}", "result": e.final.value.upper(),
         "why": e.reviews[-1].explanation if e.reviews else ""}
        for e in reversed(recent)
    ])
    data["veto_quality"] = veto_quality(journal)
    return data


def render(st, data: dict[str, Any]) -> None:
    st.set_page_config(page_title="LumiSafe", layout="wide")
    st.title("LumiSafe")
    if data["config"] is None:
        st.info("No plan yet. Run `lumisafe init` in a terminal.")
    if data["config"] is not None:
        render_what_to_buy(st, data)
        st.divider()
    if data["headline"]:
        st.header(data["headline"])
    elif len(data["equity"]) >= 2:
        st.header(f"Your return: {bm.total_return(data['equity']):+.2%} · benchmark comparison unavailable")
    else:
        st.header("Not enough paper history yet for a return comparison.")
    if len(data["equity"]) >= 2:
        st.line_chart(data["equity"], y_label="Account value ($)")

    left, right = st.columns(2)
    with left:
        st.subheader("Allocation vs target")
        if data["allocation"].empty:
            st.write("No holdings yet.")
        else:
            st.dataframe(data["allocation"].style.format({"now (share of account)": "{:.1%}",
                                                          "target (share of core)": "{:.0%}"}),
                         hide_index=True)
    with right:
        st.subheader("Halts")
        halts = data["halts"]
        st.write(f"Satellite drawdown halt: **{'ON' if halts.get('satellite_latched') else 'off'}**")
        st.write(f"Account drawdown alert: **{'ON' if halts.get('account_alert_active') else 'off'}**")
        st.subheader("Strategies")
        reg = data["registry"]
        st.write("\n".join(f"- {n}: **{r['state']}**" for n, r in sorted(reg.items())) or "None backtested yet.")

    st.subheader("Recent decisions (taken and held back)")
    if data["journal"].empty:
        st.write("No decisions yet.")
    else:
        st.dataframe(data["journal"], hide_index=True)

    st.subheader("Were held-back trades worth holding back?")
    st.caption("Price change over the 20 trading days after a trade was held back. For held-back buys, a negative "
               "number means the hold-back avoided a drop; for held-back sells, a positive number means it avoided "
               "selling before a rise.")
    vq = data["veto_quality"]
    if vq.empty:
        st.write("Not enough history yet (needs 20 trading days after a held-back trade).")
    else:
        st.dataframe(vq.style.format({"avg 20-day price move after": "{:+.1%}"}), hide_index=True)

    st.divider()
    st.caption(data["disclaimer"])


def render_what_to_buy(st, data: dict[str, Any]) -> None:
    """Interactive, read-only: type in what you own and what you're adding; see what to buy and when."""
    from lumisafe.portfolio.advice import what_to_buy, when_text

    core = data["config"].core
    st.header("What to buy next")
    st.caption("For your own account. This page never places an order. Your plan: "
               + " · ".join(f"{w:.0%} {s}" for s, w in core.etfs.items()))
    prices = data["latest_prices"]
    if any(prices.get(s, 0) <= 0 for s in core.etfs):
        st.warning("Couldn't load recent prices right now, so there's no suggestion. Try reloading in a minute.")
        return

    snap = data["snapshot"] or {}
    start_from = st.radio("Start from", ["Shares I own (type them below)", "The paper account's holdings"],
                          horizontal=True, disabled=not snap)
    use_paper = bool(snap) and start_from.startswith("The paper")
    cols = st.columns(len(core.etfs) + 1)
    default_amount = float(core.monthly_contribution_usd or 100.0)
    amount = cols[0].number_input("Adding now ($)", min_value=0.0, value=default_amount, step=50.0)
    held = {}
    for col, sym in zip(cols[1:], core.etfs):
        paper_qty = float(snap.get("positions", {}).get(sym, 0.0)) if use_paper else 0.0
        held[sym] = col.number_input(f"{sym} shares owned", min_value=0.0, value=paper_qty, step=1.0,
                                     disabled=use_paper, key=f"held_{sym}_{use_paper}")
    whole = st.checkbox("My broker only sells whole shares")

    advice = what_to_buy(core, held, prices, amount, fractional=not whole)
    asof = data["prices_asof"]
    if advice.lines:
        rows = [{"action": l.side.upper(), "fund": l.symbol,
                 "shares": f"{l.shares:g}" if whole else f"{l.shares:.4f}",
                 "about": f"${l.dollars:,.2f}",
                 "your mix now → after": f"{advice.weights_before[l.symbol]:.0%} → {advice.weights_after[l.symbol]:.0%}",
                 "target": f"{advice.targets[l.symbol]:.0%}"}
                for l in sorted(advice.lines, key=lambda l: (l.side != "sell", -l.dollars))]
        st.dataframe(pd.DataFrame(rows), hide_index=True)
        if advice.leftover >= 0.01:
            st.write(f"Left over: ${advice.leftover:,.2f}. Keep it for next month.")
    else:
        st.write("Nothing to buy with this amount.")
    st.markdown("**When:** " + when_text(datetime.now(timezone.utc).date()))
    st.write("Then do nothing until next month. The plan doesn't react to prices going up and down.")
    for n in advice.notes:
        st.info(n)
    if asof is not None:
        st.caption(f"Prices from the {asof:%b %-d} close; share counts are approximate because prices move.")


def main() -> None:  # pragma: no cover - exercised through streamlit's AppTest
    import streamlit as st

    render(st, load_data())


if __name__ == "__main__":
    main()
