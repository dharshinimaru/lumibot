"""Read-only local dashboard (SPEC §9). Run: ``streamlit run lumisafe/reporting/dashboard.py``.

It never places orders and never writes anything. It only reads the journal,
state, config and registry, plus benchmark prices. There is deliberately no
win-rate metric anywhere.
"""

from __future__ import annotations

from datetime import timedelta
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


def main() -> None:  # pragma: no cover - exercised through streamlit's AppTest
    import streamlit as st

    render(st, load_data())


if __name__ == "__main__":
    main()
