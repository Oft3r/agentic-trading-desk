#!/usr/bin/env python3
"""
score.py
========
Converts the indicator stack into the THREE PILLARS framework
(Trend / Momentum / Macro-Sentiment, each graded -2..+2) and applies the
Agentic account decision logic:

  EXIT (total or partial): exit when bullish momentum is EXHAUSTED,
                          or when bearish momentum is RELENTLESS.
  RE-ENTRY: after exiting a bullish trend (partial or total), re-enter
            when a rebound/reversal pattern marks the start of a new
            bullish cycle.

All recommendations are framed within this exhaustion/rebound logic.
Imports indicators.py. The Macro-Sentiment score is injected (comes from
macro_pillar.py, shared by all names in the session).
stdlib only.
"""
from __future__ import annotations
import argparse
import json
import sys
from typing import Optional

import indicators as I


# --------------------------------------------------------------------------
# Pillar 1: Trend (EMA structure + price position + long-term slope)
# --------------------------------------------------------------------------

def score_trend(ind: dict) -> tuple[int, str]:
    c = ind["close"]
    e20, e50, e200 = ind["ema20"], ind["ema50"], ind["ema200"]
    s200 = ind["ema200_slope"]
    pts, bits = 0, []
    if e20 is not None:
        if c > e20: pts += 1; bits.append("price>EMA20")
        else: pts -= 1; bits.append("price<EMA20")
    if e20 is not None and e50 is not None:
        if e20 > e50: pts += 1; bits.append("EMA20>EMA50")
        else: pts -= 1; bits.append("EMA20<EMA50")
    if e50 is not None and e200 is not None:
        if e50 > e200: pts += 1; bits.append("EMA50>EMA200")
        else: pts -= 1; bits.append("EMA50<EMA200")
    if s200 is not None:
        if s200 > 0: pts += 1; bits.append("EMA200↑")
        else: pts -= 1; bits.append("EMA200↓")
    score = 2 if pts >= 3 else 1 if pts >= 1 else 0 if pts == 0 else -1 if pts >= -2 else -2
    return score, ", ".join(bits)


# --------------------------------------------------------------------------
# Pillar 2: Momentum (RSI-Wilder + MACD histogram + TRIX)
# --------------------------------------------------------------------------

def score_momentum(ind: dict) -> tuple[int, str]:
    rsi = ind["rsi14"]
    hist = ind["macd_hist"]
    trix, trix_sig = ind["trix"], ind["trix_signal"]
    pts, bits = 0, []
    if rsi is not None:
        if rsi >= 55: pts += 1; bits.append(f"RSI {rsi:.0f}≥55")
        elif rsi <= 45: pts -= 1; bits.append(f"RSI {rsi:.0f}≤45")
        else: bits.append(f"RSI {rsi:.0f} neutral")
    if hist is not None:
        if hist > 0: pts += 1; bits.append("MACD hist>0")
        else: pts -= 1; bits.append("MACD hist<0")
    if trix is not None and trix_sig is not None:
        if trix > trix_sig and trix > 0: pts += 1; bits.append("TRIX>signal>0")
        elif trix < trix_sig and trix < 0: pts -= 1; bits.append("TRIX<signal<0")
        else: bits.append("TRIX mixed")
    score = 2 if pts >= 2 else 1 if pts == 1 else 0 if pts == 0 else -1 if pts == -1 else -2
    return score, ", ".join(bits)


# --------------------------------------------------------------------------
# Decision Layer: exhaustion / relentless / rebound
# --------------------------------------------------------------------------

