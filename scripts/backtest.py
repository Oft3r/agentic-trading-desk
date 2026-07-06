#!/usr/bin/env python3
"""
backtest.py
===========
Event-driven replay of the EXACT production rules in score.py — not a
re-implementation. Every bar, the scorer sees only the prefix of history up
to that bar (no lookahead by construction) and its decision is executed with
a configurable lag and cost.

Fill model (conservative by default):
  - decisions computed on close[t]; fills at close[t + lag] (default lag=1:
    tomorrow's close, since the desk trades after the daily close)
  - slippage `cost_bps` charged per side, plus optional ATR trailing stop
    exits (the chandelier stop from the risk block, checked before signals)

Anti-curve-fitting design:
  - the rules take NO fitted parameters, so there is no train set; instead
    rigor = stability. `--splits N` reports metrics per contiguous segment
    (walk-forward style): a strategy that only works in one segment is
    curve-fit to that era.
  - `--sensitivity` reruns the whole test across a lag x cost grid.
    Edge that dies with 1 extra day of lag or 10 bps of slippage is not edge.
  - buy & hold on the same window is always reported as the null hypothesis.

Entry actions: RE-ENTRY (new cycle), TACTICAL REBOUND (counter-trend).
Exit actions:  EXIT / TRIM, EXIT (full exit — the backtest holds 0 or 1 unit).

stdlib only. Deterministic. Input JSON: {"symbol", "close":[...],
"high":[...]?, "low":[...]?, "dates":[...]?}
"""
from __future__ import annotations
import argparse
import json
import math
import sys
from typing import Optional

import score as S
import volatility as V

ENTRY_ACTIONS = {"RE-ENTRY (new cycle)", "TACTICAL REBOUND (counter-trend)"}
EXIT_ACTIONS = {"EXIT / TRIM", "EXIT"}
TRADING_DAYS = 252


# --------------------------------------------------------------------------
# Core replay
# --------------------------------------------------------------------------

