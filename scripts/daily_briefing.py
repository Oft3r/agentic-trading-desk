#!/usr/bin/env python3
"""
daily_briefing.py — morning scan of the watchlist
=================================================
Runs the exact score.py rules over every ticker in the watchlist, detects
the patterns each one is printing, and buckets them into an actionable
briefing: OPPORTUNITIES (flat → enter), WARNINGS (holding → exit/trim),
HOLDS, and WATCH. Deterministic, stdlib only — Claude fetches the bars via
Robinhood MCP, this ranks and summarizes; render.py briefing shows it.

Input JSON (same shape as auto_engine batch):
  {
    "as_of": "2026-07-06T13:30:00Z",
    "macro_score": 1,
    "macro": { ... optional macro_pillar.py output for the banner ... },
    "tickers": {
      "NVDA": {"close":[...], "high":[...], "low":[...], "holding": true},
      "AAPL": {"close":[...], "holding": false},
      ...
    }
  }

Usage:
  python3 daily_briefing.py batch.json --json > briefing.json
  python3 render.py briefing briefing.json -o briefing.html
  # or, with no input, a self-test sample:
  python3 daily_briefing.py --json
"""
from __future__ import annotations
import argparse
import json
import sys

import score as S

# Human-readable pattern tags derived from score.py flags.
_EXH = "exhaustion"
_BEAR = "bearish"
_REB = "rebound"


def _bucket(action: str) -> str:
    a = (action or "").upper()
    if a.startswith(("RE-ENTRY", "TACTICAL")):
        return "opportunity"
    if a.startswith("EXIT"):
        return "warning"
    if a.startswith("HOLD") and "OBSERVE" not in a:
        return "hold"
    return "watch"


def _patterns(flags: dict) -> list[str]:
    """Short pattern tags for the briefing (the flags ARE the patterns)."""
    out: list[str] = []
    for x in flags.get("exhaustion", []):
        out.append(x)
    for x in flags.get("bearish", []):
        out.append(x)
    for x in flags.get("rebound", []):
        out.append(x)
    if flags.get("death_cross"):
        out.append("active death-cross (EMA50<EMA200, price<EMA50)")
    return out


# Opportunity ranking: higher pillar_total first, entries beat rebounds.
def _opp_rank(row: dict) -> tuple:
    a = row["action"].upper()
    entry_first = 0 if a.startswith("RE-ENTRY") else 1
    return (entry_first, -row["pillar_total"])


def _warn_rank(row: dict) -> tuple:
    # Deepest negative first; relentless EXIT before EXIT/TRIM.
    a = row["action"].upper()
    exit_first = 0 if a == "EXIT" else 1
    return (exit_first, row["pillar_total"])


def build(batch: dict) -> dict:
    macro_score = batch.get("macro_score")
    if macro_score is None:
        macro_score = (batch.get("macro") or {}).get("pillar_score", 0)
    rows: list[dict] = []
    for sym, td in (batch.get("tickers") or {}).items():
        close = td.get("close") or []
        if len(close) < 30:
            rows.append({"symbol": sym.upper(), "action": "INSUFFICIENT DATA",
                         "pillar_total": 0, "score": {}, "close": close[-1] if close else None,
                         "patterns": ["need ≥30 bars"], "bucket": "watch",
                         "note": "not enough history to score"})
            continue
        card = S.score_symbol(close, macro_score=macro_score, symbol=sym.upper(),
                              holding=bool(td.get("holding")),
                              high=td.get("high"), low=td.get("low"),
                              with_garch=td.get("with_garch", True))
        d = card["decision"]
        p = card["pillars"]
        r = card.get("risk") or {}
        action = d["action"]
        rows.append({
            "symbol": card["symbol"],
            "action": action,
            "bucket": _bucket(action),
            "pillar_total": card["pillar_total"],
            "holding": bool(td.get("holding")),
            "score": {"trend": p["trend"]["score"],
                      "momentum": p["momentum"]["score"],
                      "macro": p["macro_sentiment"]["score"]},
            "close": close[-1],
            "spark": close[-40:],
            "stop": r.get("suggested_stop"),
            "stop_pct": r.get("stop_distance_pct"),
            "size_fraction": r.get("vol_target_fraction"),
            "forecast_vol": r.get("forecast_vol_annual"),
            "zscore_20": r.get("zscore_20"),
            "patterns": _patterns(d["flags"]),
            "note": f'{d["rationale"]} {d["framing"]}'.strip(),
        })

    opportunities = sorted([r for r in rows if r["bucket"] == "opportunity"], key=_opp_rank)
    warnings = sorted([r for r in rows if r["bucket"] == "warning"], key=_warn_rank)
    holds = [r for r in rows if r["bucket"] == "hold"]
    watch = [r for r in rows if r["bucket"] == "watch"]

    return {
        "as_of": batch.get("as_of"),
        "macro": batch.get("macro") or {"pillar_score": macro_score},
        "summary": {"tickers": len(rows), "opportunities": len(opportunities),
                    "warnings": len(warnings), "holds": len(holds), "watch": len(watch)},
        "sections": {"opportunities": opportunities, "warnings": warnings,
                     "holds": holds, "watch": watch},
        "rows": sorted(rows, key=lambda r: -r["pillar_total"]),
    }


def _selftest_batch() -> dict:
    import math
    def series(base, trend, wave, n=260):
        return [round(base + trend * i + wave * math.sin(i / 12), 2) for i in range(n)]
    up = series(100, 0.30, 4)                          # healthy uptrend
    up += [up[-1] * 1.04, up[-1] * 1.09]               # stretched → exhaustion
    down = series(300, -0.20, 6)                       # death-cross territory
    reb = series(50, 0.02, 2)
    reb += [reb[-1] * 0.98, reb[-1] * 1.03, reb[-1] * 1.06]   # rebound tick-up
    flat = series(200, 0.01, 1)
    return {
        "as_of": "2026-07-06T13:30:00Z",
        "macro": {"pillar_score": 1, "regime": "risk-on",
                  "stat_regime": {"state": "calm", "p_turbulent": 0.12}},
        "macro_score": 1,
        "tickers": {
            "NVDA": {"close": up, "holding": True},
            "TSLA": {"close": down, "holding": True},
            "AAPL": {"close": reb, "holding": False},
            "MSFT": {"close": flat, "holding": False},
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Daily watchlist briefing (patterns + suggestions).")
    ap.add_argument("batch", nargs="?", help="batch JSON (omit for self-test)")
    ap.add_argument("--json", action="store_true", help="print briefing JSON to stdout")
    ap.add_argument("-o", "--out", help="write briefing JSON to a file")
    args = ap.parse_args()

    if args.batch:
        with open(args.batch) as f:
            batch = json.load(f)
    else:
        batch = _selftest_batch()
        print("[self-test briefing]", file=sys.stderr)

    briefing = build(batch)

    if args.out:
        with open(args.out, "w") as f:
            json.dump(briefing, f, indent=2)
        print(f"wrote {args.out}")
    if args.json or not args.out:
        print(json.dumps(briefing, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
