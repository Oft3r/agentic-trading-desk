#!/usr/bin/env python3
"""
macro_pillar.py
================

Key skill:
  - No FMP. Consumes daily closes (OHLCV close) that you already fetch
    from Robinhood historicals, in JSON.
  - The 10Y-2Y curve is NOT approximated with treasury ETFs (fragile proxy):
    the spread that you already fetch from Investing.com is injected directly.
    If not provided, its weight (20%) is redistributed proportionally among
    the other components.
  - The final output is NOT just a "regime label": it translates the cross-asset
    composite to your Macro-Sentiment pillar on a -2..+2 scale.

Sign orientation: +1 = risk-on / broadening, -1 = risk-off /
concentration or contraction. All components are aligned to
this convention before weighting.

stdlib only. Python 3.9+.
"""

from __future__ import annotations
import argparse
import json
import sys
from dataclasses import dataclass, field, asdict
from typing import Optional


# --------------------------------------------------------------------------
# Numerical utilities (no numpy, to keep it clean on your Ubuntu)
# --------------------------------------------------------------------------

def sma(series: list[float], window: int) -> Optional[float]:
    """Simple average of the last `window` observations."""
    if len(series) < window:
        return None
    return sum(series[-window:]) / window


def ratio_series(num: list[float], den: list[float]) -> list[float]:
    """Series of ratios element-by-element, aligned by the end."""
    n = min(len(num), len(den))
    num, den = num[-n:], den[-n:]
    return [a / b for a, b in zip(num, den) if b != 0]


def pct_returns(series: list[float]) -> list[float]:
    out = []
    for i in range(1, len(series)):
        if series[i - 1] != 0:
            out.append(series[i] / series[i - 1] - 1.0)
    return out


def pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    n = min(len(xs), len(ys))
    if n < 5:
        return None
    xs, ys = xs[-n:], ys[-n:]
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx == 0 or vy == 0:
        return None
    return cov / (vx ** 0.5 * vy ** 0.5)


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# --------------------------------------------------------------------------
# Component Scorer
# --------------------------------------------------------------------------

@dataclass
class Component:
    name: str
    ratio: str
    weight: float
    signal: Optional[float] = None     # -1 / 0 / +1 (risk-on oriented)
    detail: str = ""
    available: bool = True


def trend_signal(series: list[float], fast: int, slow: int, slope_win: int) -> tuple[Optional[float], str]:
    """
    Directional signal -1/0/+1 based on:
      base  = position of the last value vs its slow SMA
      trend = slope of the slow SMA over `slope_win`
    signal = 0.5*base + 0.5*trend  -> {-1, 0, +1}
    """
    s_slow = sma(series, slow)
    if s_slow is None or len(series) < slow + slope_win:
        return None, "insufficient data"
    base = 1.0 if series[-1] > s_slow else -1.0
    slow_then = sma(series[: -slope_win], slow)
    if slow_then is None:
        return None, "insufficient data for slope"
    trend = 1.0 if s_slow > slow_then else -1.0
    sig = 0.5 * base + 0.5 * trend
    pos = "above" if base > 0 else "below"
    slp = "rising" if trend > 0 else "falling"
    return sig, f"ratio {pos} SMA{slow}, SMA{slow} {slp}"


# --------------------------------------------------------------------------
# Main Scorer Engine
# --------------------------------------------------------------------------

@dataclass
class MacroResult:
    as_of: str
    composite: float
    regime: str
    pillar_score: int
    pillar_label: str
    inflationary_flag: bool
    spy_tlt_corr: Optional[float]
    components: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# Canonical weights from original skill
BASE_WEIGHTS = {
    "concentration": ("RSP/SPY", 0.25),
    "yield_curve":   ("10Y-2Y", 0.20),
    "credit":        ("HYG/LQD", 0.15),
    "size":          ("IWM/SPY", 0.15),
    "equity_bond":   ("SPY/TLT", 0.15),
    "sector":        ("XLY/XLP", 0.10),
}


