"""Bot factory dashboard (read-only). Run: ``lumisafe factory dashboard``
(or ``streamlit run lumisafe/reporting/factory_dashboard.py``).

Reads ``~/.lumisafe/factory/state.json``, which ``lumisafe factory run`` rewrites every few minutes
while the market is open. Refreshes itself every minute. Never places orders.
"""

from __future__ import annotations

import time

import pandas as pd

from lumisafe import DISCLAIMER
from lumisafe.factory import engine

REFRESH_SECONDS = 60


def _pct(x):
    return None if x is None else round(100 * x, 2)


def table(state: dict) -> pd.DataFrame:
    rows = []
    for b in state["bots"]:
        m = b["metrics"] or {}
        bt = (b.get("backtest") or {}).get("discovery") or {}
        rows.append({
            "bot": b["name"], "type": b["kind"], "horizon": b["horizon"], "vs": b["benchmark"],
            "today % (live)": _pct(b.get("live_today")),
            "5 days %": _pct(m.get("last_5d")), "20 days %": _pct(m.get("last_20d")),
            "since start %": _pct(m.get("total")), "benchmark since start %": _pct(m.get("bench_total")),
            "ahead of benchmark %": _pct(m.get("excess")), "worst drop %": _pct(m.get("max_dd")),
            "days live": m.get("days", 0), "verdict": b["verdict"],
            "backtest 2017-23 excess %/yr": _pct(bt.get("excess_ann")),
        })
    df = pd.DataFrame(rows)
    return df.sort_values(["ahead of benchmark %", "since start %"], ascending=False, na_position="last")


def render(st, state: dict | None) -> None:
    st.set_page_config(page_title="Bot factory", layout="wide")
    st.title("LumiSafe bot factory (paper)")
    if state is None:
        st.info("No factory data yet. Start it in a terminal: `lumisafe factory run` (or `lumisafe factory once`).")
        return
    st.caption(f"Updated {state['updated_at']} · prices through the {state['as_of_close']} close (live marks "
               "during market hours) · every decision is frozen before the open it trades at.")
    df = table(state)
    c1, c2 = st.columns(2)
    kinds = c1.multiselect("Type", sorted(df["type"].unique()), default=sorted(df["type"].unique()))
    hz = c2.multiselect("Horizon", ["short", "long"], default=["short", "long"])
    view = df[df["type"].isin(kinds) & df["horizon"].isin(hz)]
    st.subheader("Leaderboard")
    st.dataframe(view, hide_index=True, width="stretch")
    st.caption("Nothing counts as evidence before 60 trading days. With many bots, some will lead by luck; "
               "'PROVEN' requires beating luck across all of them (deflated Sharpe ≥ 0.95).")

    st.subheader("Copy now: latest orders")
    any_changes = False
    for b in state["bots"]:
        if b["latest_changes"] and b["name"] in set(view["bot"]):
            any_changes = True
            with st.expander(f"{b['name']} · decided after the {b['latest_decision_day']} close · {b['verdict']}"):
                st.write(b["description"])
                for c in b["latest_changes"]:
                    st.write("• " + c)
                if b["holdings"]:
                    st.write("Holds now: " + ", ".join(f"{s} {w:.0%}" for s, w in b["holdings"].items()))
    if not any_changes:
        st.write("No orders yet.")

    st.subheader("Growth of $1 since each bot started")
    pick = st.multiselect("Bots", list(view["bot"]), default=list(view["bot"])[:5])
    series = {}
    for name in pick:
        pts = state["curves"].get(name)
        if pts:
            series[name] = pd.Series({pd.Timestamp(d): v for d, v in pts})
    for k, pts in (state.get("bench_curves") or {}).items():
        if pts:
            series[f"[{k}]"] = pd.Series({pd.Timestamp(d): v for d, v in pts})
    if series:
        st.line_chart(pd.DataFrame(series))
    else:
        st.write("Curves appear after the first full trading day.")
    st.caption(DISCLAIMER)


def main() -> None:  # pragma: no cover - streamlit entry point
    import streamlit as st

    render(st, engine.read_state())
    time.sleep(REFRESH_SECONDS)
    st.rerun()


if __name__ == "__main__":
    main()
