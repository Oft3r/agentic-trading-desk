#!/usr/bin/env python3
"""
allocate.py — score → % of a 30-day Agentic budget, paced, with buy/sell deltas
================================================================================
Turns a daily_briefing.json into a target book for the Agentic (cash) account,
sized against a 30-DAY BUDGET CYCLE:

  - The budget ($500 by default) funds one 30-day cycle.
  - Each eligible ticker gets a SCORE (pillar_total, -6..+6) and a target
    WEIGHT (% of budget), sized score-proportionally, vol-aware, per-name capped.
  - Purchases are PACED across the cycle: today's proposed buy for a name is
    (target_dollars - current_value) / days_remaining. So you scale in over the
    30 days instead of lump-sum, and the pace self-corrects as scores/prices move.
  - At day 30 the cycle refreshes (cycle_index advances): re-score, rebuild
    targets, and rotate — held names no longer eligible are SELL → 0.

Holdings (`current_value` per symbol) MUST come from the Agentic account
(692801525), passed via --held. Broad-index ETFs are excluded by default so the
rotation trades single names (override with --allow-etfs).

Deterministic, stdlib only. Claude fetches data + confirms; this only proposes.

Usage:
  python3 allocate.py briefing.json \
      --cash 500 --cycle-days 30 --cycle-start 2026-07-06 --as-of 2026-07-06 \
      --held agentic_positions.json --per-name-cap 0.25 --json
"""
from __future__ import annotations
import argparse
import datetime as _dt
import json
import sys

_AVOID = ("EXIT", "STAY OUT", "AVOID")

# Broad-index / total-market ETFs excluded from the single-name rotation.
_BROAD_ETFS = {
    "VOO", "SPY", "VTI", "IVV", "SPLG", "RSP", "SCHB", "SCHX", "ITOT",
    "QQQ", "QQQM", "DIA", "IWM", "IWB", "IWV", "VUG", "VTV", "VV", "MGC",
    "VEA", "VWO", "VXUS", "EFA", "EEM", "ACWI", "URTH",
}


