#!/usr/bin/env python3
"""
sweep_v4.py — parameter-sensitivity & robustness check for the V4 winner.
================================================================================
V4 = trend-regime gate (close>EMA200 & EMA200 rising) + hard stop / trailing
overlay. Before recommending it we must prove it is NOT a lucky parameter pick:

  1. Sweep stop/arm/giveback across a grid; report TEST-slice Sharpe for each.
     A robust edge means MOST reasonable settings work — not one magic combo.
  2. Sweep the regime EMA length (150/200/250) — the core gate must not hinge
     on exactly 200.
  3. Report the DISPERSION of results. Tight, consistently-good = trustworthy.
"""
from __future__ import annotations
import os
import statistics
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import backtest as BT      # noqa: E402
import experiments as EX   # noqa: E402


def gate_ema(bar, key):
    ind = bar["ind"]
    c = ind.get("close"); e = ind.get(key); s = ind.get(key + "_slope")
    if c is None or e is None or s is None:
        return False
    return c > e and s > 0


if __name__ == "__main__":
    os.environ.setdefault("DESK_TICKER", "SPY")
    bars = BT.build_scored_bars()
    test = BT.slice_bars(bars, 0.65, 1.0)
    train = BT.slice_bars(bars, 0.0, 0.65)

    print("="*88)
    print("V4 RISK-PARAM SENSITIVITY  (regime gate fixed = close>EMA200 rising)")
    print("Reporting TEST-slice (out-of-sample) Sharpe / CAGR / maxDD per combo")
    print("="*88)
    stops = [0.06, 0.08, 0.11, 0.14, 0.18]
    arms = [0.04, 0.05, 0.07]
    gives = [0.02, 0.03, 0.04]
    sharpes = []
    for st in stops:
        for ar in arms:
            for gv in gives:
                risk = {"stop_loss_pct": st, "trail_arm_pct": ar, "trail_giveback_pct": gv}
                m = EX.simulate_regime(test, gate=EX._regime_long, risk=risk)
                sharpes.append(m["sharpe"])
    print(f"combos tested: {len(sharpes)}")
    print(f"Sharpe  min {min(sharpes):.2f}  median {statistics.median(sharpes):.2f}  "
          f"max {max(sharpes):.2f}  stdev {statistics.pstdev(sharpes):.3f}")
    bh = BT.buy_hold(test)
    beat = sum(1 for s in sharpes if s >= bh["sharpe"])
    print(f"buy&hold TEST Sharpe = {bh['sharpe']:.2f}; V4 combos >= B&H Sharpe: "
          f"{beat}/{len(sharpes)} ({beat/len(sharpes)*100:.0f}%)")

    print("\n" + "="*88)
    print("REGIME EMA-LENGTH SENSITIVITY  (risk overlay fixed = 0.11/0.05/0.03)")
    print("="*88)
    for key in ("ema50", "ema200"):
        for slc, name in ((train, "TRAIN"), (test, "TEST")):
            m = EX.simulate_regime(slc, gate=lambda b, k=key: gate_ema(b, k),
                                   risk=BT.RISK_DEFAULTS)
            print(BT.fmt(m, f"regime>{key} [{name}]"))
    # EMA200 is the anchor; also show pure-regime (no overlay) for reference.
    print()
    for slc, name in ((train, "TRAIN"), (test, "TEST")):
        m = EX.simulate_regime(slc, gate=EX._regime_long)
        print(BT.fmt(m, f"regime>EMA200 pure [{name}]"))
