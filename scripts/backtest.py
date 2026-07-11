#!/usr/bin/env python3
"""
backtest.py — honest, out-of-sample backtest harness for the Agentic Trading Desk.
================================================================================
Purpose: measure whether the three-pillar strategy ACTUALLY makes money vs a
buy-and-hold benchmark, and test evidence-led variants — WITHOUT curve-fitting.

Design principles (kept deliberately strict):
  * Reuse the REAL decision engine (score.decide / score.score_trend/momentum)
    computed on point-in-time windows (no lookahead). The risk overlay is a thin
    mechanical re-implementation of run_desk's overlay so stop/arm/giveback are
    tunable per variant.
  * Two-phase for speed + reproducibility:
      Phase A (expensive, once): compute the indicator dict per bar over all
        history and CACHE it to disk (spy_scored_bars.json). O(n^2), ~minutes.
      Phase B (cheap, many times): replay any entry/exit/risk RULE over the
        cached bars. Variants that don't change indicator MATH reuse the cache.
  * ALWAYS report the buy-and-hold benchmark next to every strategy. If B&H
    wins, that is the headline — no cherry-picking.
  * Train/test split to catch overfitting: parameters are chosen on the TRAIN
    slice and reported on the untouched TEST slice.

Metrics: total return, CAGR, annualized Sharpe (rf=0), max drawdown, time in
market (exposure), number of round-trip trades, win rate, avg win/loss.

Data: Robinhood MCP daily SPY closes (dates preserved). Cached raw to
spy_raw_history.json so variant iteration never re-hits the API.

Signal-only research. Places no orders. Writes only local cache/report files.
"""
from __future__ import annotations

import json
import math
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import indicators as I   # noqa: E402
import score as S        # noqa: E402

TICKER = os.environ.get("DESK_TICKER", "SPY")
RAW_CACHE = SCRIPT_DIR / f"spy_raw_history_{TICKER}.json"
BARS_CACHE = SCRIPT_DIR / f"spy_scored_bars_{TICKER}.json"
MIN_BARS = 220           # need EMA200 warm; matches live engine's ideal
LOOKBACK_DAYS = 4600     # ~12.5 years of calendar days
TRADING_DAYS = 252

RISK_DEFAULTS = {"stop_loss_pct": 0.11, "trail_arm_pct": 0.05,
                 "trail_giveback_pct": 0.03}
ENTRY_ACTIONS = {"RE-ENTRY (new cycle)", "TACTICAL REBOUND (counter-trend)"}
EXIT_ACTIONS = {"EXIT / TRIM", "EXIT"}


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------
def fetch_raw(force: bool = False) -> list[tuple[str, float]]:
    """Return [(date, close)] old->new. Cache to disk."""
    if RAW_CACHE.exists() and not force:
        raw = json.loads(RAW_CACHE.read_text())
        return [(d, float(c)) for d, c in raw]
    import rh_data
    data = rh_data.RHData()
    start = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
             ).strftime("%Y-%m-%dT00:00:00Z")
    res = data.mcp.call_tool("get_equity_historicals", {
        "symbols": [TICKER], "start_time": start,
        "interval": "day", "bounds": "regular", "adjustment_type": "split",
    })
    seq = []
    for r in res.get("data", {}).get("results", []):
        for b in r.get("bars", []):
            d = (b.get("begins_at") or "")[:10]
            cp = b.get("close_price")
            if d and cp is not None:
                seq.append((d, float(cp)))
    seq.sort(key=lambda x: x[0])
    RAW_CACHE.write_text(json.dumps(seq))
    return seq


def build_scored_bars(force: bool = False) -> list[dict]:
    """Phase A: compute the indicator dict per bar (point-in-time). Cache it."""
    if BARS_CACHE.exists() and not force:
        return json.loads(BARS_CACHE.read_text())
    seq = fetch_raw(force=force)
    closes = [c for _, c in seq]
    dates = [d for d, _ in seq]
    n = len(closes)
    if n < MIN_BARS + 30:
        raise SystemExit(f"Only {n} bars; need >= {MIN_BARS+30}.")
    bars = []
    for i in range(MIN_BARS, n):
        window = closes[:i + 1]
        ind = I.compute(window)
        t, _ = S.score_trend(ind)
        m, _ = S.score_momentum(ind)
        bars.append({
            "date": dates[i], "close": closes[i],
            "trend": t, "momentum": m, "ind": _json_ind(ind),
        })
    BARS_CACHE.write_text(json.dumps(bars))
    return bars


def _json_ind(ind: dict) -> dict:
    """Keep only JSON-serializable scalar fields score.decide/_flags need."""
    keys = ("close", "ema20", "ema50", "ema200", "ema20_slope", "ema200_slope",
            "rsi14", "rsi14_prev", "macd_hist", "macd_hist_prev", "trix",
            "trix_signal", "trix_prev", "trix_signal_prev", "percent_b",
            "bars_since_below_ema20")
    return {k: ind.get(k) for k in keys}