def _num(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _date(s: str) -> _dt.date:
    return _dt.date.fromisoformat(str(s)[:10])


def cycle_info(as_of: str, cycle_start: str, cycle_days: int) -> dict:
    """Where are we in the 30-day budget cycle? day_in_cycle is 1-based;
    days_remaining includes today (so today's tranche = remaining/days_remaining)."""
    try:
        a, c = _date(as_of), _date(cycle_start)
        elapsed = (a - c).days
    except (ValueError, TypeError):
        elapsed = 0
    if elapsed < 0:
        elapsed = 0
    idx = elapsed // cycle_days
    off = elapsed % cycle_days                 # 0-based day offset within cycle
    this_start = _date(cycle_start) + _dt.timedelta(days=idx * cycle_days) \
        if _valid(cycle_start) else None
    return {
        "cycle_index": idx,
        "cycle_days": cycle_days,
        "day_in_cycle": off + 1,
        "days_remaining": cycle_days - off,    # includes today
        "cycle_start": this_start.isoformat() if this_start else None,
        "cycle_end": (this_start + _dt.timedelta(days=cycle_days - 1)).isoformat()
        if this_start else None,
        "refresh_today": off == 0,
    }


def _valid(s: str) -> bool:
    try:
        _date(s)
        return True
    except (ValueError, TypeError):
        return False


def _eligible(row: dict) -> bool:
    a = (row.get("action") or "").upper()
    held = bool(row.get("holding"))
    if a.startswith(("RE-ENTRY", "TACTICAL")):
        return True                      # fresh long trigger
    if held and not any(k in a for k in _AVOID):
        return True                      # keep an existing position unless told out
    return False


def allocate(briefing: dict, cash: float = 500.0, deploy: float = 1.0,
             per_name_cap: float = 0.25, min_score: float = 0.0,
             cycle_days: int = 30, cycle_start: str | None = None,
             as_of: str | None = None, held: dict | None = None,
             allow_etfs: bool = False, exclude: set | None = None) -> dict:
    rows = briefing.get("rows") or []
    as_of = as_of or briefing.get("as_of") or ""
    cycle_start = cycle_start or as_of
    held = {k.upper(): _num(v) for k, v in (held or {}).items()}
    deny = set(exclude or set())
    if not allow_etfs:
        deny |= _BROAD_ETFS
    cyc = cycle_info(as_of, cycle_start, cycle_days)
    days_left = max(cyc["days_remaining"], 1)

    held_syms = {r["symbol"] for r in rows if r.get("holding")} | set(held)

    # 1. candidate longs with a positive score (minus excluded ETFs)
    cand, excluded = [], []
    for r in rows:
        sym = r["symbol"]
        score = _num(r.get("pillar_total"))
        if not (_eligible(r) and score > min_score):
            continue
        if sym in deny:
            excluded.append(sym)
            continue
        vol_cap = _num(r.get("size_fraction"), per_name_cap) or per_name_cap
        cand.append({
            "symbol": sym, "score": score, "close": _num(r.get("close")),
            "action": r.get("action"), "holding": sym in held_syms,
            "cap": min(per_name_cap, vol_cap if vol_cap > 0 else per_name_cap),
        })

    # 2. score-proportional weights with iterative cap + redistribution
    weights = {c["symbol"]: 0.0 for c in cand}
    active = {c["symbol"]: c for c in cand}
    remaining = 1.0
    while active:
        tot = sum(active[s]["score"] for s in active)
        if tot <= 0:
            break
        newly = []
        for s, c in active.items():
            w = remaining * c["score"] / tot
            if w >= c["cap"]:
                weights[s] = c["cap"]
                remaining -= c["cap"]
                newly.append(s)
            else:
                weights[s] = w
        if not newly:
            break
        for s in newly:
            del active[s]

    # 3. dollarize target + pace today's tranche across days_remaining
    book = []
    for c in cand:
        w = weights[c["symbol"]] * deploy
        target = round(cash * w, 2)
        cur = held.get(c["symbol"], 0.0)
        gap = max(target - cur, 0.0)
        today = round(gap / days_left, 2)
        today_sh = round(today / c["close"], 6) if c["close"] > 0 else 0.0
        book.append({
            "symbol": c["symbol"], "score": c["score"], "action": c["action"],
            "holding": c["holding"], "close": c["close"],
            "target_weight_pct": round(w * 100, 2), "target_dollars": target,
            "current_value": round(cur, 2), "gap_to_target": round(gap, 2),
            "buy_today_dollars": today, "buy_today_shares": today_sh,
            "side": "ADD" if c["holding"] else "BUY",
        })
    book.sort(key=lambda x: -x["target_weight_pct"])

    funded = {b["symbol"] for b in book if b["target_weight_pct"] > 0}
    sells = []
    for r in rows:
        sym = r["symbol"]
        if sym in held_syms and sym not in funded:
            cur = held.get(sym, 0.0)
            sells.append({
                "symbol": sym, "action": r.get("action"),
                "score": _num(r.get("pillar_total")), "current_value": round(cur, 2),
                "reason": "no longer eligible — rotate to cash", "side": "SELL",
                "sell_today_shares": round(cur / _num(r.get("close")), 6)
                if _num(r.get("close")) > 0 else None,
            })

    deployed_pct = round(sum(b["target_weight_pct"] for b in book), 2)
    invested = round(sum(b["current_value"] for b in book), 2)
    buy_today = round(sum(b["buy_today_dollars"] for b in book), 2)
    return {
        "as_of": as_of,
        "account": {"name": "Agentic (cash)", "budget": cash,
                    "deploy_target_pct": round(deploy * 100, 2)},
        "cycle": cyc,
        "params": {"per_name_cap_pct": round(per_name_cap * 100, 2),
                   "min_score": min_score, "allow_etfs": allow_etfs,
                   "excluded_etfs": sorted(excluded)},
        "summary": {"funded": len(funded), "deployed_target_pct": deployed_pct,
                    "invested_so_far": invested, "buy_today_dollars": buy_today,
                    "sells": len(sells)},
        "buys": book, "sells": sells,
    }


def _print(a: dict) -> None:
    c, s = a["cycle"], a["summary"]
    print(f"Agentic ${a['account']['budget']:.0f} budget · 30-day cycle #{c['cycle_index']} "
          f"day {c['day_in_cycle']}/{c['cycle_days']} · {c['days_remaining']} days left"
          f"{'  [REFRESH DAY]' if c['refresh_today'] else ''}")
    if c["cycle_start"]:
        print(f"cycle {c['cycle_start']} → {c['cycle_end']}")
    print(f"funded {s['funded']} · target {s['deployed_target_pct']:.1f}% · invested "
          f"${s['invested_so_far']:.2f} · BUY TODAY ${s['buy_today_dollars']:.2f} · sells {s['sells']}")
    if a["params"]["excluded_etfs"]:
        print(f"excluded ETFs: {', '.join(a['params']['excluded_etfs'])}")
    print(f"\n{'SYM':6} {'SCORE':>5} {'TARGET%':>7} {'TARGET$':>8} {'HELD$':>7} "
          f"{'BUY_TODAY$':>10} {'SHARES':>9}  ACTION")
    for b in a["buys"]:
        print(f"{b['symbol']:6} {b['score']:+5.0f} {b['target_weight_pct']:6.1f}% "
              f"{b['target_dollars']:8.2f} {b['current_value']:7.2f} "
              f"{b['buy_today_dollars']:10.2f} {b['buy_today_shares']:9.4f}  "
              f"{b['side']} · {b['action']}")
    for x in a["sells"]:
        sh = x.get("sell_today_shares")
        print(f"{x['symbol']:6} {x['score']:+5.0f} {'0.0%':>7} {'—':>8} "
              f"{x['current_value']:7.2f} {'SELL ALL':>10} {sh if sh is not None else '—':>9}  "
              f"SELL · {x['action']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Score → % of a 30-day Agentic budget, paced.")
    ap.add_argument("briefing", nargs="?", help="briefing.json (omit for self-test)")
    ap.add_argument("--cash", type=float, default=500.0, help="Agentic budget for the cycle")
    ap.add_argument("--deploy", type=float, default=1.0, help="fraction of budget to deploy (0-1)")
    ap.add_argument("--per-name-cap", type=float, default=0.25, help="max weight per name (0-1)")
    ap.add_argument("--min-score", type=float, default=0.0, help="minimum pillar_total to fund")
    ap.add_argument("--cycle-days", type=int, default=30, help="budget cycle length in days")
    ap.add_argument("--cycle-start", help="YYYY-MM-DD anchor for the cycle (default: as-of)")
    ap.add_argument("--as-of", help="YYYY-MM-DD today (default: briefing as_of)")
    ap.add_argument("--held", help="JSON {SYM: current_market_value} — Agentic positions only")
    ap.add_argument("--allow-etfs", action="store_true", help="include broad-index ETFs")
    ap.add_argument("--exclude", help="extra comma-separated symbols to exclude")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("-o", "--out")
    args = ap.parse_args()

    if args.briefing:
        with open(args.briefing) as f:
            briefing = json.load(f)
    else:
        briefing = {"as_of": "2026-07-06", "rows": [
            {"symbol": "XOM", "action": "RE-ENTRY (new cycle)", "pillar_total": 5, "holding": False, "close": 137.09, "size_fraction": 1.0},
            {"symbol": "BA", "action": "RE-ENTRY (new cycle)", "pillar_total": 4, "holding": False, "close": 226.49, "size_fraction": 0.45},
            {"symbol": "NOW", "action": "TACTICAL BOUNCE", "pillar_total": 3, "holding": False, "close": 902.0, "size_fraction": 0.5},
            {"symbol": "SPY", "action": "RE-ENTRY (new cycle)", "pillar_total": 2, "holding": False, "close": 744.78, "size_fraction": 1.0},
            {"symbol": "NVDA", "action": "STAY OUT / AVOID", "pillar_total": -1, "holding": True, "close": 194.83, "size_fraction": 0.44},
        ]}
        print("[self-test allocation — SPY excluded as broad ETF, NVDA held→SELL]", file=sys.stderr)

    held = {}
    if args.held:
        with open(args.held) as f:
            held = json.load(f)
    exclude = {s.strip().upper() for s in args.exclude.split(",")} if args.exclude else set()

    a = allocate(briefing, cash=args.cash, deploy=args.deploy,
                 per_name_cap=args.per_name_cap, min_score=args.min_score,
                 cycle_days=args.cycle_days, cycle_start=args.cycle_start,
                 as_of=args.as_of, held=held, allow_etfs=args.allow_etfs,
                 exclude=exclude)
    if args.out:
        with open(args.out, "w") as f:
            json.dump(a, f, indent=2)
        print(f"wrote {args.out}")
    if args.json:
        print(json.dumps(a, indent=2))
    else:
        _print(a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
