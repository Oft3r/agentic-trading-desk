#!/usr/bin/env python3
"""
execution_plan.py
=================
Deterministic execution planner. The scorecard says WHAT (EXIT, RE-ENTRY...);
this script says HOW: slicing, limit pricing, and pre-trade checks — before
the human approves anything.

Pre-trade checks (each can BLOCK or WARN):
  staleness   - quote age vs threshold; MCP round-trips make stale quotes the
                #1 retail slippage source. BLOCK if too old.
  spread      - quoted spread in bps; wide spread => limit-only, never market.
  imbalance   - bid_size vs ask_size; entering against heavy opposing size
                is paying for immediacy right when it is most expensive.
  participation - order size vs average daily volume; cap at max_pov to
                avoid moving the tape.

Slicing:
  TWAP        - equal slices across `horizon_min`; slice count derived from
                order size vs ADV so small orders stay single-fill.
  VWAP-lite   - if intraday cumulative volume fractions are provided
                (`volume_curve`), slices are weighted by the U-curve instead
                of equally.

Limit pricing: passive (join near side), mid, or aggressive (cross a fraction
of the spread) depending on urgency; never a naked market order.

stdlib only. Deterministic. Input JSON:
{
  "symbol": "AAPL", "side": "buy"|"sell", "qty": 120,
  "quote": {"bid": 227.10, "ask": 227.18, "last": 227.12,
             "bid_size": 400, "ask_size": 900, "age_sec": 3.2},
  "adv": 48000000,             # average daily volume (shares), optional
  "urgency": "low"|"normal"|"high",   # default normal
  "horizon_min": 30,           # execution window, default 30
  "volume_curve": [0.12, ...]  # optional intraday volume fractions
}
"""
from __future__ import annotations
import json
import sys
from typing import Optional

STALE_BLOCK_SEC = 30.0     # quote older than this: refuse to price
STALE_WARN_SEC = 5.0       # warn and require refresh before submit
WIDE_SPREAD_BPS = 20.0     # wide spread: passive-only
IMBALANCE_WARN = 3.0       # opposing size 3x own side
MAX_POV = 0.05             # max fraction of interval volume we take


