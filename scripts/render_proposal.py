#!/usr/bin/env python3
"""
render_proposal.py
==================
Renders the action that is ABOUT TO BE EXECUTED as a self-contained HTML
card: scorecard pillars, flags, ATR risk levels, the execution plan slices
and pre-trade checks, and the explicit confirmation banner.

The card is the last thing the user sees before approving an order —
everything the decision rests on, on one screen, zero external assets.

Usage:
  python3 score.py ticker.json --json > card.json
  python3 execution_plan.py order.json --json > plan.json
  python3 render_proposal.py card.json --plan plan.json --macro macro.json -o proposal.html

stdlib only. Deterministic output for identical inputs.
"""
from __future__ import annotations
import argparse
import html
import json
import sys
from typing import Optional

CSS = """
:root{--bg:#0d1117;--panel:#161b22;--line:#30363d;--txt:#e6edf3;--dim:#8b949e;
--green:#3fb950;--red:#f85149;--amber:#d29922;--blue:#58a6ff;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;padding:24px;max-width:880px;margin:auto}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:20px;margin-bottom:16px}
h1{font-size:20px;margin-bottom:4px} h2{font-size:13px;color:var(--dim);text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px}
.banner{border-radius:8px;padding:14px 18px;font-size:17px;font-weight:700;margin-bottom:16px;border:1px solid}
.banner.exit{background:#2d1214;border-color:var(--red);color:var(--red)}
.banner.enter{background:#0f2417;border-color:var(--green);color:var(--green)}
.banner.hold{background:#1b2030;border-color:var(--blue);color:var(--blue)}
.banner.wait{background:#251d0e;border-color:var(--amber);color:var(--amber)}
.banner small{display:block;font-weight:400;font-size:13px;color:var(--txt);margin-top:4px}
.pillars{display:flex;gap:12px}
.pillar{flex:1;text-align:center;padding:10px;border:1px solid var(--line);border-radius:6px}
.pillar .val{font-size:26px;font-weight:700}
.pillar .val.pos{color:var(--green)}.pillar .val.neg{color:var(--red)}.pillar .val.zero{color:var(--dim)}
.pillar .name{color:var(--dim);font-size:12px}
.pillar .det{font-size:11px;color:var(--dim);margin-top:6px}
.total{margin-top:12px;font-size:15px}
.flag{display:block;padding:2px 0;font-size:13px}
.flag.exh{color:var(--amber)}.flag.bear{color:var(--red)}.flag.reb{color:var(--green)}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left}
th{color:var(--dim);font-weight:400;font-size:12px;text-transform:uppercase}
.r{text-align:right}
.chk-PASS{color:var(--green)}.chk-WARN{color:var(--amber)}.chk-BLOCK{color:var(--red)}.chk-SKIP{color:var(--dim)}
.confirm{background:#2d1214;border:1px solid var(--red);border-radius:8px;padding:14px 18px;text-align:center;font-weight:700;color:var(--red)}
.kv{display:flex;justify-content:space-between;padding:3px 0;font-size:13px}
.kv .k{color:var(--dim)}
.footer{color:var(--dim);font-size:11px;text-align:center;margin-top:12px}
"""


def _cls(action: str) -> str:
    if action.startswith(("EXIT", "STAY OUT")):
        return "exit"
    if action.startswith(("RE-ENTRY", "TACTICAL")):
        return "enter"
    if action.startswith("HOLD"):
        return "hold"
    return "wait"


def _pillar(name: str, score, detail: str) -> str:
    if score is None:
        v, c = "?", "zero"
    else:
        v = f"{score:+d}"
        c = "pos" if score > 0 else "neg" if score < 0 else "zero"
    return (f'<div class="pillar"><div class="val {c}">{v}</div>'
            f'<div class="name">{html.escape(name)}</div>'
            f'<div class="det">{html.escape(detail or "")}</div></div>')


