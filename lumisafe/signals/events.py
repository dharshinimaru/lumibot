"""Turn headlines into structured events (pure; generic patterns that work for any company).

Only headlines tagged with exactly ONE ticker become events, so roundups and
sector notes never count. Each event keeps the headline's publish time; the
study only trades it at the first market open at least 5 minutes later.

Event kinds (``value`` in brackets):
  earnings_beat / earnings_miss / earnings_inline   [EPS surprise vs estimate, fraction]
  sales_beat / sales_miss
  upgrade / downgrade                               [new rating]
  initiate_bullish / initiate_other                 [rating]
  pt_raise / pt_cut                                 [new target]
  guide_up / guide_down                             [guided EPS midpoint vs estimate, fraction]
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

BULLISH = {"buy", "strong buy", "outperform", "overweight", "accumulate", "positive", "sector outperform",
           "market outperform", "top pick", "conviction buy", "add"}
BEARISH = {"sell", "strong sell", "underperform", "underweight", "reduce", "negative", "sector underperform",
           "market underperform"}

_NUM = r"\$?\(?-?[\d,]*\.?\d+\)?"
# Two Benzinga styles: "Q1 EPS $0.44 Beats $0.26 Estimate" (2018+) and "Q4 EPS $1.80 vs $1.63 Est." /
# "Q1 EPS $0.95 vs. Est. $0.83" (2016-2017). YoY comparisons ("Up From $0.30 YoY") never count.
_EPS = re.compile(rf"\bQ[1-4]\b[^,;]*?\bEPS (?P<actual>{_NUM})"
                  rf"(?: (?:Beats|Misses|In-Line|Inline|Meets))?(?: (?:vs\.?|versus))?"
                  rf" (?:Est\.? |Estimate )?(?P<est>{_NUM})(?P<tail> (?:Estimate|Est\b\.?|est\b))?", re.I)
_SALES = re.compile(r"\b(?:Sales|Revenue) \$?[\d.,]+[BMK]? (?P<word>Beat|Miss|In-Line|Inline|Meet)", re.I)
_SALES_VS = re.compile(r"\b(?:Sales|Revenue|Rev\.) \$?(?P<a>[\d.,]+)(?P<au>[BMK])? vs\.? (?:Est\.? )?\$?(?P<e>[\d.,]+)"
                       r"(?P<eu>[BMK])?", re.I)
_ANALYST = re.compile(r"^(?P<firm>[A-Z][\w&.'/ -]{1,40}?) (?P<act>Upgrades|Downgrades) (?P<co>.+?) to "
                      r"(?P<rating>[A-Za-z][A-Za-z -]{1,25}?)(?:,|$| Rating| From| and)")
_INIT = re.compile(r"Initiates Coverage [Oo]n .+? (?:[Ww]ith|[Aa]t) (?P<rating>[A-Za-z][A-Za-z -]{1,25}?)"
                   r"(?: Rating|,|$)")
_PT = re.compile(r"(?P<dir>Raises|Lowers) (?:Price Target|PT) [Tt]o \$(?P<pt>[\d,.]+)")
_GUIDE_RAISE = re.compile(r"\bRaises (?:FY|Q)\d*[^,;]*?(?:Guidance|Outlook|EPS|Sales|Revenue)", re.I)
_GUIDE_CUT = re.compile(r"\b(?:Lowers|Cuts) (?:FY|Q)\d*[^,;]*?(?:Guidance|Outlook|EPS|Sales|Revenue)", re.I)
_SEES = re.compile(rf"\bSees (?:Q[1-4]|FY\d+) (?:Adj\.? )?EPS (?P<lo>{_NUM})(?:\s*-\s*(?P<hi>{_NUM}))? [Vv]s\.? "
                   rf"(?P<est>{_NUM}) Est", re.I)
_ROUNDUP = re.compile(r"^(Benzinga's|Top \d|\d+ Stocks|Stocks To Watch|Earnings Scheduled|Mid-Day|Pre-Market)", re.I)


@dataclass(frozen=True)
class Event:
    symbol: str
    at: datetime
    kind: str
    value: float | str | None
    headline: str


def _num(s: str | None) -> float | None:
    if s is None:
        return None
    neg = "(" in s or s.strip().startswith("-") or s.strip().startswith("$-")
    try:
        v = float(re.sub(r"[^\d.]", "", s))
    except ValueError:
        return None
    return -v if neg else v


def _scaled(num: str, unit: str | None) -> float | None:
    v = _num(num)
    return None if v is None else v * {"B": 1e9, "M": 1e6, "K": 1e3}.get((unit or "").upper(), 1.0)


def _surprise(actual: float | None, est: float | None) -> float | None:
    if actual is None or est is None:
        return None
    return (actual - est) / max(abs(est), 0.01)


def parse(item: dict) -> list[Event]:
    """Events in one headline item ``{"created_at", "headline", "symbols"}``."""
    syms = item.get("symbols") or []
    h = item.get("headline") or ""
    if len(syms) != 1 or not h or _ROUNDUP.search(h):
        return []
    sym = syms[0]
    at = datetime.fromisoformat(item["created_at"])
    out: list[Event] = []

    def add(kind, value=None):
        out.append(Event(sym, at, kind, value, h))

    m = _EPS.search(h)
    text = h[max(0, m.start() - 40):m.end()].lower() if m else ""  # include the words just before "Q1"
    forecast = re.search(r"\b(sees|guid|outlook|raises|lowers|affirms|reaffirms|expects|forecast)", text)
    vs_estimate = m is not None and "est" in text and "yoy" not in text and not forecast  # results, not forecasts
    if vs_estimate:
        s = _surprise(_num(m.group("actual")), _num(m.group("est")))
        if s is not None:
            add("earnings_beat" if s > 0.005 else "earnings_miss" if s < -0.005 else "earnings_inline", s)
    m = _SALES.search(h)
    if m and out:
        w = m.group("word").lower()
        add("sales_beat" if w == "beat" else "sales_miss" if w == "miss" else "sales_inline")
    elif out and (m2 := _SALES_VS.search(h)):
        a, e = _scaled(m2.group("a"), m2.group("au")), _scaled(m2.group("e"), m2.group("eu") or m2.group("au"))
        if a is not None and e:
            add("sales_beat" if a > e * 1.0005 else "sales_miss" if a < e * 0.9995 else "sales_inline")
    m = _ANALYST.search(h)
    if m:
        add("upgrade" if m.group("act") == "Upgrades" else "downgrade", m.group("rating").strip().lower())
    m = _INIT.search(h)
    if m:
        r = m.group("rating").strip().lower()
        add("initiate_bullish" if r in BULLISH else "initiate_other", r)
    m = _PT.search(h)
    if m:
        add("pt_raise" if m.group("dir") == "Raises" else "pt_cut", _num(m.group("pt")))
    m = _SEES.search(h)
    if m:
        lo, hi, est = _num(m.group("lo")), _num(m.group("hi")), _num(m.group("est"))
        mid = (lo + hi) / 2 if lo is not None and hi is not None else lo
        s = _surprise(mid, est)
        if s is not None and abs(s) > 0.005:
            add("guide_up" if s > 0 else "guide_down", s)
    elif _GUIDE_RAISE.search(h):
        add("guide_up", None)
    elif _GUIDE_CUT.search(h):
        add("guide_down", None)
    return out


def parse_all(items: list[dict]) -> list[Event]:
    seen, out = set(), []
    for it in items:
        for e in parse(it):
            key = (e.symbol, e.kind, e.headline)
            if key not in seen:  # the same headline is often re-published ("UPDATE: ...")
                seen.add(key)
                out.append(e)
    return out
