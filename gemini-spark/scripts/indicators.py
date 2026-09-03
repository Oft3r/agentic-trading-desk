#!/usr/bin/env python3
"""
indicators.py
=============
DETERMINISTIC indicator engine for the trading desk's exact stack:
  EMA 20/50/200 · RSI-14 (Wilder) · MACD 12/26/9 · TRIX-15 (signal 9) · Bollinger 20/2

Purpose: Claude should NEVER calculate these values by "reasoning" over bars.
The correct flow is: Claude fetches raw bars via Robinhood MCP
(get_equity_historicals, ~290 daily bars) -> passes them to this module ->
numbers are computed, not estimated.

stdlib only. Python 3.9+. Input: list of close prices old->new.
For Bollinger %B precision, high/low can be passed, but close is enough.
"""
from __future__ import annotations
import json
import sys
from statistics import pstdev
from typing import Optional


# --------------------------------------------------------------------------
# Primitives
# --------------------------------------------------------------------------

def _sma(values: list[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema_series(values: list[float], period: int) -> list[Optional[float]]:
    """
    EMA with None padding in the warmup. Seed = SMA of the first `period`
    observations (TradingView / ta-lib adjust=False convention).
    Returns list of same length as `values`.
    """
    n = len(values)
    out: list[Optional[float]] = [None] * n
    if n < period:
        return out
    k = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def _strip(values: list[Optional[float]]) -> list[float]:
    return [v for v in values if v is not None]


def rsi_wilder(close: list[float], period: int = 14) -> list[Optional[float]]:
    """RSI with Wilder smoothing. None padding in warmup."""
    n = len(close)
    out: list[Optional[float]] = [None] * n
    if n < period + 1:
        return out
    gains, losses = [], []
    for i in range(1, n):
        ch = close[i] - close[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    # First simple average over the first `period` changes
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def rsi_val(ag: float, al: float) -> float:
        if al == 0:
            return 100.0
        rs = ag / al
        return 100.0 - 100.0 / (1.0 + rs)

    out[period] = rsi_val(avg_gain, avg_loss)
    for i in range(period + 1, n):
        g, l = gains[i - 1], losses[i - 1]
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        out[i] = rsi_val(avg_gain, avg_loss)
    return out


def macd(close: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    """Returns (macd_line, signal_line, histogram), all None-padded."""
    ef = ema_series(close, fast)
    es = ema_series(close, slow)
    line: list[Optional[float]] = [
        (a - b) if (a is not None and b is not None) else None for a, b in zip(ef, es)
    ]
    valid = _strip(line)
    sig_valid = ema_series(valid, signal)
    # re-align signal to original length
    sig: list[Optional[float]] = [None] * len(close)
    first = next((i for i, v in enumerate(line) if v is not None), None)
    if first is not None:
        for off, v in enumerate(sig_valid):
            sig[first + off] = v
    hist: list[Optional[float]] = [
        (m - s) if (m is not None and s is not None) else None for m, s in zip(line, sig)
    ]
    return line, sig, hist


def trix(close: list[float], period: int = 15, signal: int = 9):
    """TRIX (% ROC of triple EMA) and its signal. None-padded to original length."""
    n = len(close)
    e1 = _strip(ema_series(close, period))
    e2 = _strip(ema_series(e1, period))
    e3 = _strip(ema_series(e2, period))
    trix_valid: list[float] = []
    for i in range(1, len(e3)):
        prev = e3[i - 1]
        trix_valid.append((e3[i] - prev) / prev * 100.0 if prev != 0 else 0.0)
    sig_valid = _strip(ema_series(trix_valid, signal))
    # align to end (TRIX is one of the most lagging)
    t: list[Optional[float]] = [None] * n
    for off, v in enumerate(trix_valid):
        idx = n - len(trix_valid) + off
        if idx >= 0:
            t[idx] = v
    s: list[Optional[float]] = [None] * n
    for off, v in enumerate(sig_valid):
        idx = n - len(sig_valid) + off
        if idx >= 0:
            s[idx] = v
    return t, s


def bollinger(close: list[float], period: int = 20, mult: float = 2.0):
    """Returns (mid, upper, lower, percent_b) for the last bar."""
    if len(close) < period:
        return None, None, None, None
    window = close[-period:]
    mid = sum(window) / period
    sd = pstdev(window)  # population, like TradingView
    upper = mid + mult * sd
    lower = mid - mult * sd
    rng = upper - lower
    pct_b = (close[-1] - lower) / rng if rng != 0 else 0.5
    return mid, upper, lower, pct_b


# --------------------------------------------------------------------------
# High-level API
# --------------------------------------------------------------------------

def _slope(series: list[Optional[float]], lookback: int) -> Optional[float]:
    """Absolute variation of the indicator relative to `lookback` bars ago."""
    valid_idx = [i for i, v in enumerate(series) if v is not None]
    if len(valid_idx) <= lookback:
        return None
    last_i = valid_idx[-1]
    prev_i = valid_idx[-1 - lookback]
    return series[last_i] - series[prev_i]


def compute(close: list[float], slope_lookback: int = 5) -> dict:
    """
    Computes the entire stack and returns the latest values + recent slopes.
    `slope_lookback`: bars to measure the slope (default 5 ~ one week).
    """
    if len(close) < 210:
        # Not a fatal error: EMA200 will simply be None. We warn about it.
        warn = f"Only {len(close)} bars; EMA200/some indicators may be None. Ideal >=220."
    else:
        warn = None

    ema20 = ema_series(close, 20)
    ema50 = ema_series(close, 50)
    ema200 = ema_series(close, 200)
    rsi = rsi_wilder(close, 14)
    macd_line, macd_sig, macd_hist = macd(close, 12, 26, 9)
    trix_line, trix_sig = trix(close, 15, 9)
    bb_mid, bb_up, bb_lo, pct_b = bollinger(close, 20, 2.0)

    def last(s):
        v = _strip(s)
        return v[-1] if v else None

    def prev(s):
        v = _strip(s)
        return v[-2] if len(v) >= 2 else None

    # Bars since the last close BELOW the EMA20 (0 = current bar closed below).
    # None if it never closed below in the available window. Helps distinguish
    # a genuine recovery of EMA20 (recent dip) from the normal state of an uptrend.
    bars_since_below_ema20 = None
    for back in range(len(close)):
        i = len(close) - 1 - back
        if ema20[i] is not None and close[i] < ema20[i]:
            bars_since_below_ema20 = back
            break

    return {
        "n_bars": len(close),
        "warning": warn,
        "close": close[-1],
        "ema20": last(ema20), "ema50": last(ema50), "ema200": last(ema200),
        "ema20_slope": _slope(ema20, slope_lookback),
        "ema50_slope": _slope(ema50, slope_lookback),
        "ema200_slope": _slope(ema200, slope_lookback),
        "rsi14": last(rsi), "rsi14_prev": prev(rsi),
        "macd_line": last(macd_line), "macd_signal": last(macd_sig),
        "macd_hist": last(macd_hist), "macd_hist_prev": prev(macd_hist),
        "trix": last(trix_line), "trix_prev": prev(trix_line),
        "trix_signal": last(trix_sig), "trix_signal_prev": prev(trix_sig),
        "bars_since_below_ema20": bars_since_below_ema20,
        "bb_mid": bb_mid, "bb_upper": bb_up, "bb_lower": bb_lo, "percent_b": pct_b,
    }


def _round(d: dict, nd: int = 4) -> dict:
    return {k: (round(v, nd) if isinstance(v, float) else v) for k, v in d.items()}


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Deterministic indicator stack (input: JSON of closes).")
    ap.add_argument("input", nargs="?", help="JSON: {'close':[...]} or [..]. If no file: self-test.")
    ap.add_argument("--slope-lookback", type=int, default=5)
    args = ap.parse_args()

    if args.input:
        with open(args.input) as f:
            raw = json.load(f)
        close = raw["close"] if isinstance(raw, dict) else raw
        close = [float(x) for x in close]
    else:
        import math
        close = [round(100 + 18 * math.sin(i / 22) + i * 0.06, 2) for i in range(290)]
        print("[self-test: synthetic series of 290 bars]\n", file=sys.stderr)

    print(json.dumps(_round(compute(close, args.slope_lookback)), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