def run(close: list[float], high: Optional[list[float]] = None,
        low: Optional[list[float]] = None, dates: Optional[list[str]] = None,
        warmup: int = 220, lag: int = 1, cost_bps: float = 5.0,
        use_stop: bool = True, stop_mult: float = 3.0) -> dict:
    n = len(close)
    if n < warmup + 10:
        raise ValueError(f"need >= {warmup + 10} bars, got {n}")

    def d(i: int) -> str:
        return dates[i] if dates and i < len(dates) else f"bar {i}"

    cost = cost_bps / 1e4
    pos = 0                      # 0 flat, 1 long (the desk holds 0 or 1 unit)
    entry_px = entry_i = None
    tactical = False
    stop_level: Optional[float] = None
    pending: Optional[dict] = None   # scheduled fill: {"i", "kind", "reason"}
    trades: list[dict] = []
    strat_ret: list[float] = []      # daily strategy returns, bars warmup..n-1
    exposure_bars = 0

    for i in range(warmup, n):
        # Mark-to-market: bar i return accrues if long through (i-1, i]
        r = close[i] / close[i - 1] - 1.0 if pos == 1 else 0.0

        # Process a fill scheduled for this bar (executes AT close[i])
        if pending and pending["i"] == i:
            if pending["kind"] == "entry" and pos == 0:
                pos = 1
                entry_px = close[i] * (1 + cost)
                entry_i = i
                tactical = pending["reason"].startswith("TACTICAL")
                r -= cost
                trades.append({"entry_date": d(i),
                               "entry_px": round(entry_px, 4),
                               "signal": pending["reason"], "open": True})
                # Initial stop from data known at signal time
                sig_i = i - lag
                atr = V.atr_wilder(close[:sig_i + 1], 14,
                                   high[:sig_i + 1] if high else None,
                                   low[:sig_i + 1] if low else None)
                if tactical and atr:
                    stop_level = close[sig_i] - 1.5 * atr
                else:
                    stop_level = V.chandelier_stop(
                        close[:sig_i + 1], atr, 22, stop_mult,
                        high[:sig_i + 1] if high else None)
            elif pending["kind"] == "exit" and pos == 1:
                r -= cost   # held through this bar, pay cost at the close
                exit_px = close[i] * (1 - cost)
                trades.append(_close_trade(trades, entry_px, exit_px,
                                           entry_i, i, d, pending["reason"]))
                pos, entry_px, stop_level, tactical = 0, None, None, False
            pending = None

        strat_ret.append(r)
        if pos == 1:
            exposure_bars += 1

        # Decide at close[i]; fill lands at close[i+lag]
        if pending is not None or i + lag >= n:
            continue
        prefix = close[:i + 1]
        h_pre = high[:i + 1] if high else None
        l_pre = low[:i + 1] if low else None

        if pos == 1 and use_stop:
            # ATR trailing stop checked before the signal cascade
            if stop_level is not None and close[i] < stop_level:
                pending = {"i": i + lag, "kind": "exit", "reason": "ATR stop"}
                continue
            # Ratchet the chandelier up (never down); tactical stops stay fixed
            if not tactical:
                atr = V.atr_wilder(prefix, 14, h_pre, l_pre)
                ch = V.chandelier_stop(prefix, atr, 22, stop_mult, h_pre)
                if ch is not None:
                    stop_level = max(stop_level or ch, ch)

        card = S.score_symbol(prefix, macro_score=None, holding=pos == 1,
                              high=h_pre, low=l_pre, with_garch=False)
        action = card["decision"]["action"]
        if pos == 1 and action in EXIT_ACTIONS:
            pending = {"i": i + lag, "kind": "exit", "reason": action}
        elif pos == 0 and action in ENTRY_ACTIONS:
            pending = {"i": i + lag, "kind": "entry", "reason": action}

    # Force-close any open position at the last bar for reporting
    if pos == 1 and entry_px:
        exit_px = close[-1] * (1 - cost)
        strat_ret[-1] -= cost
        trades.append(_close_trade(trades, entry_px, exit_px,
                                   entry_i, n - 1, d, "end of data (mark)"))

    closed = [t for t in trades if not t.get("open")]
    bh_ret = close[-1] / close[warmup] - 1.0
    bars = len(strat_ret)
    # Equity curves (both indexed to 1.0 at the first tested bar) — for the
    # HTML view layer (render.py backtest). No lookahead; pure replay of rets.
    strat_equity, eq = [], 1.0
    for r in strat_ret:
        eq *= 1 + r
        strat_equity.append(round(eq, 5))
    bh_curve = [round(close[warmup + k] / close[warmup], 5) for k in range(bars)]
    return {
        "bars_tested": bars,
        "period": {"start": d(warmup), "end": d(n - 1)},
        "config": {"warmup": warmup, "lag": lag, "cost_bps": cost_bps,
                   "atr_stop": use_stop, "stop_mult": stop_mult},
        "metrics": _metrics(strat_ret, closed, exposure_bars, bars),
        "buy_hold_return_pct": round(bh_ret * 100, 2),
        "equity_curve": strat_equity,
        "buyhold_equity": bh_curve,
        "trades": closed,
    }


def _close_trade(trades: list[dict], entry_px: float, exit_px: float,
                 entry_i: int, exit_i: int, d, reason: str) -> dict:
    # Replace the matching open marker with the completed round trip
    for t in reversed(trades):
        if t.get("open"):
            trades.remove(t)
            break
    ret = exit_px / entry_px - 1.0
    return {"entry_date": d(entry_i), "exit_date": d(exit_i),
            "entry_px": round(entry_px, 4), "exit_px": round(exit_px, 4),
            "return_pct": round(ret * 100, 2),
            "bars_held": exit_i - entry_i, "exit_reason": reason}


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------