def _flags(ind: dict) -> dict:
    c = ind["close"]
    e20 = ind["ema20"]; e50 = ind["ema50"]; e200 = ind["ema200"]
    s200 = ind["ema200_slope"]
    rsi, rsi_p = ind["rsi14"], ind["rsi14_prev"]
    hist, hist_p = ind["macd_hist"], ind["macd_hist_prev"]
    trix, trix_sig = ind["trix"], ind["trix_signal"]
    pb = ind["percent_b"]
    stretch = (c / e20 - 1.0) if e20 else 0.0

    exhaustion, bearish, rebound = [], [], []

    # --- Bullish exhaustion ---
    if rsi is not None and rsi_p is not None and rsi >= 70 and rsi < rsi_p:
        exhaustion.append(f"RSI turning from overbought ({rsi_p:.0f}→{rsi:.0f})")
    if hist is not None and hist_p is not None and hist > 0 and hist < hist_p:
        exhaustion.append("MACD histogram shrinking in positive territory")
    if pb is not None and pb >= 1.0:
        exhaustion.append("price at/above upper Bollinger Band (%B≥1)")
    if stretch >= 0.10:
        exhaustion.append(f"price stretched {stretch*100:.0f}% above EMA20")

    # --- Relentless bearish ---
    if e50 and e200 and s200 is not None and c < e50 and e50 < e200 and s200 < 0:
        bearish.append("price<EMA50<EMA200 with EMA200↓")
    if hist is not None and hist_p is not None and hist < 0 and hist < hist_p:
        bearish.append("MACD histogram deepening in negative territory")
    if trix is not None and trix_sig is not None and trix < trix_sig and trix < 0:
        bearish.append("TRIX<signal below zero")
    if rsi is not None and rsi_p is not None and rsi < 45 and rsi < rsi_p:
        bearish.append(f"RSI weak and falling ({rsi:.0f})")

    # --- Rebound / reversal (for re-entry) ---
    if rsi is not None and rsi_p is not None and rsi_p < 35 and rsi > rsi_p:
        rebound.append(f"RSI turning from oversold ({rsi_p:.0f}→{rsi:.0f})")
    if hist is not None and hist_p is not None and hist > hist_p and hist_p < 0:
        rebound.append("MACD histogram crossing bullishly")
    # Genuine recovery of EMA20: currently above EMA20, but closed below it within last 5 bars.
    # Without a recent dip, it is a normal uptrend, not a rebound.
    bsb = ind.get("bars_since_below_ema20")
    if (e20 and c > e20 and ind["ema20_slope"] is not None and ind["ema20_slope"] > 0
            and bsb is not None and 1 <= bsb <= 5):
        rebound.append(f"price reclaims EMA20 (closed below {bsb} bar{'s' if bsb > 1 else ''} ago)")
    # Fresh TRIX cross detected on the crossover bar (not while it persists).
    trix_p, sig_p = ind["trix_prev"], ind["trix_signal_prev"]
    if (trix is not None and trix_sig is not None and trix_p is not None and sig_p is not None
            and trix > trix_sig and trix_p <= sig_p and trix <= 0):
        rebound.append("fresh bullish TRIX cross below zero")

    # Structure in a true death-cross (not proxied by trend score)
    death_cross = bool(e50 and e200 and e50 < e200 and c < e50)

    return {"exhaustion": exhaustion, "bearish": bearish, "rebound": rebound,
            "death_cross": death_cross, "stretch_pct": round(stretch * 100, 1)}


