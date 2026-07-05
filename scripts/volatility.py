#!/usr/bin/env python3
"""
volatility.py
=============
Volatility & mean-reversion engine for the trading desk.

Adds the volatility-adjusted layer the plain indicator stack lacks:
  ATR-14 (Wilder)        - true range with close-only fallback
  Chandelier stop        - ATR-based dynamic trailing stop (long side)
  EWMA volatility        - RiskMetrics lambda=0.94, annualized
  GARCH(1,1)             - deterministic MLE fit (Nelder-Mead, fixed start),
                           next-day conditional vol forecast
  AR(1) half-life        - Ornstein-Uhlenbeck mean-reversion speed on log price
  Z-score vs SMA         - statistical stretch replacing raw % stretch
  Vol-target sizing      - position fraction so the position contributes a
                           target daily vol to the book

All deterministic: same input -> same output. No RNG, no network.
stdlib only. Python 3.9+. Input closes old->new; high/low optional.
"""
from __future__ import annotations
import json
import math
import sys
from statistics import pstdev
from typing import Optional

TRADING_DAYS = 252


# --------------------------------------------------------------------------
# True Range / ATR
# --------------------------------------------------------------------------

def true_range(close: list[float], high: Optional[list[float]] = None,
               low: Optional[list[float]] = None) -> list[float]:
    """
    TR series (len = n-1). With high/low: classic Wilder TR.
    Close-only fallback: |close_t - close_{t-1}| (understates gaps intraday
    range but keeps the stop logic usable when MCP returns closes only).
    """
    n = len(close)
    out: list[float] = []
    has_hl = bool(high) and bool(low) and len(high) == n and len(low) == n
    for i in range(1, n):
        if has_hl:
            out.append(max(high[i] - low[i],
                           abs(high[i] - close[i - 1]),
                           abs(low[i] - close[i - 1])))
        else:
            out.append(abs(close[i] - close[i - 1]))
    return out


def atr_wilder(close: list[float], period: int = 14,
               high: Optional[list[float]] = None,
               low: Optional[list[float]] = None) -> Optional[float]:
    """Wilder-smoothed ATR of the last bar. None if insufficient data."""
    tr = true_range(close, high, low)
    if len(tr) < period:
        return None
    atr = sum(tr[:period]) / period
    for x in tr[period:]:
        atr = (atr * (period - 1) + x) / period
    return atr


def chandelier_stop(close: list[float], atr: Optional[float],
                    lookback: int = 22, mult: float = 3.0,
                    high: Optional[list[float]] = None) -> Optional[float]:
    """
    Long-side trailing stop: highest high (or close) of `lookback` bars
    minus mult*ATR. Ratchets up as the cycle rides; never widens.
    """
    if atr is None or len(close) < lookback:
        return None
    ref = high if high and len(high) == len(close) else close
    return max(ref[-lookback:]) - mult * atr


# --------------------------------------------------------------------------
# Return-based volatility
# --------------------------------------------------------------------------

def log_returns(close: list[float]) -> list[float]:
    return [math.log(close[i] / close[i - 1])
            for i in range(1, len(close)) if close[i - 1] > 0]


def ewma_vol(close: list[float], lam: float = 0.94,
             annualize: bool = True) -> Optional[float]:
    """RiskMetrics EWMA volatility of the last bar."""
    r = log_returns(close)
    if len(r) < 30:
        return None
    var = r[0] * r[0]
    for x in r[1:]:
        var = lam * var + (1 - lam) * x * x
    v = math.sqrt(var)
    return v * math.sqrt(TRADING_DAYS) if annualize else v


def _garch_nll(params: tuple[float, float, float], r2: list[float],
               var0: float) -> float:
    """Negative log-likelihood of GARCH(1,1) with Gaussian innovations."""
    omega, alpha, beta = params
    if omega <= 0 or alpha < 0 or beta < 0 or alpha + beta >= 0.9999:
        return float("inf")
    var = var0
    nll = 0.0
    for x2 in r2:
        nll += math.log(var) + x2 / var
        var = omega + alpha * x2 + beta * var
        if var <= 0:
            return float("inf")
    return nll