def build(card: dict, plan: Optional[dict] = None,
          macro: Optional[dict] = None) -> str:
    d = card["decision"]
    p = card["pillars"]
    action = d["action"]
    sym = card.get("symbol") or "?"
    B = []
    B.append(f"<h1>{html.escape(sym)}</h1>")
    B.append(f'<div style="color:var(--dim);margin-bottom:14px">'
             f'{card["n_bars"]} bars · pillar total '
             f'<b>{card["pillar_total"]:+d}</b> / ±6</div>')

    B.append(f'<div class="banner {_cls(action)}">► {html.escape(action)}'
             f'<small>{html.escape(d["rationale"])} {html.escape(d["framing"])}</small></div>')

    B.append('<div class="card"><h2>Three Pillars</h2><div class="pillars">')
    B.append(_pillar("Trend", p["trend"]["score"], p["trend"]["detail"]))
    B.append(_pillar("Momentum", p["momentum"]["score"], p["momentum"]["detail"]))
    B.append(_pillar("Macro-Sentiment", p["macro_sentiment"]["score"],
                     (macro or {}).get("regime", "") or p["macro_sentiment"]["detail"]))
    B.append("</div>")
    f = d["flags"]
    if f["exhaustion"] or f["bearish"] or f["rebound"] or f.get("death_cross"):
        B.append('<div style="margin-top:12px">')
        for x in f["exhaustion"]:
            B.append(f'<span class="flag exh">⚠ exhaustion — {html.escape(x)}</span>')
        for x in f["bearish"]:
            B.append(f'<span class="flag bear">▼ bearish — {html.escape(x)}</span>')
        for x in f["rebound"]:
            B.append(f'<span class="flag reb">▲ rebound — {html.escape(x)}</span>')
        if f.get("death_cross"):
            B.append('<span class="flag bear">✖ structure — active death-cross</span>')
        B.append("</div>")
    B.append("</div>")

    r = card.get("risk")
    if r:
        B.append('<div class="card"><h2>Risk (ATR / vol-target)</h2>')
        rows = [
            ("Suggested stop", f'{r["suggested_stop"]:.2f} ({r["stop_distance_pct"]}% away, {r["stop_type"]})'
             if r.get("suggested_stop") else "n/a"),
            ("ATR-14", f'{r["atr"]:.2f} ({r["atr_pct"]:.2f}%)' if r.get("atr") else "n/a"),
            ("Vol-target size", f'{r["vol_target_fraction"]*100:.0f}% of sleeve'
             if r.get("vol_target_fraction") is not None else "n/a"),
            ("Forecast vol (ann.)", f'{r["forecast_vol_annual"]*100:.1f}%'
             if r.get("forecast_vol_annual") else "n/a"),
            ("20d z-score", f'{r["zscore_20"]:+.2f}σ' if r.get("zscore_20") is not None else "n/a"),
        ]
        if r.get("vol_ratio") is not None:
            rows.append(("GARCH vol ratio",
                         f'{r["vol_ratio"]:.2f}× long-run'
                         + (" ⚠ expanding" if r["vol_ratio"] >= 1.3 else "")))
        for k, v in rows:
            B.append(f'<div class="kv"><span class="k">{k}</span><span>{html.escape(str(v))}</span></div>')
        B.append("</div>")

    if macro:
        B.append('<div class="card"><h2>Macro Regime</h2>')
        B.append(f'<div class="kv"><span class="k">Regime</span><span>{html.escape(macro.get("regime",""))}</span></div>')
        B.append(f'<div class="kv"><span class="k">Composite</span><span>{macro.get("composite",0):+.3f}</span></div>')
        B.append(f'<div class="kv"><span class="k">Pillar</span><span>{macro.get("pillar_score",0):+d} · {html.escape(macro.get("pillar_label",""))}</span></div>')
        sr = macro.get("stat_regime")
        if sr:
            B.append(f'<div class="kv"><span class="k">Vol regime (GMM/HMM)</span>'
                     f'<span>{html.escape(sr["state"].upper())} · p_turb {sr["p_turbulent"]:.2f} · {sr["bars_in_state"]} bars</span></div>')
        B.append("</div>")

    if plan:
        st = plan["status"]
        col = "var(--red)" if st == "BLOCKED" else "var(--green)"
        B.append('<div class="card"><h2>Execution Plan — pending your approval</h2>')
        B.append(f'<div style="font-size:16px;font-weight:700;margin-bottom:8px">'
                 f'{plan["side"].upper()} {plan["qty"]} {html.escape(plan["symbol"])}'
                 f' — <span style="color:{col}">{st}</span></div>')
        q = plan["quote"]
        B.append(f'<div class="kv"><span class="k">Quote</span><span>'
                 f'{q["bid"]:.2f} / {q["ask"]:.2f} · spread {q["spread_bps"]} bps · age {q["age_sec"]:.1f}s</span></div>')
        pr = plan["pricing"]
        B.append(f'<div class="kv"><span class="k">Limit</span><span>{pr["limit"]:.2f} · {html.escape(pr["style"])}</span></div>')
        B.append(f'<div class="kv"><span class="k">Est. all-in cost</span><span>≤{plan["est_all_in_cost_bps"]} bps</span></div>')
        s = plan["slicing"]
        B.append(f'<table style="margin-top:10px"><tr><th>#</th><th class="r">Qty</th>'
                 f'<th class="r">Limit</th><th class="r">t+min</th></tr>')
        for sl in s["slices"]:
            B.append(f'<tr><td>{sl["slice"]}</td><td class="r">{sl["qty"]}</td>'
                     f'<td class="r">{sl["limit"]:.2f}</td><td class="r">{sl["at_min"]}</td></tr>')
        B.append("</table>")
        B.append(f'<div style="color:var(--dim);font-size:12px;margin-top:6px">{html.escape(s["algo"])} over {s["horizon_min"]:.0f} min</div>')
        B.append('<table style="margin-top:10px"><tr><th>Check</th><th>Status</th><th>Detail</th></tr>')
        for c in plan["checks"]:
            B.append(f'<tr><td>{html.escape(c["name"])}</td>'
                     f'<td class="chk-{c["status"]}">{c["status"]}</td>'
                     f'<td>{html.escape(c["detail"])}</td></tr>')
        B.append("</table></div>")

    B.append('<div class="confirm">⛔ NOTHING EXECUTES WITHOUT YOUR EXPLICIT CONFIRMATION — '
             'review_*_order simulation first, then approve.</div>')
    B.append('<div class="footer">agentic-trading-desk · deterministic scripts, human-approved execution</div>')

    return ("<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{html.escape(sym)} — proposal</title><style>{CSS}</style></head>"
            f"<body>{''.join(B)}</body></html>")


