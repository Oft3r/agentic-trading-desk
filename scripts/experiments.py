#!/usr/bin/env python3
"""
experiments.py — evidence-led strategy variants vs current engine vs buy-hold.
================================================================================
Runs on the cached scored bars from backtest.py (no API re-hit, no re-score).
Every variant is a FEW-parameter, economically-motivated rule — not a curve-fit.
Each is reported on TRAIN (first 65%) and TEST (last 35%) slices so overfitting
is visible: a variant that only shines on TRAIN is rejected.

Variants (each with a one-line economic thesis):
  V0  current            : the live engine (counter-trend rebounds). Baseline.
  V1  regime_long        : long while close>EMA200 & EMA200 rising, else flat.
                           Thesis: capture equity drift, sidestep bear regimes.
                           (The single most robust equity-index edge.)
  V2  regime_momentum    : V1 AND momentum pillar >=0 to enter; exit when
                           close<EMA50. Thesis: add a faster de-risk trigger.
  V3  regime_dtrend      : long while close>EMA200 rising AND close>EMA20
                           (dual timeframe). Exit when close<EMA20 for de-risk.
  V4  regime_risk        : V1 entries but keep the hard stop / trailing overlay.
                           Thesis: does discretionary risk help a regime filter?
Benchmarks: BUY & HOLD and V0 current on the SAME slice.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import backtest as BT  # noqa: E402


def _regime_long(bar) -> bool:
    """close above a rising EMA200 — the core trend-regime gate."""
    ind = bar["ind"]
    c = ind.get("close"); e200 = ind.get("ema200"); s200 = ind.get("ema200_slope")
    if c is None or e200 is None or s200 is None:
        return False
    return c > e200 and s200 > 0


def simulate_regime(bars, *, gate, exit_when=None, risk=None):
    """Long/flat purely by a regime gate (bypasses score.decide entirely).
    gate(bar)->bool: hold long today if True.
    exit_when(bar)->bool: optional faster exit (OR-ed with gate going False).
    risk: optional stop/arm/giveback overlay dict; None = pure regime.
    """
    TD = BT.TRADING_DAYS
    holding = False
    entry_price = peak = None
    entry_idx = None
    trades = []
    daily_ret = []
    exposure = 0
    stop = arm = give = None
    if risk:
        stop, arm, give = risk["stop_loss_pct"], risk["trail_arm_pct"], risk["trail_giveback_pct"]

    for i, bar in enumerate(bars):
        px = bar["close"]
        if i > 0 and holding:
            daily_ret.append(px / bars[i - 1]["close"] - 1.0)
            exposure += 1
        else:
            daily_ret.append(0.0)

        want_regime = gate(bar)
        if holding:
            peak = max(peak, px)
            unreal = px / entry_price - 1.0
            exit_now = False; why = None
            if risk and unreal <= -stop:
                exit_now, why = True, "stop"
            elif risk and (peak / entry_price - 1.0 >= arm) and (px / peak - 1.0 <= -give):
                exit_now, why = True, "trail"
            elif not want_regime:
                exit_now, why = True, "regime_off"
            elif exit_when and exit_when(bar):
                exit_now, why = True, "fast_exit"
            if exit_now:
                trades.append({"entry": entry_price, "exit": px, "ret": unreal,
                               "bars": i - entry_idx, "why": why,
                               "entry_date": bars[entry_idx]["date"],
                               "exit_date": bar["date"]})
                holding = False; entry_price = peak = entry_idx = None
        else:
            if want_regime:
                holding = True; entry_price = peak = px; entry_idx = i

    if holding:
        last = bars[-1]
        unreal = last["close"] / entry_price - 1.0
        trades.append({"entry": entry_price, "exit": last["close"], "ret": unreal,
                       "bars": len(bars) - 1 - entry_idx, "why": "mark_out",
                       "entry_date": bars[entry_idx]["date"], "exit_date": last["date"]})
    return BT._metrics(bars, daily_ret, trades, exposure)


def _below(ind_key, ref_key):
    def f(bar):
        ind = bar["ind"]
        a, b = ind.get(ind_key), ind.get(ref_key)
        return a is not None and b is not None and a < b
    return f


def run_slice(bars, title):
    print(f"\n{'='*104}\n{title}   ({bars[0]['date']} → {bars[-1]['date']}, {len(bars)} bars)\n{'='*104}")
    rows = []
    rows.append(("BUY & HOLD", BT.buy_hold(bars)))
    rows.append(("V0 current engine", BT.simulate(bars)))
    rows.append(("V1 regime_long", simulate_regime(bars, gate=_regime_long)))
    rows.append(("V2 regime+EMA50 exit", simulate_regime(
        bars, gate=_regime_long, exit_when=_below("close", "ema50"))))
    rows.append(("V3 regime+EMA20 exit", simulate_regime(
        bars, gate=_regime_long, exit_when=_below("close", "ema20"))))
    rows.append(("V4 regime+risk overlay", simulate_regime(
        bars, gate=_regime_long, risk=BT.RISK_DEFAULTS)))
    for label, m in rows:
        print(BT.fmt(m, label))
    return dict(rows)


if __name__ == "__main__":
    os.environ.setdefault("DESK_TICKER", "SPY")
    bars = BT.build_scored_bars()
    run_slice(bars, "FULL HISTORY")
    train = BT.slice_bars(bars, 0.0, 0.65)
    test = BT.slice_bars(bars, 0.65, 1.0)
    run_slice(train, "TRAIN (first 65%)")
    run_slice(test, "TEST / OUT-OF-SAMPLE (last 35%)")