def _nelder_mead(f, x0: list[float], steps: list[float],
                 max_iter: int = 400, tol: float = 1e-10) -> list[float]:
    """Deterministic Nelder-Mead (fixed initial simplex, no restarts)."""
    n = len(x0)
    simplex = [list(x0)]
    for i in range(n):
        p = list(x0)
        p[i] += steps[i]
        simplex.append(p)
    vals = [f(tuple(p)) for p in simplex]
    for _ in range(max_iter):
        order = sorted(range(n + 1), key=lambda i: vals[i])
        simplex = [simplex[i] for i in order]
        vals = [vals[i] for i in order]
        if abs(vals[-1] - vals[0]) < tol:
            break
        centroid = [sum(simplex[i][j] for i in range(n)) / n for j in range(n)]
        refl = [centroid[j] + (centroid[j] - simplex[-1][j]) for j in range(n)]
        fr = f(tuple(refl))
        if vals[0] <= fr < vals[-2]:
            simplex[-1], vals[-1] = refl, fr
        elif fr < vals[0]:
            exp = [centroid[j] + 2 * (centroid[j] - simplex[-1][j]) for j in range(n)]
            fe = f(tuple(exp))
            if fe < fr:
                simplex[-1], vals[-1] = exp, fe
            else:
                simplex[-1], vals[-1] = refl, fr
        else:
            con = [centroid[j] + 0.5 * (simplex[-1][j] - centroid[j]) for j in range(n)]
            fc = f(tuple(con))
            if fc < vals[-1]:
                simplex[-1], vals[-1] = con, fc
            else:  # shrink toward best
                for i in range(1, n + 1):
                    simplex[i] = [simplex[0][j] + 0.5 * (simplex[i][j] - simplex[0][j])
                                  for j in range(n)]
                    vals[i] = f(tuple(simplex[i]))
    best = min(range(n + 1), key=lambda i: vals[i])
    return simplex[best]


def garch11(close: list[float], min_bars: int = 120) -> Optional[dict]:
    """
    Fits GARCH(1,1) by MLE with a deterministic Nelder-Mead start
    (variance targeting: omega = uncond_var * (1 - alpha - beta)).
    Returns params, persistence, and annualized next-day vol forecast.
    """
    r = log_returns(close)
    if len(r) < min_bars:
        return None
    mean = sum(r) / len(r)
    r = [x - mean for x in r]
    r2 = [x * x for x in r]
    uncond = sum(r2) / len(r2)
    if uncond <= 0:
        return None
    # Fixed textbook start: alpha=0.08, beta=0.90, omega via variance targeting
    a0, b0 = 0.08, 0.90
    x0 = [uncond * (1 - a0 - b0), a0, b0]
    steps = [uncond * 0.01, 0.02, 0.02]
    omega, alpha, beta = _nelder_mead(
        lambda p: _garch_nll(p, r2, uncond), x0, steps)
    persistence = alpha + beta
    # One-step-ahead forecast from filtered variance
    var = uncond
    for x2 in r2:
        var = omega + alpha * x2 + beta * var
    fvol = math.sqrt(var)
    lt_var = omega / (1 - persistence) if persistence < 1 else uncond
    return {
        "omega": omega, "alpha": alpha, "beta": beta,
        "persistence": persistence,
        "forecast_vol_daily": fvol,
        "forecast_vol_annual": fvol * math.sqrt(TRADING_DAYS),
        "long_run_vol_annual": math.sqrt(lt_var) * math.sqrt(TRADING_DAYS),
        "vol_ratio": fvol / math.sqrt(lt_var) if lt_var > 0 else None,
    }


# --------------------------------------------------------------------------
# Mean reversion statistics
# --------------------------------------------------------------------------