def main() -> int:
    ap = argparse.ArgumentParser(description="HTML proposal card (scorecard + execution plan).")
    ap.add_argument("card", nargs="?", help="scorecard JSON from score.py --json. No file: self-test.")
    ap.add_argument("--plan", help="execution plan JSON from execution_plan.py --json")
    ap.add_argument("--macro", help="macro JSON from macro_pillar.py --json")
    ap.add_argument("-o", "--out", default="proposal.html")
    args = ap.parse_args()

    if args.card:
        with open(args.card) as f:
            card = json.load(f)
    else:
        import math
        import score as S
        close = [round(100 + i * 0.25 + 6 * math.sin(i / 12), 2) for i in range(260)]
        close += [close[-1] * 1.05, close[-1] * 1.10]
        card = S.score_symbol(close, macro_score=1, symbol="SELFTEST", holding=True)
        print("[self-test scorecard]", file=sys.stderr)

    plan = macro = None
    if args.plan:
        with open(args.plan) as f:
            plan = json.load(f)
    elif not args.card:
        import execution_plan as E
        plan = E.plan({"symbol": "SELFTEST", "side": "sell", "qty": 800,
                       "quote": {"bid": 183.10, "ask": 183.18, "last": 183.12,
                                 "bid_size": 900, "ask_size": 400, "age_sec": 2.0},
                       "adv": 1_500_000, "urgency": "normal", "horizon_min": 20})
    if args.macro:
        with open(args.macro) as f:
            macro = json.load(f)

    with open(args.out, "w") as f:
        f.write(build(card, plan, macro))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
