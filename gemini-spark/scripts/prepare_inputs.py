#!/usr/bin/env python3
"""
prepare_inputs.py
=================
Turns raw Robinhood MCP output into the inputs of macro_pillar.py / score.py,
deterministically, so Step 0 is code and not judgment:

  1. Drops bars flagged `interpolated: true`. get_equity_historicals synthesizes
     bars to fill gaps — including the whole span before a symbol's first trade
     (a June IPO requested from last July comes back with ~230 fake bars).
     Indicators computed over them are fiction, EMA200 especially.
  2. Step 0 — makes the last bar today's, using the broker's own dates instead
     of a hard-coded clock:
       - last real bar is today (ET)                          -> nothing appended
       - last bar == quote.previous_close_date and the quote's last trade is
         today at/after 09:30 ET                              -> append
         last_trade_price ("final" after 16:00 ET, else "provisional")
       - last bar == previous_close_date, no trade today
         (pre-open, weekend, holiday)                         -> nothing appended
       - last bar older than previous_close_date              -> STALE: symbol
         excluded (data does not reconcile)
     last_non_reg_trade_price is never used: extended-hours prints are not closes.

Input:
  --historicals  one or more saved get_equity_historicals results
                 ({"data": {"results": [{"symbol", "bars": [...]}]}})
  --quotes       one or more saved get_equity_quotes results
                 ({"data": {"results": [{"quote": {...}}]}})
Output (in --out):
  closes.json         per-symbol report: real bars, dropped bars, appended mode, warnings
  ticker_<SYM>.json   {symbol, close} for score.py (add --macro-score / --holding there)
  macro_input.json    {as_of, series} for macro_pillar.py, if any of the 8 ETFs are present

stdlib only. Python 3.9+.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone, tzinfo


class _USEastern(tzinfo):
    """America/New_York from the US DST rule (2nd Sunday of March 02:00 ->
    1st Sunday of November 02:00). Fallback for sandboxes without tzdata, where
    zoneinfo raises — stdlib only, exact for 2007 onwards."""

    @staticmethod
    def _nth_sunday(year: int, month: int, n: int) -> date:
        d = date(year, month, 1)
        d += timedelta(days=(6 - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)

    def _is_dst(self, dt: datetime) -> bool:
        # Wall time 01:00-02:00 on the November Sunday occurs twice; fold=1 is
        # the second (standard-time) occurrence, as in zoneinfo.
        naive = dt.replace(tzinfo=None)
        start = datetime.combine(self._nth_sunday(naive.year, 3, 2), datetime.min.time()) + timedelta(hours=2)
        end = datetime.combine(self._nth_sunday(naive.year, 11, 1), datetime.min.time()) + timedelta(hours=2)
        if end - timedelta(hours=1) <= naive < end:
            return dt.fold == 0
        return start <= naive < end

    def utcoffset(self, dt):
        return timedelta(hours=-4) if self._is_dst(dt) else timedelta(hours=-5)

    def dst(self, dt):
        return timedelta(hours=1) if self._is_dst(dt) else timedelta(0)

    def tzname(self, dt):
        return "EDT" if self._is_dst(dt) else "EST"

    def fromutc(self, dt):
        # DST switches at 07:00 UTC (Mar) and 06:00 UTC (Nov); decide on UTC time.
        naive = dt.replace(tzinfo=None)
        y = naive.year
        start = datetime.combine(self._nth_sunday(y, 3, 2), datetime.min.time()) + timedelta(hours=7)
        end = datetime.combine(self._nth_sunday(y, 11, 1), datetime.min.time()) + timedelta(hours=6)
        off = timedelta(hours=-4) if start <= naive < end else timedelta(hours=-5)
        # The first standard-time hour after the November switch is the repeated one.
        fold = 1 if end <= naive < end + timedelta(hours=1) else 0
        return (naive + off).replace(tzinfo=self, fold=fold)


try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # ZoneInfoNotFoundError / ImportError: no tzdata in this sandbox
    ET = _USEastern()
MACRO_ETFS = ["SPY", "RSP", "IWM", "HYG", "LQD", "TLT", "XLY", "XLP"]
MIN_BARS = 220


def _parse_ts(s: str) -> datetime:
    # Robinhood timestamps carry nanoseconds; fromisoformat takes at most micro.
    s = s.replace("Z", "+00:00")
    if "." in s:
        head, rest = s.split(".", 1)
        frac, tz = rest[:-6], rest[-6:]
        s = f"{head}.{frac[:6]}{tz}"
    return datetime.fromisoformat(s)


def _load_results(paths: list[str]) -> list[dict]:
    out = []
    for p in paths:
        with open(p) as f:
            out.extend(json.load(f)["data"]["results"])
    return out


def prepare(hist_results: list[dict], quote_results: list[dict], now: datetime) -> dict:
    now_et = now.astimezone(ET)
    today = now_et.date().isoformat()
    after_close = (now_et.hour, now_et.minute) >= (16, 0)
    quotes = {r["quote"]["symbol"]: r["quote"] for r in quote_results if "quote" in r}

    report = {}
    for r in hist_results:
        sym = r["symbol"]
        bars = r.get("bars") or []
        real = [b for b in bars if not b.get("interpolated")]
        rep = {"bars_total": len(bars), "interpolated_dropped": len(bars) - len(real),
               "real_bars": len(real), "last_bar": None, "appended": "none",
               "status": "ok", "warnings": []}
        report[sym] = rep
        if not real:
            rep["status"] = "excluded"
            rep["warnings"].append("no real bars")
            continue
        closes = [float(b["close_price"]) for b in real]
        last = real[-1]["begins_at"][:10]
        rep["last_bar"] = last

        q = quotes.get(sym)
        if last == today:
            pass
        elif q is None:
            rep["status"] = "excluded"
            rep["warnings"].append(f"no quote supplied; cannot verify last bar {last} is current")
        else:
            prev = q.get("previous_close_date")
            trade_ts = q.get("venue_last_trade_time")
            trade_et = _parse_ts(trade_ts).astimezone(ET) if trade_ts else None
            traded_today = bool(trade_et and trade_et.date().isoformat() == today
                                and (trade_et.hour, trade_et.minute) >= (9, 30))
            if prev and last < prev:
                rep["status"] = "excluded"
                rep["warnings"].append(f"STALE: last bar {last} is older than the prior session {prev}")
            elif last == prev and traded_today:
                closes.append(float(q["last_trade_price"]))
                rep["appended"] = "final" if after_close else "provisional"
                if not after_close:
                    rep["warnings"].append("intraday (partial bar): last bar is a live price, flags on it can flip by the close")
            elif last == prev:
                pass  # pre-open / weekend / holiday: last bar is the latest completed session
            else:
                rep["status"] = "excluded"
                rep["warnings"].append(f"last bar {last} does not match the broker's prior session {prev}")

        if rep["status"] != "ok":
            continue
        n = len(closes)
        rep["closes_used"] = n
        if rep["interpolated_dropped"]:
            rep["warnings"].append(f"dropped {rep['interpolated_dropped']} interpolated bars")
        if n < MIN_BARS:
            rep["warnings"].append(f"only {n} real bars (<{MIN_BARS}): EMA200 / long-term trend partial")
        rep["_close"] = closes
    return {"as_of": today, "now_et": now_et.isoformat(timespec="minutes"), "symbols": report}


def main() -> int:
    ap = argparse.ArgumentParser(description="Robinhood MCP output -> skill inputs (drops interpolated bars, applies Step 0).")
    ap.add_argument("--historicals", nargs="+", required=True)
    ap.add_argument("--quotes", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--now", help="ISO timestamp override (tests). Default: current time.")
    args = ap.parse_args()

    now = _parse_ts(args.now) if args.now else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    res = prepare(_load_results(args.historicals), _load_results(args.quotes), now)

    os.makedirs(args.out, exist_ok=True)
    series = {}
    for sym, rep in res["symbols"].items():
        closes = rep.pop("_close", None)
        if closes is None:
            continue
        with open(os.path.join(args.out, f"ticker_{sym}.json"), "w") as f:
            json.dump({"symbol": sym, "close": closes}, f)
        if sym in MACRO_ETFS:
            series[sym] = closes
    if series:
        with open(os.path.join(args.out, "macro_input.json"), "w") as f:
            json.dump({"as_of": res["as_of"], "series": series}, f)
    with open(os.path.join(args.out, "closes.json"), "w") as f:
        json.dump(res, f, indent=1)

    print(f"as_of {res['as_of']}  (now {res['now_et']} ET)")
    for sym, rep in sorted(res["symbols"].items()):
        tag = rep["status"].upper() if rep["status"] != "ok" else f"{rep['closes_used']} bars, appended={rep['appended']}"
        print(f"  {sym:6} {tag}" + (f"  · {'; '.join(rep['warnings'])}" if rep["warnings"] else ""))
    missing = [s for s in MACRO_ETFS if s not in series]
    if missing:
        print(f"  macro ETFs missing/excluded: {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