def _metrics(rets: list[float], trades: list[dict],
             exposure_bars: int, bars: int) -> dict:
    if not rets:
        return {}
    equity, eq = [], 1.0
    for r in rets:
        eq *= 1 + r
        equity.append(eq)
    total = equity[-1] - 1.0
    yrs = bars / TRADING_DAYS
    cagr = equity[-1] ** (1 / yrs) - 1 if yrs > 0 and equity[-1] > 0 else None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    sd = math.sqrt(var)
    sharpe = mean / sd * math.sqrt(TRADING_DAYS) if sd > 0 else None
    downs = [r for r in rets if r < 0]
    dsd = math.sqrt(sum(r * r for r in downs) / len(rets)) if downs else 0.0
    sortino = mean / dsd * math.sqrt(TRADING_DAYS) if dsd > 0 else None
    peak, maxdd = equity[0], 0.0
    for e in equity:
        peak = max(peak, e)
        maxdd = max(maxdd, 1 - e / peak)
    wins = [t for t in trades if t["return_pct"] > 0]
    losses = [t for t in trades if t["return_pct"] <= 0]
    gross_w = sum(t["return_pct"] for t in wins)
    gross_l = -sum(t["return_pct"] for t in losses)
    return {
        "total_return_pct": round(total * 100, 2),
        "cagr_pct": round(cagr * 100, 2) if cagr is not None else None,
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "sortino": round(sortino, 2) if sortino is not None else None,
        "max_drawdown_pct": round(maxdd * 100, 2),
        "n_trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else None,
        "profit_factor": round(gross_w / gross_l, 2) if gross_l > 0 else None,
        "avg_trade_pct": round(sum(t["return_pct"] for t in trades) / len(trades), 2)
                         if trades else None,
        "avg_bars_held": round(sum(t["bars_held"] for t in trades) / len(trades), 1)
                         if trades else None,
        "exposure_pct": round(exposure_bars / bars * 100, 1) if bars else None,
    }


# --------------------------------------------------------------------------
# Walk-forward segments & sensitivity grid
# --------------------------------------------------------------------------

def walk_forward(close, high, low, dates, splits: int, **kw) -> list[dict]:
    """
    Contiguous out-of-sample segments. Each segment gets `warmup` bars of
    preceding history for indicator warmup, but PnL only accrues inside the
    segment — stability across segments is the anti-curve-fit evidence.
    """
    n = len(close)
    warmup = kw.get("warmup", 220)
    testable = n - warmup
    seg = testable // splits
    out = []
    for k in range(splits):
        end = warmup + (k + 1) * seg if k < splits - 1 else n
        start = warmup + k * seg
        sl = slice(start - warmup, end)   # segment + its warmup prefix
        r = run(close[sl],
                high[sl] if high else None,
                low[sl] if low else None,
                dates[sl] if dates else None, **kw)
        out.append({"segment": k + 1, "period": r["period"],
                    "metrics": r["metrics"],
                    "buy_hold_return_pct": r["buy_hold_return_pct"]})
    return out