def score_macro(data: dict, fast: int = 50, slow: int = 200,
                slope_win: int = 20, corr_win: int = 40) -> MacroResult:
    series = data.get("series", {})
    notes: list[str] = []

    def closes(sym: str) -> Optional[list[float]]:
        v = series.get(sym)
        if not v:
            return None
        # Accepts [close,...] or [{"close":x},...]
        if isinstance(v[0], dict):
            return [float(x["close"]) for x in v]
        return [float(x) for x in v]

    comps: dict[str, Component] = {}

    # 1. Concentration: RSP/SPY  (up = broadening = +)
    rsp, spy = closes("RSP"), closes("SPY")
    c = Component("Concentration (equal vs cap-weight)", "RSP/SPY", BASE_WEIGHTS["concentration"][1])
    if rsp and spy:
        c.signal, c.detail = trend_signal(ratio_series(rsp, spy), fast, slow, slope_win)
    if c.signal is None:
        c.available = False
    comps["concentration"] = c

    # 2. Yield Curve 10Y-2Y (injected from Investing.com)  (steepening = +)
    c = Component("Yield Curve 10Y-2Y", "10Y-2Y", BASE_WEIGHTS["yield_curve"][1])
    spread = data.get("yield_spread")
    if spread is not None and not isinstance(spread, list):
        spread = [spread]  # accepts raw scalar from Investing.com
    if spread and len(spread) >= slope_win + 1:
        spread = [float(x) for x in spread]
        now, then = spread[-1], spread[-1 - slope_win]
        base = 1.0 if now > 0 else -1.0          # inverted = risk
        trend = 1.0 if now > then else -1.0       # steepening = +
        c.signal = 0.5 * base + 0.5 * trend
        sign = "+" if now >= 0 else ""
        c.detail = f"spread {sign}{now:.2f}, {'steepening' if trend>0 else 'flattening'}"
    elif spread:
        # Typical session case: only the current value from Investing.com.
        # Medium-intensity level signal (no slope component).
        now = float(spread[-1])
        c.signal = 0.5 if now > 0 else -0.5
        sign = "+" if now >= 0 else ""
        c.detail = f"spread {sign}{now:.2f} (level only; no series for slope)"
        notes.append("yield_spread with <21 observations: using level only (±0.5), no slope.")
    else:
        c.available = False
        notes.append("No yield_spread: redistributing the 20% weight of the curve among other components.")
    comps["yield_curve"] = c

    # 3. Credit: HYG/LQD  (up = risk-on = +)
    hyg, lqd = closes("HYG"), closes("LQD")
    c = Component("Credit (high-yield vs IG)", "HYG/LQD", BASE_WEIGHTS["credit"][1])
    if hyg and lqd:
        c.signal, c.detail = trend_signal(ratio_series(hyg, lqd), fast, slow, slope_win)
    if c.signal is None:
        c.available = False
    comps["credit"] = c

    # 4. Size: IWM/SPY  (up = small-cap leadership = +)
    iwm = closes("IWM")
    c = Component("Size factor (small vs large)", "IWM/SPY", BASE_WEIGHTS["size"][1])
    if iwm and spy:
        c.signal, c.detail = trend_signal(ratio_series(iwm, spy), fast, slow, slope_win)
    if c.signal is None:
        c.available = False
    comps["size"] = c

    # 5. Equity-Bond: SPY/TLT  (up = equities outperforming bonds = +)
    tlt = closes("TLT")
    c = Component("Equity vs Bond (SPY/TLT)", "SPY/TLT", BASE_WEIGHTS["equity_bond"][1])
    if spy and tlt:
        c.signal, c.detail = trend_signal(ratio_series(spy, tlt), fast, slow, slope_win)
    if c.signal is None:
        c.available = False
    comps["equity_bond"] = c

    # 6. Sector Rotation: XLY/XLP  (up = cyclicals outperforming defensives = +)
    xly, xlp = closes("XLY"), closes("XLP")
    c = Component("Sector rotation (cyclical vs defensive)", "XLY/XLP", BASE_WEIGHTS["sector"][1])
    if xly and xlp:
        c.signal, c.detail = trend_signal(ratio_series(xly, xlp), fast, slow, slope_win)
    if c.signal is None:
        c.available = False
    comps["sector"] = c

    # --- SPY-TLT Correlation for inflationary flag ---
    spy_tlt_corr = None
    if spy and tlt:
        rs, rt = pct_returns(spy[-(corr_win + 1):]), pct_returns(tlt[-(corr_win + 1):])
        spy_tlt_corr = pearson(rs, rt)

    # --- Weighted composite (renormalizes for available weights) ---
    avail = [c for c in comps.values() if c.available and c.signal is not None]
    if not avail:
        raise ValueError("No components with sufficient data. Check input series.")
    wsum = sum(c.weight for c in avail)
    composite = sum(c.signal * c.weight for c in avail) / wsum
    composite = clamp(composite, -1.0, 1.0)

    # --- Inflationary flag: positive SPY-TLT correlation + weak equity-bond ---
    eb = comps["equity_bond"]
    inflationary = bool(
        spy_tlt_corr is not None and spy_tlt_corr > 0.25 and
        eb.available and eb.signal is not None and eb.signal <= 0
    )

    # --- Regime classification (cascading by priority) ---
    rsp_sig = comps["concentration"].signal or 0
    iwm_sig = comps["size"].signal or 0
    cr_sig = comps["credit"].signal or 0
    if inflationary:
        regime = "Inflationary"
    elif composite <= -0.5 and cr_sig < 0:
        regime = "Contraction"
    elif composite >= 0.4 and iwm_sig > 0:
        regime = "Broadening"
    elif rsp_sig < 0 and iwm_sig < 0 and composite > -0.5:
        regime = "Concentration"
    else:
        regime = "Transitional"

    # --- Mapping to your Macro-Sentiment pillar (-2..+2) ---
    if composite >= 0.5:
        pillar, plabel = 2, "Strongly favorable macro"
    elif composite >= 0.2:
        pillar, plabel = 1, "Favorable macro"
    elif composite > -0.2:
        pillar, plabel = 0, "Neutral macro"
    elif composite > -0.5:
        pillar, plabel = -1, "Adverse macro"
    else:
        pillar, plabel = -2, "Strongly adverse macro"

    # Risk-off regimes cap the pillar even if the composite is not extreme
    if regime in ("Contraction", "Inflationary") and pillar > -1:
        pillar = -1
        plabel = f"Adverse macro (cap due to {regime} regime)"
        notes.append(f"Pillar capped at -1 due to {regime} regime.")

    return MacroResult(
        as_of=data.get("as_of", ""),
        composite=round(composite, 3),
        regime=regime,
        pillar_score=pillar,
        pillar_label=plabel,
        inflationary_flag=inflationary,
        spy_tlt_corr=round(spy_tlt_corr, 3) if spy_tlt_corr is not None else None,
        components=[
            {**asdict(c), "signal": (round(c.signal, 2) if c.signal is not None else None)}
            for c in comps.values()
        ],
        notes=notes,
    )