def decide(ind: dict, trend: int, mom: int, macro: Optional[int],
           holding: Optional[bool]) -> dict:
    """
    Decision cascade aligned to the Agentic account style: short-term returns
    via capital rotation. The cycle is enter on rebound → ride → exit on
    exhaustion → wait for next trigger. Accumulating positions is not the default
    (keeps capital trapped). A rebound inside a death-cross is a counter-trend
    TACTICAL opportunity (reduced size, tight stop), not a new cycle.
    """
    f = _flags(ind)
    n_exh, n_bear, n_reb = len(f["exhaustion"]), len(f["bearish"]), len(f["rebound"])
    dc = f["death_cross"]
    in_pos = holding is True  # None treated as flat (entry framing)

    # Priority cascade — separated by holding status:
    # HOLDER: exit triggers first (exhaustion, relentless).
    # FLAT:   fresh entry triggers first (lagging structural bearish flags
    #         should not overshadow a tactical rebound).
    if in_pos and n_exh >= 2:
        action = "EXIT / TRIM"
        rationale = "Bullish momentum EXHAUSTED."
        framing = ("Partial or full exit: buying momentum is dying out. "
                   "Rotate capital and flag for re-entry on the next rebound.")
    elif in_pos and (n_bear >= 3 or (dc and n_bear >= 2)):
        action = "EXIT"
        rationale = "Bearish momentum RELENTLESS."
        framing = "Exit: selling pressure is sustained. Do not average down."
        if n_reb >= 2:
            framing += (" Rebound in progress: use it to exit at a better price, "
                        "not to justify holding.")
    elif not in_pos and n_reb >= 2 and not dc:
        action = "RE-ENTRY (new cycle)"
        rationale = "Rebound/reversal with healthy EMA structure: likely start of a new bullish cycle."
        framing = ("Valid entry trigger. Confirm with candle/volume before entering "
                   "full size; stop below the rebound pivot.")
    elif not in_pos and n_reb >= 2 and dc:
        action = "TACTICAL REBOUND (counter-trend)"
        rationale = "Rebound signals within a death-cross: tactical trade, NOT a new cycle."
        framing = ("Short-term opportunity against the structure: reduced size, "
                   "close target (EMA20/EMA50 or middle band), tight stop, and quick "
                   "exit. Do not let it turn into a hold — the underlying trend remains bearish.")
        if n_bear >= 2:
            framing += " Bearish flags still active: extra tight leash."
    elif not in_pos and (n_bear >= 3 or (dc and n_bear >= 2)):
        action = "STAY OUT / AVOID"
        rationale = "Bearish momentum RELENTLESS, no fresh rebound trigger."
        framing = "Out. Watch for capitulation: the trigger would be a fresh RSI/MACD turn."
    elif trend >= 1 and mom >= 1:
        if in_pos:
            action = "HOLD (ride the cycle)"
            rationale = "Bullish cycle intact (Trend and Momentum positive)."
            framing = ("Hold and watch for exhaustion: the next expected action is "
                       "EXIT with profit, not adding to position. Accumulating is not the "
                       "default (capital rotation > large position).")
        else:
            action = "WAIT (do not chase)"
            rationale = "Healthy trend but no fresh entry trigger."
            framing = ("Entering mid-trend is chasing: poor R/R for the short term. "
                       "Wait for pullback to EMA20 and turn, or the next confirmed rebound.")
    elif trend <= -1 and mom <= -1:
        if in_pos:
            action = "HOLD (under review)"
            rationale = "Weak structure and momentum, but no full exit trigger."
            framing = ("Do not add. Prepare to exit: if more bearish flags appear or the "
                       "current rebound fizzles out, execute EXIT. If a rebound is active, "
                       "it can be used to exit at a better price.")
        else:
            action = "STAY OUT / AVOID"
            rationale = "Negative structure and momentum, no signs of turning."
            framing = "Out. The next trigger here would be a confirmed rebound (tactical trade)."
    else:
        action = "HOLD / OBSERVE" if in_pos else "OBSERVE"
        rationale = "Mixed signals; no clear exhaustion or rebound trigger."
        framing = "No action. Watch the next close."

    # Adjust for adverse macro
    if macro is not None and macro <= -1:
        if action == "HOLD (ride the cycle)":
            framing += " ⚠ Adverse macro: lower the exit threshold (take profit earlier)."
        elif action == "TACTICAL REBOUND (counter-trend)":
            framing += " ⚠ Adverse macro: reduce size further or skip this rebound."
        elif action == "RE-ENTRY (new cycle)":
            framing += " ⚠ Adverse macro: entry in reduced size."

    # Position context
    if in_pos and n_reb >= 2 and action.startswith("HOLD"):
        framing += " (Rebound signals in progress reinforce holding.)"
    if holding is False and action in ("EXIT / TRIM", "EXIT"):
        framing += " (You are flat: the exit signal only confirms not entering long.)"

    return {"action": action, "rationale": rationale, "framing": framing, "flags": f}