def ar1_halflife(close: list[float], window: int = 120) -> Optional[dict]:
    """
    OU half-life via AR(1) OLS on log price: dy_t = a + b*y_{t-1} + e.
    half_life = -ln(2)/ln(1+b). b>=0 -> trending (no reversion), None half-life.
    """
    if len(close) < window + 1:
        return None
    y = [math.log(c) for c in close[-(window + 1):]]
    x = y[:-1]
    dy = [y[i + 1] - y[i] for i in range(len(y) - 1)]
    n = len(x)
    mx, md = sum(x) / n, sum(dy) / n
    sxx = sum((xi - mx) ** 2 for xi in x)
    if sxx == 0:
        return None
    b = sum((xi - mx) * (di - md) for xi, di in zip(x, dy)) / sxx
    hl = -math.log(2) / math.log(1 + b) if -1 < b < 0 else None
    return {"ar1_beta": b, "half_life_bars": hl,
            "mean_reverting": hl is not None and hl <= window / 2}


def zscore(close: list[float], period: int = 20) -> Optional[float]:
    """Z-score of last close vs its SMA — statistical stretch measure."""
    if len(close) < period:
        return None
    window = close[-period:]
    mid = sum(window) / period
    sd = pstdev(window)
    if sd == 0:
        return None
    return (close[-1] - mid) / sd


# --------------------------------------------------------------------------
# Vol-target position sizing
# --------------------------------------------------------------------------

def vol_target_fraction(asset_vol_annual: Optional[float],
                        target_vol_annual: float = 0.15,
                        cap: float = 1.0) -> Optional[float]:
    """
    Fraction of allocated capital so the position contributes
    `target_vol_annual` of annualized vol. Capped at `cap` (no leverage).
    """
    if asset_vol_annual is None or asset_vol_annual <= 0:
        return None
    return min(cap, target_vol_annual / asset_vol_annual)


# --------------------------------------------------------------------------
# High-level API
# --------------------------------------------------------------------------

def compute(close: list[float], high: Optional[list[float]] = None,
            low: Optional[list[float]] = None,
            atr_period: int = 14, stop_mult: float = 3.0,
            target_vol: float = 0.15, with_garch: bool = True) -> dict:
    """`with_garch=False` skips the MLE fit — used by the backtester where
    refitting GARCH on every bar would be O(n^2) with a large constant."""
    atr = atr_wilder(close, atr_period, high, low)
    g = garch11(close) if with_garch else None
    ew = ewma_vol(close)
    # Prefer GARCH forecast for sizing; EWMA as fallback
    size_vol = g["forecast_vol_annual"] if g else ew
    z = zscore(close, 20)
    c = close[-1]
    return {
        "atr": atr,
        "atr_pct": (atr / c * 100.0) if atr and c else None,
        "chandelier_stop": chandelier_stop(close, atr, 22, stop_mult, high),
        "ewma_vol_annual": ew,
        "garch": g,
        "zscore_20": z,
        "ar1": ar1_halflife(close),
        "vol_target_fraction": vol_target_fraction(size_vol, target_vol),
        "close": c,
    }


def _round(obj, nd: int = 4):
    if isinstance(obj, float):
        return round(obj, nd)
    if isinstance(obj, dict):
        return {k: _round(v, nd) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round(v, nd) for v in obj]
    return obj


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Volatility & mean-reversion engine.")
    ap.add_argument("input", nargs="?",
                    help="JSON: {'close':[...], 'high':[...]?, 'low':[...]?}. No file: self-test.")
    ap.add_argument("--atr-period", type=int, default=14)
    ap.add_argument("--stop-mult", type=float, default=3.0)
    ap.add_argument("--target-vol", type=float, default=0.15)
    args = ap.parse_args()

    if args.input:
        with open(args.input) as f:
            raw = json.load(f)
        close = [float(x) for x in (raw["close"] if isinstance(raw, dict) else raw)]
        high = [float(x) for x in raw["high"]] if isinstance(raw, dict) and raw.get("high") else None
        low = [float(x) for x in raw["low"]] if isinstance(raw, dict) and raw.get("low") else None
    else:
        close = [round(100 + 18 * math.sin(i / 22) + i * 0.06, 2) for i in range(290)]
        high = low = None
        print("[self-test: synthetic series of 290 bars]\n", file=sys.stderr)

    print(json.dumps(_round(compute(close, high, low, args.atr_period,
                                    args.stop_mult, args.target_vol)),
                     indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