def plan(order: dict) -> dict:
    symbol = order.get("symbol", "?")
    side = order["side"].lower()
    qty = int(order["qty"])
    q = order["quote"]
    bid, ask = float(q["bid"]), float(q["ask"])
    last = float(q.get("last", (bid + ask) / 2))
    bid_sz = float(q.get("bid_size", 0)) or None
    ask_sz = float(q.get("ask_size", 0)) or None
    age = float(q.get("age_sec", 0.0))
    adv = float(order.get("adv", 0)) or None
    urgency = order.get("urgency", "normal")
    horizon = float(order.get("horizon_min", 30))
    curve = order.get("volume_curve")

    mid = (bid + ask) / 2
    spread = ask - bid
    spread_bps = spread / mid * 1e4 if mid > 0 else None

    checks: list[dict] = []
    blocked = False

    def check(name: str, status: str, detail: str):
        nonlocal blocked
        checks.append({"name": name, "status": status, "detail": detail})
        if status == "BLOCK":
            blocked = True

    # --- 1. Staleness ---
    if age >= STALE_BLOCK_SEC:
        check("staleness", "BLOCK",
              f"quote {age:.0f}s old (≥{STALE_BLOCK_SEC:.0f}s): refetch before pricing")
    elif age >= STALE_WARN_SEC:
        check("staleness", "WARN",
              f"quote {age:.1f}s old: refresh immediately before submit")
    else:
        check("staleness", "PASS", f"quote {age:.1f}s old")

    # --- 2. Spread ---
    if spread_bps is None or spread <= 0:
        check("spread", "BLOCK", "crossed/invalid quote: refetch")
    elif spread_bps >= WIDE_SPREAD_BPS:
        check("spread", "WARN",
              f"spread {spread_bps:.1f} bps (wide): passive limit only, no crossing")
    else:
        check("spread", "PASS", f"spread {spread_bps:.1f} bps")

    # --- 3. Order-book imbalance (L1 proxy) ---
    imbalance = None
    if bid_sz and ask_sz:
        imbalance = (bid_sz - ask_sz) / (bid_sz + ask_sz)
        opposing = ask_sz / bid_sz if side == "buy" else bid_sz / ask_sz
        if opposing >= IMBALANCE_WARN:
            check("imbalance", "WARN",
                  f"opposing size {opposing:.1f}× own side "
                  f"(imb {imbalance:+.2f}): expect adverse drift; work passively")
        else:
            check("imbalance", "PASS", f"L1 imbalance {imbalance:+.2f}")
    else:
        check("imbalance", "SKIP", "no L1 sizes in quote")

    # --- 4. Participation ---
    interval_vol = None
    if adv:
        interval_vol = adv * (horizon / 390.0)  # 390 min in a session
        pov = qty / interval_vol if interval_vol > 0 else None
        if pov is not None and pov > MAX_POV:
            need = qty / (MAX_POV * adv / 390.0)
            check("participation", "WARN",
                  f"order is {pov*100:.1f}% of expected {horizon:.0f}-min volume "
                  f"(cap {MAX_POV*100:.0f}%): extend horizon to ≥{need:.0f} min")
        else:
            check("participation", "PASS",
                  f"{(pov or 0)*100:.2f}% of expected interval volume")
    else:
        check("participation", "SKIP", "no ADV provided")

    # --- Limit pricing by urgency ---
    wide = spread_bps is not None and spread_bps >= WIDE_SPREAD_BPS
    if urgency == "high" and not wide:
        # cross 75% of the spread: near-certain fill, bounded cost
        px = bid + 0.75 * spread if side == "buy" else ask - 0.75 * spread
        style = "aggressive limit (crosses 75% of spread)"
    elif urgency == "low" or wide:
        px = bid if side == "buy" else ask
        style = "passive limit (joins near touch)"
    else:
        px = mid
        style = "mid-point limit"
    px = round(px, 2)
    slip_vs_mkt = ((ask - px) if side == "buy" else (px - bid)) / mid * 1e4

    # --- Slicing ---
    n_slices = 1
    if adv and interval_vol:
        # one slice per MAX_POV chunk of 5-minute volume, capped 1..8
        vol_5min = adv / 390.0 * 5.0
        n_slices = max(1, min(8, round(qty / max(vol_5min * MAX_POV, 1))))
    elif qty > 500:
        n_slices = min(8, max(2, qty // 500))
    slices = []
    if curve and len(curve) >= n_slices:
        # VWAP-lite: weight slices by provided intraday volume fractions
        w = curve[:n_slices]
        tot = sum(w) or 1.0
        alloc = [round(qty * x / tot) for x in w]
        algo = "VWAP-lite (volume-curve weighted)"
    else:
        alloc = [qty // n_slices] * n_slices
        algo = "TWAP (equal slices)"
    alloc[-1] += qty - sum(alloc)  # rounding remainder to last slice
    step = horizon / n_slices
    for i, a in enumerate(alloc):
        if a <= 0:
            continue
        slices.append({"slice": i + 1, "qty": a,
                       "at_min": round(i * step, 1),
                       "limit": px,
                       "note": "reprice from fresh quote before each slice"})

    est_cost_bps = round(slip_vs_mkt + (spread_bps or 0) / 2, 1)
    return {
        "symbol": symbol, "side": side, "qty": qty,
        "status": "BLOCKED" if blocked else "READY FOR REVIEW",
        "checks": checks,
        "quote": {"bid": bid, "ask": ask, "mid": round(mid, 4), "last": last,
                  "spread_bps": round(spread_bps, 1) if spread_bps else None,
                  "l1_imbalance": round(imbalance, 3) if imbalance is not None else None,
                  "age_sec": age},
        "pricing": {"limit": px, "style": style, "urgency": urgency,
                    "worst_case_vs_mid_bps": round(slip_vs_mkt, 1)},
        "slicing": {"algo": algo, "n_slices": len(slices),
                    "horizon_min": horizon, "slices": slices},
        "est_all_in_cost_bps": est_cost_bps,
        "reminder": ("Simulate with review_*_order and get explicit user "
                     "approval before place_*_order. Refetch quote if any "
                     "check is WARN/BLOCK."),
    }


def render(p: dict) -> str:
    L = []
    L.append(f"EXECUTION PLAN  ·  {p['side'].upper()} {p['qty']} {p['symbol']}")
    L.append("=" * 54)
    q = p["quote"]
    L.append(f"Quote   : {q['bid']:.2f} / {q['ask']:.2f}  "
             f"(mid {q['mid']:.2f}, spread {q['spread_bps']} bps, "
             f"age {q['age_sec']:.1f}s)")
    if q.get("l1_imbalance") is not None:
        L.append(f"L1 imb  : {q['l1_imbalance']:+.3f}")
    pr = p["pricing"]
    L.append(f"Limit   : {pr['limit']:.2f}  · {pr['style']}")
    L.append(f"Cost est: ≤{p['est_all_in_cost_bps']} bps all-in")
    s = p["slicing"]
    L.append(f"Slicing : {s['algo']} — {s['n_slices']} slice(s) over "
             f"{s['horizon_min']:.0f} min")
    for sl in s["slices"]:
        L.append(f"    #{sl['slice']}  {sl['qty']} sh @ {sl['limit']:.2f}  "
                 f"t+{sl['at_min']}min")
    L.append("-" * 54)
    L.append(f"STATUS  : {p['status']}")
    for c in p["checks"]:
        mark = {"PASS": "✓", "WARN": "⚠", "BLOCK": "✗", "SKIP": "·"}[c["status"]]
        L.append(f"  {mark} {c['name']:<14} {c['detail']}")
    L.append(f"\n{p['reminder']}")
    return "\n".join(L)


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="TWAP/VWAP execution planner with pre-trade checks.")
    ap.add_argument("input", nargs="?", help="JSON order spec. No file: self-test.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.input:
        with open(args.input) as f:
            order = json.load(f)
    else:
        order = {
            "symbol": "SELFTEST", "side": "buy", "qty": 1200,
            "quote": {"bid": 227.10, "ask": 227.18, "last": 227.12,
                      "bid_size": 400, "ask_size": 1400, "age_sec": 6.0},
            "adv": 2_000_000, "urgency": "normal", "horizon_min": 30,
        }
        print("[self-test order]\n", file=sys.stderr)

    p = plan(order)
    print(json.dumps(p, indent=2, ensure_ascii=False) if args.json else render(p))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
