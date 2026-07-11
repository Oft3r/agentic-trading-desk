#!/usr/bin/env python3
"""
experiments_v2.py — whipsaw refinements of the live regime_v4 strategy.
================================================================================
regime_v4 (live) = long while close>EMA200 & EMA200 rising, flat otherwise,
+ stop/trailing overlay. Its known weakness: churn when price hovers around
the EMA200 line. Classic, few-parameter fixes tested here:

  R0 baseline      : live regime_v4 exactly (reference).
  R1 hysteresis    : enter when close > EMA200*(1+b), exit when close <
                     EMA200*(1-b). Asymmetric band kills line-hugging churn.
                     b tested at 1% and 2%.
  R2 confirmation  : flip regime state only after N consecutive days of the
                     new signal (N=3, 5). Slower but ignores 1-2 day fakeouts.
  R3 price-only    : close>EMA200 without the slope condition — does the
                     slope requirement actually add value?
  R4 month-end     : Faber GTAA style — evaluate the gate only on the last
                     bar of each month. Radically fewer decisions.

All variants keep the SAME stop/trailing overlay as live. Reported on TRAIN
(first 65%) and TEST (last 35%) — adoption requires beating R0 on the TEST
slice (Sharpe AND not-worse maxDD), not just on TRAIN.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import backtest as BT  # noqa: E402

RISK = BT.RISK_DEFAULTS


def _ind(bar):
    return bar["ind"]


def sim_stateful(bars, want_long_fn, *, risk=RISK):
    """Long/flat sim where want_long_fn(bar, i, holding, ctx) -> bool decides
    the DESIRED state each bar; ctx is a persistent dict for stateful gates
    (confirmation counters etc.). Risk overlay (stop/trail) still forces exits.
    """
    stop, arm, give = (risk["stop_loss_pct"], risk["trail_arm_pct"],
                       risk["trail_giveback_pct"])
    holding = False
    entry_price = peak = None
    entry_idx = None
    trades = []
    daily_ret = []
    exposure = 0
    ctx: dict = {}

    for i, bar in enumerate(bars):
        px = bar["close"]
        if i > 0 and holding:
            daily_ret.append(px / bars[i - 1]["close"] - 1.0)
            exposure += 1
        else:
            daily_ret.append(0.0)

        want = want_long_fn(bar, i, holding, ctx)

        if holding:
            peak = max(peak, px)
            unreal = px / entry_price - 1.0
            exit_now = None
            if unreal <= -stop:
                exit_now = "stop"
            elif (peak / entry_price - 1.0 >= arm) and (px / peak - 1.0 <= -give):
                exit_now = "trail"
            elif not want:
                exit_now = "regime_off"
            if exit_now:
                trades.append({"entry": entry_price, "exit": px, "ret": unreal,
                               "bars": i - entry_idx, "why": exit_now,
                               "entry_date": bars[entry_idx]["date"],
                               "exit_date": bar["date"]})
                holding = False
                entry_price = peak = entry_idx = None
        else:
            if want:
                holding = True
                entry_price = peak = px
                entry_idx = i

    if holding:
        last = bars[-1]
        unreal = last["close"] / entry_price - 1.0
        trades.append({"entry": entry_price, "exit": last["close"],
                       "ret": unreal, "bars": len(bars) - 1 - entry_idx,
                       "why": "mark_out",
                       "entry_date": bars[entry_idx]["date"],
                       "exit_date": last["date"]})
    return BT._metrics(bars, daily_ret, trades, exposure)


# ---------------------------------------------------------------- gates ----
def raw_regime(bar) -> bool | None:
    """The live gate: close > EMA200 AND EMA200 rising. None if not computable."""
    ind = _ind(bar)
    c, e, s = ind.get("close"), ind.get("ema200"), ind.get("ema200_slope")
    if c is None or e is None or s is None:
        return None
    return c > e and s > 0


def g_baseline(bar, i, holding, ctx):
    r = raw_regime(bar)
    return holding if r is None else r


def make_hysteresis(band: float):
    """Enter above EMA200*(1+band); exit below EMA200*(1-band); hold otherwise.
    Slope condition kept for entries (matches live), dropped for the exit test
    so the band alone decides exits (prevents slope-flicker churn)."""
    def g(bar, i, holding, ctx):
        ind = _ind(bar)
        c, e, s = ind.get("close"), ind.get("ema200"), ind.get("ema200_slope")
        if c is None or e is None:
            return holding
        if holding:
            return c > e * (1 - band)          # stay long until clearly below
        return c > e * (1 + band) and (s or 0) > 0   # enter only clearly above
    return g


def make_confirm(n: int):
    """Flip desired state only after n consecutive bars of the opposite signal."""
    def g(bar, i, holding, ctx):
        r = raw_regime(bar)
        if r is None:
            return holding
        state = ctx.setdefault("state", holding)
        run = ctx.setdefault("run", 0)
        if r != state:
            run += 1
            if run >= n:
                state, run = r, 0
        else:
            run = 0
        ctx["state"], ctx["run"] = state, run
        return state
    return g


def g_price_only(bar, i, holding, ctx):
    ind = _ind(bar)
    c, e = ind.get("close"), ind.get("ema200")
    if c is None or e is None:
        return holding
    return c > e


def g_month_end(bar, i, holding, ctx):
    """Re-evaluate the raw gate only when the month changes; hold state between."""
    month = bar["date"][:7]
    last_month = ctx.get("month")
    state = ctx.setdefault("state", holding)
    if month != last_month:            # first bar of a new month -> re-evaluate
        r = raw_regime(bar)
        if r is not None:
            state = r
        ctx["month"] = month
    ctx["state"] = state
    return state


VARIANTS = [
    ("R0 live regime_v4", g_baseline),
    ("R1 hysteresis 1%", make_hysteresis(0.01)),
    ("R1 hysteresis 2%", make_hysteresis(0.02)),
    ("R2 confirm 3d", make_confirm(3)),
    ("R2 confirm 5d", make_confirm(5)),
    ("R3 price-only", g_price_only),
    ("R4 month-end", g_month_end),
]


def run_slice(bars, title):
    print(f"\n{'='*104}\n{title}   ({bars[0]['date']} → {bars[-1]['date']}, {len(bars)} bars)\n{'='*104}")
    print(BT.fmt(BT.buy_hold(bars), "BUY & HOLD"))
    out = {}
    for label, gate in VARIANTS:
        m = sim_stateful(bars, gate)
        out[label] = m
        print(BT.fmt(m, label))
    return out


if __name__ == "__main__":
    os.environ.setdefault("DESK_TICKER", "SPY")
    bars = BT.build_scored_bars()
    run_slice(bars, "FULL HISTORY")
    run_slice(BT.slice_bars(bars, 0.0, 0.65), "TRAIN (first 65%)")
    run_slice(BT.slice_bars(bars, 0.65, 1.0), "TEST / OUT-OF-SAMPLE (last 35%)")