# --------------------------------------------------------------------------
# Simulation (Phase B) — replay a rule over cached bars
# --------------------------------------------------------------------------
def simulate(bars: list[dict], *, macro_for=None, risk=RISK_DEFAULTS,
             entry_gate: Optional[Callable] = None,
             exit_gate: Optional[Callable] = None,
             min_hold: int = 0) -> dict:
    """Long/flat simulation.

    Uses the REAL score.decide() for base actions on point-in-time indicators.
    entry_gate(bar, ctx)->bool : extra filter ANDed with the engine's entry.
    exit_gate(bar, ctx)->bool  : force-exit override (e.g. time/reversal exit).
    macro_for(bar)->int|None   : optional macro pillar per bar (default None).
    risk: stop_loss_pct / trail_arm_pct / trail_giveback_pct.
    min_hold: minimum bars to hold before non-risk exits (avoids churn).
    """
    stop = risk["stop_loss_pct"]; arm = risk["trail_arm_pct"]
    give = risk["trail_giveback_pct"]

    holding = False
    entry_price = peak = None
    entry_idx = None
    trades = []
    daily_ret = []          # strategy daily return (0 when flat)
    exposure_days = 0

    for i, bar in enumerate(bars):
        px = bar["close"]
        ind = bar["ind"]
        macro = macro_for(bar) if macro_for else None
        # daily return contribution: holding INTO today captures today's move
        if i > 0 and holding:
            prev = bars[i - 1]["close"]
            daily_ret.append(px / prev - 1.0)
            exposure_days += 1
        else:
            daily_ret.append(0.0)

        dec = S.decide(ind, bar["trend"], bar["momentum"], macro, holding)
        base = dec["action"]

        if holding:
            peak = max(peak, px)
            unreal = px / entry_price - 1.0
            armed = peak / entry_price - 1.0 >= arm
            dd_from_peak = px / peak - 1.0
            held = i - entry_idx
            do_exit = None
            if unreal <= -stop:
                do_exit = "stop"
            elif armed and dd_from_peak <= -give:
                do_exit = "trail"
            elif base in EXIT_ACTIONS and held >= min_hold:
                do_exit = "framework"
            elif exit_gate and exit_gate(bar, {"held": held, "unreal": unreal,
                                               "entry_price": entry_price}) \
                    and held >= min_hold:
                do_exit = "rule"
            if do_exit:
                trades.append({"entry": entry_price, "exit": px,
                               "ret": unreal, "bars": held, "why": do_exit,
                               "entry_date": bars[entry_idx]["date"],
                               "exit_date": bar["date"]})
                holding = False
                entry_price = peak = entry_idx = None
        else:
            want = base in ENTRY_ACTIONS
            if entry_gate is not None:
                want = want and entry_gate(bar, {})
            if want:
                holding = True
                entry_price = peak = px
                entry_idx = i

    # close any open position at last price (mark-out)
    if holding:
        last = bars[-1]
        unreal = last["close"] / entry_price - 1.0
        trades.append({"entry": entry_price, "exit": last["close"],
                       "ret": unreal, "bars": len(bars) - 1 - entry_idx,
                       "why": "mark_out", "entry_date": bars[entry_idx]["date"],
                       "exit_date": last["date"]})

    return _metrics(bars, daily_ret, trades, exposure_days)


def _metrics(bars, daily_ret, trades, exposure_days) -> dict:
    n = len(bars)
    years = n / TRADING_DAYS
    # strategy equity
    eq = 1.0
    curve = []
    for r in daily_ret:
        eq *= (1 + r)
        curve.append(eq)
    total = eq - 1.0
    cagr = eq ** (1 / years) - 1.0 if years > 0 else 0.0
    sharpe = _sharpe(daily_ret)
    mdd = _max_dd(curve)
    wins = [t["ret"] for t in trades if t["ret"] > 0]
    losses = [t["ret"] for t in trades if t["ret"] <= 0]
    return {
        "total_return_pct": total * 100,
        "cagr_pct": cagr * 100,
        "sharpe": sharpe,
        "max_dd_pct": mdd * 100,
        "exposure_pct": exposure_days / n * 100 if n else 0,
        "n_trades": len(trades),
        "win_rate_pct": (len(wins) / len(trades) * 100) if trades else 0.0,
        "avg_win_pct": (statistics.mean(wins) * 100) if wins else 0.0,
        "avg_loss_pct": (statistics.mean(losses) * 100) if losses else 0.0,
        "years": years,
        "final_equity": eq,
        "_curve": curve,
        "_trades": trades,
    }


def buy_hold(bars: list[dict]) -> dict:
    daily = [0.0]
    for i in range(1, len(bars)):
        daily.append(bars[i]["close"] / bars[i - 1]["close"] - 1.0)
    m = _metrics(bars, daily, [], len(bars) - 1)
    m["exposure_pct"] = 100.0
    return m


def _sharpe(daily: list[float]) -> float:
    xs = [r for r in daily]
    if len(xs) < 2:
        return 0.0
    sd = statistics.pstdev(xs)
    if sd == 0:
        return 0.0
    return statistics.mean(xs) / sd * math.sqrt(TRADING_DAYS)


def _max_dd(curve: list[float]) -> float:
    peak = -1e9; mdd = 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    return mdd


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------
def fmt(m: dict, label: str) -> str:
    return (f"{label:<28} "
            f"ret {m['total_return_pct']:+7.1f}%  "
            f"CAGR {m['cagr_pct']:+6.2f}%  "
            f"Sharpe {m['sharpe']:+5.2f}  "
            f"maxDD {m['max_dd_pct']:6.1f}%  "
            f"expo {m['exposure_pct']:4.0f}%  "
            f"trades {m['n_trades']:>3}  "
            f"win {m['win_rate_pct']:3.0f}%")


def slice_bars(bars, frac_start=0.0, frac_end=1.0):
    a = int(len(bars) * frac_start)
    b = int(len(bars) * frac_end)
    return bars[a:b]


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="refetch + rescore")
    ap.add_argument("--rescore", action="store_true", help="rescore from raw")
    args = ap.parse_args()
    bars = build_scored_bars(force=args.refresh or args.rescore)
    print(f"Scored bars: {len(bars)}  ({bars[0]['date']} → {bars[-1]['date']})")
    bh = buy_hold(bars)
    base = simulate(bars)
    print(fmt(bh, "BUY & HOLD"))
    print(fmt(base, "CURRENT STRATEGY"))