def sensitivity(close, high, low, dates, **kw) -> list[dict]:
    """Lag x cost grid. Fragile edge dies here before it dies in production."""
    base = dict(kw)
    grid = []
    for lag in (1, 2):
        for cost in (0.0, 5.0, 10.0):
            base.update(lag=lag, cost_bps=cost)
            r = run(close, high, low, dates, **base)
            m = r["metrics"]
            grid.append({"lag": lag, "cost_bps": cost,
                         "total_return_pct": m["total_return_pct"],
                         "sharpe": m["sharpe"], "n_trades": m["n_trades"],
                         "max_drawdown_pct": m["max_drawdown_pct"]})
    return grid


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def render(res: dict, wf: Optional[list] = None,
           sens: Optional[list] = None) -> str:
    m = res["metrics"]
    L = []
    L.append(f"BACKTEST  ·  {res['period']['start']} → {res['period']['end']}"
             f"  ({res['bars_tested']} bars)")
    L.append("=" * 60)
    c = res["config"]
    L.append(f"config: lag={c['lag']} bar(s), cost={c['cost_bps']}bps/side, "
             f"ATR stop={'on' if c['atr_stop'] else 'off'} "
             f"({c['stop_mult']}×)")
    L.append("-" * 60)
    L.append(f"  Total return : {m['total_return_pct']:+.2f}%   "
             f"(buy&hold {res['buy_hold_return_pct']:+.2f}%)")
    L.append(f"  CAGR         : {m['cagr_pct']:+.2f}%" if m["cagr_pct"] is not None else "  CAGR         : n/a")
    L.append(f"  Sharpe       : {m['sharpe']}    Sortino: {m['sortino']}")
    L.append(f"  Max drawdown : {m['max_drawdown_pct']:.2f}%")
    L.append(f"  Trades       : {m['n_trades']}  ·  win rate "
             f"{m['win_rate_pct']}%  ·  PF {m['profit_factor']}  ·  "
             f"avg {m['avg_trade_pct']}% / {m['avg_bars_held']} bars")
    L.append(f"  Exposure     : {m['exposure_pct']}% of bars")
    if wf:
        L.append("-" * 60)
        L.append("WALK-FORWARD SEGMENTS (stability = anti-curve-fit):")
        for s in wf:
            mm = s["metrics"]
            L.append(f"  #{s['segment']}  {s['period']['start']}→{s['period']['end']}"
                     f"  ret {mm['total_return_pct']:+.1f}% "
                     f"(B&H {s['buy_hold_return_pct']:+.1f}%)  "
                     f"Sharpe {mm['sharpe']}  DD {mm['max_drawdown_pct']}%  "
                     f"trades {mm['n_trades']}")
    if sens:
        L.append("-" * 60)
        L.append("SENSITIVITY (lag × cost — edge must survive):")
        for g in sens:
            L.append(f"  lag={g['lag']} cost={g['cost_bps']:>4.1f}bps  "
                     f"ret {g['total_return_pct']:+7.2f}%  "
                     f"Sharpe {g['sharpe']}  DD {g['max_drawdown_pct']}%  "
                     f"trades {g['n_trades']}")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="No-lookahead replay of score.py rules.")
    ap.add_argument("input", nargs="?",
                    help="JSON: {symbol, close:[...], high?, low?, dates?}. No file: self-test.")
    ap.add_argument("--warmup", type=int, default=220)
    ap.add_argument("--lag", type=int, default=1)
    ap.add_argument("--cost-bps", type=float, default=5.0)
    ap.add_argument("--no-stop", action="store_true", help="disable ATR trailing stop")
    ap.add_argument("--stop-mult", type=float, default=3.0)
    ap.add_argument("--splits", type=int, default=0, help="walk-forward segment count")
    ap.add_argument("--sensitivity", action="store_true", help="run lag×cost grid")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    high = low = dates = None
    if args.input:
        with open(args.input) as f:
            raw = json.load(f)
        close = [float(x) for x in raw["close"]]
        high = [float(x) for x in raw["high"]] if raw.get("high") else None
        low = [float(x) for x in raw["low"]] if raw.get("low") else None
        dates = raw.get("dates")
    else:
        # Synthetic multi-cycle series: trend, crash, recovery, chop.
        close, v, seed = [], 100.0, 777
        for i in range(700):
            seed = (seed * 1103515245 + 12345) % (2 ** 31)
            u = seed / (2 ** 31) - 0.5
            drift = (0.0012 if i < 250 else
                     -0.004 if i < 330 else
                     0.0015 if i < 550 else 0.0)
            v *= 1 + drift + 0.015 * u
            close.append(round(v, 2))
        print("[self-test: synthetic 700-bar multi-cycle series]\n", file=sys.stderr)

    if dates is None:
        dates = [f"bar {i}" for i in range(len(close))]  # absolute labels for segments
    kw = dict(warmup=args.warmup, lag=args.lag, cost_bps=args.cost_bps,
              use_stop=not args.no_stop, stop_mult=args.stop_mult)
    res = run(close, high, low, dates, **kw)
    wf = walk_forward(close, high, low, dates, args.splits, **kw) if args.splits >= 2 else None
    sens = sensitivity(close, high, low, dates,
                       warmup=args.warmup, use_stop=not args.no_stop,
                       stop_mult=args.stop_mult) if args.sensitivity else None

    if args.json:
        print(json.dumps({"backtest": res, "walk_forward": wf,
                          "sensitivity": sens}, indent=2, ensure_ascii=False))
    else:
        print(render(res, wf, sens))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
