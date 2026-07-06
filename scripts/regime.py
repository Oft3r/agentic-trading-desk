#!/usr/bin/env python3
"""
regime.py
=========
Statistical market-regime detector: 2-state Gaussian Mixture fitted by EM on
daily log returns, smoothed with a sticky 2-state HMM (forward-backward with
a fixed high-persistence transition prior).

DETERMINISTIC by construction:
  - EM initialized from return quantiles (no random init, no restarts)
  - fixed iteration cap and convergence tolerance
  - fixed transition prior (p_stay = 0.97)
Same input -> same output, always. stdlib only, O(n) per EM pass — fast
enough to run every session on ~250-500 bars.

States are labelled by fitted variance:
  calm    = low-variance component  (typically drifting up)
  turbulent = high-variance component (typically crash/chop)

Output feeds macro_pillar.py: `regime_state`, `p_turbulent` (smoothed
posterior of the last bar), and a suggested cap for the macro pillar.
"""
from __future__ import annotations
import json
import math
import sys
from typing import Optional

SQRT_2PI = math.sqrt(2 * math.pi)


def log_returns(close: list[float]) -> list[float]:
    return [math.log(close[i] / close[i - 1])
            for i in range(1, len(close)) if close[i - 1] > 0]


def _norm_pdf(x: float, mu: float, var: float) -> float:
    if var <= 0:
        return 0.0
    return math.exp(-((x - mu) ** 2) / (2 * var)) / (math.sqrt(var) * SQRT_2PI)


# --------------------------------------------------------------------------
# 2-component Gaussian Mixture via EM (deterministic quantile init)
# --------------------------------------------------------------------------

def fit_gmm2(r: list[float], max_iter: int = 200, tol: float = 1e-10) -> dict:
    """
    Returns {weights, means, vars, loglik, iters}. Component 0 = low variance
    (calm), component 1 = high variance (turbulent), enforced after fit.
    """
    n = len(r)
    s = sorted(r)
    # Quantile init: inner 60% -> calm component, outer tails -> turbulent
    lo, hi = int(n * 0.2), int(n * 0.8)
    inner, outer = s[lo:hi], s[:lo] + s[hi:]

    def mv(xs: list[float]) -> tuple[float, float]:
        m = sum(xs) / len(xs)
        v = sum((x - m) ** 2 for x in xs) / len(xs)
        return m, max(v, 1e-12)

    mu = [0.0, 0.0]
    var = [0.0, 0.0]
    mu[0], var[0] = mv(inner)
    mu[1], var[1] = mv(outer)
    w = [0.6, 0.4]

    prev_ll = -float("inf")
    resp = [[0.0, 0.0] for _ in range(n)]
    iters = 0
    for iters in range(1, max_iter + 1):
        # E-step
        ll = 0.0
        for i, x in enumerate(r):
            p0 = w[0] * _norm_pdf(x, mu[0], var[0])
            p1 = w[1] * _norm_pdf(x, mu[1], var[1])
            tot = p0 + p1
            if tot <= 0:
                tot, p0, p1 = 1.0, 0.5, 0.5
            resp[i][0], resp[i][1] = p0 / tot, p1 / tot
            ll += math.log(tot) if tot > 0 else 0.0
        # M-step
        for k in (0, 1):
            nk = sum(resp[i][k] for i in range(n))
            if nk < 1e-9:
                continue
            w[k] = nk / n
            mu[k] = sum(resp[i][k] * r[i] for i in range(n)) / nk
            var[k] = max(
                sum(resp[i][k] * (r[i] - mu[k]) ** 2 for i in range(n)) / nk,
                1e-12)
        if abs(ll - prev_ll) < tol:
            break
        prev_ll = ll

    # Enforce ordering: component 0 = calm (lower variance)
    if var[0] > var[1]:
        w.reverse(); mu.reverse(); var.reverse()
    return {"weights": w, "means": mu, "vars": var,
            "loglik": prev_ll, "iters": iters}


# --------------------------------------------------------------------------
# Sticky HMM smoothing (forward-backward, fixed transition prior)
# --------------------------------------------------------------------------