# --------------------------------------------------------------------------
# Human-readable report rendering
# --------------------------------------------------------------------------

def render(r: MacroResult) -> str:
    L = []
    L.append(f"MACRO-SENTIMENT  ·  {r.as_of or 'n/a'}")
    L.append("=" * 52)
    L.append(f"Regime         : {r.regime}")
    L.append(f"Composite      : {r.composite:+.3f}  (scale -1..+1)")
    L.append(f"PILLAR (-2..+2): {r.pillar_score:+d}  · {r.pillar_label}")
    if r.spy_tlt_corr is not None:
        L.append(f"SPY-TLT Corr   : {r.spy_tlt_corr:+.3f}"
                 f"{'  ⚠ inflationary flag' if r.inflationary_flag else ''}")
    L.append("-" * 52)
    L.append("Components:")
    for c in r.components:
        if c["available"] and c["signal"] is not None:
            arrow = "▲" if c["signal"] > 0 else ("▼" if c["signal"] < 0 else "─")
            L.append(f"  {arrow} {c['ratio']:<9} w={c['weight']:.2f}  "
                     f"sig={c['signal']:+.2f}  {c['detail']}")
        else:
            L.append(f"  · {c['ratio']:<9} w={c['weight']:.2f}  (no data)")
    if r.notes:
        L.append("-" * 52)
        for n in r.notes:
            L.append(f"  note: {n}")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="Macro-Sentiment pillar scorer (cross-asset, Robinhood-compatible).")
    ap.add_argument("input", nargs="?", help="JSON with close price series. If omitted, runs a synthetic self-test.")
    ap.add_argument("--json", action="store_true", help="Output in JSON format instead of human-readable report.")
    ap.add_argument("--fast", type=int, default=50)
    ap.add_argument("--slow", type=int, default=200)
    ap.add_argument("--slope-win", type=int, default=20)
    ap.add_argument("--corr-win", type=int, default=40)
    args = ap.parse_args()

    if args.input:
        with open(args.input) as f:
            data = json.load(f)
    else:
        data = _synthetic()
        print("[self-test with synthetic data — no input file]\n", file=sys.stderr)

    r = score_macro(data, fast=args.fast, slow=args.slow,
                    slope_win=args.slope_win, corr_win=args.corr_win)
    print(json.dumps(asdict(r), indent=2, ensure_ascii=False) if args.json else render(r))
    return 0


def _synthetic() -> dict:
    """Generates ~260 bars with a gentle broadening bias to validate the engine."""
    import math
    n = 260
    def gen(start, drift, noise_seed):
        out, v = [], start
        for i in range(n):
            v *= (1 + drift + 0.004 * math.sin(i / 9 + noise_seed))
            out.append(round(v, 2))
        return out
    return {
        "as_of": "self-test",
        "series": {
            "SPY": gen(400, 0.0006, 1),
            "RSP": gen(150, 0.0009, 2),   # equal-weight gains -> broadening
            "IWM": gen(180, 0.0011, 3),   # small-cap wins
            "HYG": gen(75, 0.0004, 4),
            "LQD": gen(105, 0.0001, 5),
            "TLT": gen(95, -0.0003, 6),
            "XLY": gen(190, 0.0008, 7),
            "XLP": gen(78, 0.0002, 8),
        },
        "yield_spread": [round(-0.3 + 0.004 * i, 3) for i in range(60)],  # re-steepening
    }


if __name__ == "__main__":
    raise SystemExit(main())