# --------------------------------------------------------------------------
# Full Scorecard Card
# --------------------------------------------------------------------------

def score_symbol(close: list[float], macro_score: Optional[int] = None,
                 symbol: Optional[str] = None, holding: Optional[bool] = None,
                 slope_lookback: int = 5) -> dict:
    ind = I.compute(close, slope_lookback)
    t, t_detail = score_trend(ind)
    m, m_detail = score_momentum(ind)
    dec = decide(ind, t, m, macro_score, holding)
    composite = t + m + (macro_score if macro_score is not None else 0)
    return {
        "symbol": symbol,
        "n_bars": ind["n_bars"],
        "warning": ind["warning"],
        "pillars": {
            "trend": {"score": t, "detail": t_detail},
            "momentum": {"score": m, "detail": m_detail},
            "macro_sentiment": {"score": macro_score, "detail": "injected from macro_pillar.py"},
        },
        "pillar_total": composite,
        "decision": dec,
        "indicators": I._round(ind),
    }


def render(card: dict) -> str:
    p = card["pillars"]; d = card["decision"]
    ms = p["macro_sentiment"]["score"]
    L = []
    L.append(f"{'═'*54}")
    L.append(f" {card['symbol'] or 'SYMBOL'}   ·   {card['n_bars']} bars")
    L.append(f"{'═'*54}")
    def line(name, sc, det):
        s = f"{sc:+d}" if sc is not None else " ?"
        return f"  {name:<16} {s:>3}   {det}"
    L.append(line("Trend", p["trend"]["score"], p["trend"]["detail"]))
    L.append(line("Momentum", p["momentum"]["score"], p["momentum"]["detail"]))
    L.append(line("Macro-Sentiment", ms, p["macro_sentiment"]["detail"] if ms is not None else "(not injected)"))
    L.append(f"  {'─'*50}")
    tot = card["pillar_total"]
    L.append(f"  TOTAL (-6..+6): {tot:+d}")
    L.append(f"{'─'*54}")
    L.append(f"  ► {d['action']}  —  {d['rationale']}")
    L.append(f"    {d['framing']}")
    f = d["flags"]
    if f["exhaustion"]:
        L.append(f"    exhaustion: {'; '.join(f['exhaustion'])}")
    if f["bearish"]:
        L.append(f"    bearish: {'; '.join(f['bearish'])}")
    if f["rebound"]:
        L.append(f"    rebound: {'; '.join(f['rebound'])}")
    if f.get("death_cross"):
        L.append("    structure: active death-cross (EMA50<EMA200, price<EMA50)")
    if card["warning"]:
        L.append(f"    ⚠ {card['warning']}")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="Three pillars scoring + exit/re-entry decision.")
    ap.add_argument("input", nargs="?", help="JSON: {symbol, close:[...], macro_score?, holding?}. Without file: self-test.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.input:
        with open(args.input) as f:
            raw = json.load(f)
        card = score_symbol(
            [float(x) for x in raw["close"]],
            macro_score=raw.get("macro_score"),
            symbol=raw.get("symbol"),
            holding=raw.get("holding"),
        )
    else:
        import math
        # bullish series stretching toward a ceiling (should trigger exhaustion)
        close = [round(100 + i * 0.25 + 6 * math.sin(i / 12), 2) for i in range(260)]
        close += [close[-1] * 1.05, close[-1] * 1.10]  # final spike
        card = score_symbol(close, macro_score=1, symbol="SELFTEST", holding=True)
        print("[synthetic self-test]\n", file=sys.stderr)

    print(json.dumps(card, indent=2, ensure_ascii=False) if args.json else render(card))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