def hmm_smooth(r: list[float], gmm: dict, p_stay: float = 0.97) -> list[float]:
    """
    Smoothed posterior P(turbulent | all data) per bar. GMM components act as
    emission densities; the sticky transition prior kills single-bar flicker
    so the regime label only flips on persistent evidence.
    """
    n = len(r)
    mu, var, w = gmm["means"], gmm["vars"], gmm["weights"]
    A = [[p_stay, 1 - p_stay], [1 - p_stay, p_stay]]
    # Emissions
    b = [[_norm_pdf(x, mu[0], var[0]), _norm_pdf(x, mu[1], var[1])] for x in r]
    # Forward (scaled)
    alpha = [[0.0, 0.0] for _ in range(n)]
    scale = [0.0] * n
    alpha[0] = [w[0] * b[0][0], w[1] * b[0][1]]
    scale[0] = sum(alpha[0]) or 1.0
    alpha[0] = [a / scale[0] for a in alpha[0]]
    for t in range(1, n):
        for j in (0, 1):
            alpha[t][j] = (alpha[t - 1][0] * A[0][j] +
                           alpha[t - 1][1] * A[1][j]) * b[t][j]
        scale[t] = sum(alpha[t]) or 1.0
        alpha[t] = [a / scale[t] for a in alpha[t]]
    # Backward (scaled)
    beta = [[1.0, 1.0] for _ in range(n)]
    for t in range(n - 2, -1, -1):
        for i in (0, 1):
            beta[t][i] = sum(A[i][j] * b[t + 1][j] * beta[t + 1][j]
                             for j in (0, 1)) / scale[t + 1]
    # Smoothed posterior of state 1 (turbulent)
    post = []
    for t in range(n):
        g0 = alpha[t][0] * beta[t][0]
        g1 = alpha[t][1] * beta[t][1]
        tot = g0 + g1
        post.append(g1 / tot if tot > 0 else 0.5)
    return post


# --------------------------------------------------------------------------
# High-level API
# --------------------------------------------------------------------------

def classify(close: list[float], min_bars: int = 120,
             p_stay: float = 0.97, threshold: float = 0.6,
             max_bars: int = 750) -> Optional[dict]:
    """
    Full pipeline: returns -> GMM fit -> HMM smoothing -> label.
    `threshold`: smoothed P(turbulent) above which the state flips.
    `max_bars`: cap the return window (default ~3y daily). This is a read of
    the *current* regime, so only the recent window matters; the cap keeps EM
    cost bounded (fit_gmm2 is O(n*iters) and dominates runtime) without
    changing output on normal inputs (≤750 returns pass through untouched).
    Set max_bars=0 to disable and fit the full series.
    """
    r = log_returns(close)
    if len(r) < min_bars:
        return None
    if max_bars and len(r) > max_bars:
        r = r[-max_bars:]
    gmm = fit_gmm2(r)
    post = hmm_smooth(r, gmm, p_stay)
    p_turb = post[-1]
    state = "turbulent" if p_turb >= threshold else "calm"
    # Persistence: bars since the smoothed posterior last crossed threshold
    bars_in_state = 0
    for p in reversed(post):
        if (p >= threshold) == (state == "turbulent"):
            bars_in_state += 1
        else:
            break
    ann = math.sqrt(252)
    return {
        "state": state,
        "p_turbulent": p_turb,
        "bars_in_state": bars_in_state,
        "calm_vol_annual": math.sqrt(gmm["vars"][0]) * ann,
        "turbulent_vol_annual": math.sqrt(gmm["vars"][1]) * ann,
        "calm_mean_annual": gmm["means"][0] * 252,
        "turbulent_mean_annual": gmm["means"][1] * 252,
        "gmm_weights": gmm["weights"],
        "em_iters": gmm["iters"],
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
    ap = argparse.ArgumentParser(description="GMM+HMM statistical regime detector.")
    ap.add_argument("input", nargs="?",
                    help="JSON: {'close':[...]} (e.g. SPY closes). No file: self-test.")
    ap.add_argument("--p-stay", type=float, default=0.97)
    ap.add_argument("--threshold", type=float, default=0.6)
    args = ap.parse_args()

    if args.input:
        with open(args.input) as f:
            raw = json.load(f)
        close = [float(x) for x in (raw["close"] if isinstance(raw, dict) else raw)]
    else:
        # Synthetic: 200 calm bars (drift up, low vol) then 60 turbulent bars
        # (drift down, 4x vol) using a fixed congruential sequence (no RNG).
        close, v, seed = [], 400.0, 12345
        for i in range(260):
            seed = (seed * 1103515245 + 12345) % (2 ** 31)
            u = seed / (2 ** 31) - 0.5
            if i < 200:
                v *= 1 + 0.0006 + 0.008 * u
            else:
                v *= 1 - 0.0015 + 0.032 * u
            close.append(round(v, 2))
        print("[self-test: 200 calm bars + 60 turbulent bars]\n", file=sys.stderr)

    res = classify(close, p_stay=args.p_stay, threshold=args.threshold)
    if res is None:
        print("insufficient data (need >=121 bars)", file=sys.stderr)
        return 1
    print(json.dumps(_round(res), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
