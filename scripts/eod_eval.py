#!/usr/bin/env python3
"""
eod_eval.py — end-of-day scorecard for the day's SPY signals.
================================================================================
Grades the two signals emitted today (11am ET + 3pm ET) by marking a $1000
investment made at each signal's price to the official market close.

For each signal:
  * value_at_close = $1000 * (close / signal_price)
  * pnl = value_at_close - $1000  (literal long P/L)
  * DIRECTIONAL GRADE — was the CALL right?
      - Bullish calls (RE-ENTRY / TACTICAL REBOUND / HOLD…): GOOD if pnl >= 0.
      - Exit/flat calls (EXIT… / STAY OUT / WAIT / OBSERVE): GOOD if pnl <= 0
        (correctly avoided a drawdown / stayed out of a down move).

Fail-safe: needs a valid Robinhood token (auto-refreshed by the caller) and the
official close for today. If either is unavailable, prints a short notice and
exits without a bogus grade. If no signals were logged today (e.g. both runs
aborted), it says so.

Prints a Telegram-ready summary to STDOUT (no_agent verbatim delivery).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import rh_data  # noqa: E402

TICKER = os.environ.get("DESK_TICKER", "SPY")
STAKE = 1000.0
STATE_DIR = Path(os.environ.get("DESK_POS_DIR",
                                str(Path.home() / ".hermes" / "state")))
SIGNAL_LOG = STATE_DIR / f"desk_signals_{TICKER}.jsonl"

BULLISH_PREFIXES = ("RE-ENTRY", "TACTICAL REBOUND", "HOLD (ride")
# Treated as "should be long"; everything else is a stay-out/exit stance.


def load_today_signals(today: str) -> list[dict]:
    if not SIGNAL_LOG.exists():
        return []
    out = []
    for line in SIGNAL_LOG.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("date") == today and rec.get("symbol") == TICKER:
            out.append(rec)
    return out


def is_bullish(action: str) -> bool:
    a = (action or "").upper()
    if a.startswith("EXIT"):
        return False
    return any(a.startswith(p.upper()) for p in BULLISH_PREFIXES)


def grade(action: str, pnl: float) -> tuple[str, str]:
    """Return (verdict, explanation) for the directional call."""
    if is_bullish(action):
        if pnl >= 0:
            return "✅ GOOD", "bullish call; $1000 long gained into the close"
        return "❌ POOR", "bullish call; $1000 long lost into the close"
    # exit / stay-out / wait / observe
    if pnl <= 0:
        return "✅ GOOD", "stay-out/exit call; a $1000 long would have lost — correctly avoided"
    return "⚠️ MISS", "stay-out/exit call; a $1000 long would have gained — missed upside"


def main() -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    signals = load_today_signals(today)
    if not signals:
        print(f"📉 EOD SCORECARD — {TICKER} ({today})\n"
              "No signals were logged today (both runs may have aborted). "
              "Nothing to grade.")
        return 0

    # Official market close (fail-safe on data/auth failure).
    try:
        data = rh_data.RHData()
        sc = data.session_close(TICKER)
        close = sc["price"]
        close_date = sc.get("date") or today
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ EOD SCORECARD — {TICKER} ({today}): could not fetch the "
              f"official close ({e}). Grading skipped; will retry next session.")
        return 0

    lines = []
    lines.append(f"📊 EOD SCORECARD — {TICKER}   ({today})")
    lines.append(f"Official close: {close:.2f}  (settle date {close_date})")
    lines.append(f"Stake graded per signal: ${STAKE:,.0f}")
    lines.append("─" * 40)

    total_pnl = 0.0
    good = 0
    labels = ["Signal 1 (≈11am ET)", "Signal 2 (≈3pm ET)"]
    for i, rec in enumerate(signals):
        label = labels[i] if i < len(labels) else f"Signal {i+1}"
        price = float(rec["price"])
        action = rec.get("action", "?")
        val = STAKE * (close / price)
        pnl = val - STAKE
        pct = (close / price - 1.0) * 100.0
        total_pnl += pnl
        verdict, why = grade(action, pnl)
        if verdict.startswith("✅"):
            good += 1
        tstamp = rec.get("ts_utc", "")[11:16]
        lines.append(f"{label}  [{tstamp} UTC]")
        lines.append(f"  Action : {action}")
        pt = rec.get("pillar_total")
        if isinstance(pt, int):
            lines.append(f"  Entry  : {price:.2f}   (pillar total {pt:+d})")
        else:
            lines.append(f"  Entry  : {price:.2f}")
        lines.append(f"  $1000 →: ${val:,.2f}   ({pnl:+.2f} / {pct:+.2f}%)")
        lines.append(f"  Grade  : {verdict} — {why}")
        lines.append("")

    lines.append("─" * 40)
    lines.append(f"Signals graded : {len(signals)}   |   Good calls: {good}/{len(signals)}")
    lines.append(f"Combined $1000×{len(signals)} mark-to-close P/L: {total_pnl:+.2f}")
    avg_pct = (total_pnl / (STAKE * len(signals))) * 100.0
    lines.append(f"Blended return : {avg_pct:+.2f}%")
    lines.append("")
    lines.append("Note: paper grading of signal timing vs. the day's close. "
                 "Signal-only — you approve all real orders.")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
