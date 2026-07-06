#!/usr/bin/env python3
"""
hist_to_batch.py — turn saved Robinhood historicals into pipeline inputs
========================================================================
`get_equity_historicals` returns huge JSON (a day of daily bars for 8 symbols
is ~440k chars — over the token cap). The token-safe pattern is:

  1. Fetch in batches of <=4 symbols/call; each oversized response is written
     to a tool-results file on disk (Claude never loads it into context).
  2. Run THIS script over those files. It extracts only close/high/low per
     symbol and emits the two small JSON inputs the pipeline needs:
       - macro_input.json  -> macro_pillar.py   (series for the macro pillar)
       - batch.json        -> daily_briefing.py  (watchlist OHLC + holding)

Deterministic, stdlib only. Reads bars via ijson-free streaming json.load per
file (each file is one MCP result: {data:{results:[{symbol,bars:[...]}]}}).

Usage:
  python3 hist_to_batch.py \
      --hist f1.json f2.json \
      --watch NVDA,AAPL,TSLA,MSFT \
      --macro SPY,RSP,IWM,HYG,LQD,TLT,XLY,XLP \
      --as-of 2026-07-06T13:30:00Z \
      --holdings holdings.json \
      --macro-out macro_input.json \
      --batch-out batch.json
"""
from __future__ import annotations
import argparse
import json
import sys


def _f(x) -> float:
    return round(float(x), 4)


def load_bars(paths: list[str]) -> dict[str, dict]:
    """symbol -> {close:[...], high:[...], low:[...]} from one or more MCP files."""
    out: dict[str, dict] = {}
    for p in paths:
        with open(p) as fh:
            doc = json.load(fh)
        results = (doc.get("data") or {}).get("results") or doc.get("results") or []
        for r in results:
            sym = str(r.get("symbol", "")).upper()
            bars = r.get("bars") or []
            if not sym or not bars:
                continue
            out[sym] = {
                "close": [_f(b["close_price"]) for b in bars],
                "high": [_f(b["high_price"]) for b in bars],
                "low": [_f(b["low_price"]) for b in bars],
            }
    return out


def _syms(csv: str) -> list[str]:
    return [s.strip().upper() for s in csv.split(",") if s.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract closes from saved historicals into pipeline inputs.")
    ap.add_argument("--hist", nargs="+", required=True, help="saved get_equity_historicals JSON file(s)")
    ap.add_argument("--watch", required=True, help="comma-separated watchlist symbols")
    ap.add_argument("--macro", default="SPY,RSP,IWM,HYG,LQD,TLT,XLY,XLP",
                    help="comma-separated macro ETF symbols")
    ap.add_argument("--as-of", default="", help="timestamp for the briefing/macro inputs")
    ap.add_argument("--yield-spread", help="optional JSON file: list of 10y-2y spread observations")
    ap.add_argument("--holdings", help="optional JSON file: {SYM: true/false} holding flags")
    ap.add_argument("--macro-out", default="macro_input.json")
    ap.add_argument("--batch-out", default="batch.json")
    args = ap.parse_args()

    bars = load_bars(args.hist)
    watch, macro = _syms(args.watch), _syms(args.macro)

    holdings = {}
    if args.holdings:
        with open(args.holdings) as fh:
            holdings = {k.upper(): bool(v) for k, v in json.load(fh).items()}

    missing = [s for s in watch + macro if s not in bars]
    if missing:
        print(f"WARNING: no bars for {missing} (skipped)", file=sys.stderr)

    # macro_input.json  (series of closes only)
    macro_in = {"as_of": args.as_of,
                "series": {s: bars[s]["close"] for s in macro if s in bars}}
    if args.yield_spread:
        with open(args.yield_spread) as fh:
            macro_in["yield_spread"] = json.load(fh)
    with open(args.macro_out, "w") as fh:
        json.dump(macro_in, fh)

    # batch.json  (watchlist OHLC + holding)  — macro_score injected later
    batch = {"as_of": args.as_of, "tickers": {
        s: {"close": bars[s]["close"], "high": bars[s]["high"],
            "low": bars[s]["low"], "holding": holdings.get(s, False)}
        for s in watch if s in bars}}
    with open(args.batch_out, "w") as fh:
        json.dump(batch, fh)

    print(f"wrote {args.macro_out} ({len(macro_in['series'])} series) and "
          f"{args.batch_out} ({len(batch['tickers'])} tickers); "
          f"bars/symbol={ {s: len(bars[s]['close']) for s in list(bars)[:1]} }")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
