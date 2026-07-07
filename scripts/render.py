#!/usr/bin/env python3
"""
render.py — unified HTML view layer for the trading desk skill
===============================================================
Every thing the skill *does*, Claude can *show*. This module turns the
JSON output of the deterministic scripts into a self-contained HTML page
(zero external assets, works from file://) so the user can see the
reasoning pictorially and — for orders — APPROVE via an HTML form.

Subcommands
-----------
  proposal   single-ticker buy/sell advice: pillars, risk, EXECUTION
             TIMELINE (SVG), pre-trade checks, and an APPROVAL FORM.
             python3 render.py proposal card.json --plan plan.json \
                     --macro macro.json -o proposal.html
  scanner    multi-ticker grid: pillar bars, action badge, sparkline, risk.
             python3 render.py scanner scan.json -o scanner.html
  portfolio  positions with allocation bars, P&L, protected badges.
             python3 render.py portfolio port.json -o portfolio.html
  backtest   equity curve vs buy&hold + walk-forward + sensitivity grid.
             python3 render.py backtest bt.json -o backtest.html

Run any subcommand with NO input file to render a self-test sample.

Approval bridge (no server): the form does not POST anywhere. On submit it
assembles a confirmation line + token and copies it to the clipboard. The
user pastes that line back to Claude, who then runs review_*_order
(simulation) and, only on a passing review, place_*_order.

stdlib only. Deterministic output for identical inputs.
"""
from __future__ import annotations
import argparse
import hashlib
import html
import json
import math
import re
import sys
from typing import Optional, Sequence

# ── Shared theme ──────────────────────────────────────────────────────────
CSS = """
:root{--bg:#0d1117;--panel:#161b22;--panel2:#1c2230;--line:#30363d;--txt:#e6edf3;
--dim:#8b949e;--green:#3fb950;--red:#f85149;--amber:#d29922;--blue:#58a6ff;
--purple:#bc8cff;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;padding:24px;max-width:1080px;margin:auto}
h1{font-size:20px;margin-bottom:4px}
h2{font-size:12px;color:var(--dim);text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px}
.sub{color:var(--dim);margin-bottom:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:18px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}
.banner{border-radius:8px;padding:14px 18px;font-size:17px;font-weight:700;margin-bottom:16px;border:1px solid}
.banner.exit{background:#2d1214;border-color:var(--red);color:var(--red)}
.banner.enter{background:#0f2417;border-color:var(--green);color:var(--green)}
.banner.hold{background:#1b2030;border-color:var(--blue);color:var(--blue)}
.banner.wait{background:#251d0e;border-color:var(--amber);color:var(--amber)}
.banner small{display:block;font-weight:400;font-size:13px;color:var(--txt);margin-top:4px}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;border:1px solid}
.b-exit{background:#2d1214;border-color:var(--red);color:var(--red)}
.b-enter{background:#0f2417;border-color:var(--green);color:var(--green)}
.b-hold{background:#1b2030;border-color:var(--blue);color:var(--blue)}
.b-wait{background:#251d0e;border-color:var(--amber);color:var(--amber)}
.b-prot{background:#1e1533;border-color:var(--purple);color:var(--purple)}
.pillars{display:flex;gap:12px}
.pillar{flex:1;text-align:center;padding:10px;border:1px solid var(--line);border-radius:6px}
.pillar .val{font-size:26px;font-weight:700}
.pillar .val.pos{color:var(--green)}.pillar .val.neg{color:var(--red)}.pillar .val.zero{color:var(--dim)}
.pillar .name{color:var(--dim);font-size:12px}
.pillar .det{font-size:11px;color:var(--dim);margin-top:6px}
.flag{display:block;padding:2px 0;font-size:13px}
.flag.exh{color:var(--amber)}.flag.bear{color:var(--red)}.flag.reb{color:var(--green)}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left}
th{color:var(--dim);font-weight:400;font-size:12px;text-transform:uppercase}
.r{text-align:right}
.chk-PASS{color:var(--green)}.chk-WARN{color:var(--amber)}.chk-BLOCK{color:var(--red)}.chk-SKIP{color:var(--dim)}
.kv{display:flex;justify-content:space-between;padding:3px 0;font-size:13px}
.kv .k{color:var(--dim)}
.pos{color:var(--green)}.neg{color:var(--red)}.zero{color:var(--dim)}
.footer{color:var(--dim);font-size:11px;text-align:center;margin-top:12px}
/* diverging pillar bar */
.pbar{position:relative;height:14px;background:var(--panel2);border-radius:3px;margin:4px 0}
.pbar .mid{position:absolute;left:50%;top:0;bottom:0;width:1px;background:var(--line)}
.pbar .fill{position:absolute;top:1px;bottom:1px;border-radius:2px}
.pbar .fill.pos{background:var(--green);left:50%}
.pbar .fill.neg{background:var(--red);right:50%}
.plabel{display:flex;justify-content:space-between;font-size:11px;color:var(--dim)}
/* allocation bar */
.abar{height:16px;background:var(--panel2);border-radius:3px;overflow:hidden}
.abar>span{display:block;height:100%;background:var(--blue)}
/* scanner card */
.scard{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px}
.scard .top{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px}
.scard .sym{font-size:16px;font-weight:700}
.scard .px{color:var(--dim);font-size:13px}
.chips{margin-top:8px}
.chip{display:inline-block;background:var(--panel2);border:1px solid var(--line);border-radius:4px;padding:1px 6px;font-size:11px;color:var(--dim);margin:2px 4px 0 0}
/* approval form */
.approve{background:#0f1a12;border:1px solid var(--green);border-radius:8px;padding:16px 18px;margin-bottom:16px}
.approve label{display:block;font-size:12px;color:var(--dim);margin:8px 0 3px}
.approve input,.approve select{background:#0d1117;color:var(--txt);border:1px solid var(--line);border-radius:5px;padding:7px 9px;font:inherit;width:100%}
.approve .row{display:flex;gap:12px}.approve .row>div{flex:1}
.btnrow{display:flex;gap:10px;margin-top:14px}
button{font:inherit;font-weight:700;border-radius:6px;padding:10px 16px;border:1px solid;cursor:pointer}
.bapprove{background:#0f2417;border-color:var(--green);color:var(--green)}
.breject{background:#2d1214;border-color:var(--red);color:var(--red)}
.confirm{background:#2d1214;border:1px solid var(--red);border-radius:8px;padding:14px 18px;text-align:center;font-weight:700;color:var(--red)}
#out{display:none;margin-top:14px;background:#0d1117;border:1px dashed var(--green);border-radius:6px;padding:12px}
#out.show{display:block}
#outline{font-size:14px;font-weight:700;word-break:break-word;white-space:pre-wrap}
.hint{color:var(--dim);font-size:11px;margin-top:6px}
"""

E = html.escape


def _num(x, dflt=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return dflt


def _cls(action: str) -> str:
    a = (action or "").upper()
    if a.startswith(("EXIT", "STAY OUT", "AVOID")):
        return "exit"
    if a.startswith(("RE-ENTRY", "TACTICAL", "BUY")):
        return "enter"
    if a.startswith("HOLD"):
        return "hold"
    if "EXIT" in a or "TRIM" in a or "SELL" in a:
        return "exit"
    return "wait"


def _page(title: str, body: str, extra_head: str = "") -> str:
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{E(title)}</title><style>{CSS}</style>{extra_head}</head>"
            f"<body>{body}</body></html>")


# ── Reusable pictorial components (pure SVG / CSS, deterministic) ──────────

def sparkline(vals: Sequence[float], w: int = 130, h: int = 34) -> str:
    pts = [v for v in (_num(x) for x in (vals or [])) if v is not None]
    if len(pts) < 2:
        return ""
    lo, hi = min(pts), max(pts)
    rng = (hi - lo) or 1.0
    n = len(pts)
    coords = []
    for i, v in enumerate(pts):
        x = round(i / (n - 1) * (w - 2) + 1, 2)
        y = round(h - 1 - (v - lo) / rng * (h - 2), 2)
        coords.append(f"{x},{y}")
    up = pts[-1] >= pts[0]
    col = "var(--green)" if up else "var(--red)"
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
            f'<polyline fill="none" stroke="{col}" stroke-width="1.5" '
            f'points="{" ".join(coords)}"/></svg>')


def pillar_bar(name: str, score, detail: str = "") -> str:
    s = int(score) if score is not None else 0
    frac = min(abs(s) / 2.0, 1.0) * 50.0  # % of half-width
    if s > 0:
        fill = f'<span class="fill pos" style="width:{frac}%"></span>'
    elif s < 0:
        fill = f'<span class="fill neg" style="width:{frac}%"></span>'
    else:
        fill = ""
    val = f"{s:+d}" if score is not None else "?"
    c = "pos" if s > 0 else "neg" if s < 0 else "zero"
    return (f'<div style="margin-bottom:6px"><div class="plabel">'
            f'<span>{E(name)}</span><span class="{c}">{val}</span></div>'
            f'<div class="pbar"><div class="mid"></div>{fill}</div>'
            + (f'<div style="font-size:11px;color:var(--dim)">{E(detail)}</div>' if detail else "")
            + "</div>")


def execution_timeline(plan: dict) -> str:
    """SVG: qty-vs-time schedule + a bid/limit/ask price gauge."""
    s = plan.get("slicing", {})
    slices = s.get("slices", [])
    if not slices:
        return ""
    W, H, pad = 760, 150, 34
    horizon = _num(s.get("horizon_min")) or max((_num(x.get("at_min")) or 0) for x in slices) or 1
    maxq = max((_num(x.get("qty")) or 0) for x in slices) or 1
    plot_w, plot_h = W - 2 * pad, H - 2 * pad
    bw = max(6, min(46, plot_w / (len(slices) * 1.6)))
    bars = []
    for sl in slices:
        at = _num(sl.get("at_min")) or 0
        q = _num(sl.get("qty")) or 0
        x = pad + (at / horizon) * plot_w if horizon else pad
        bh = (q / maxq) * plot_h
        y = pad + plot_h - bh
        bars.append(
            f'<rect x="{x - bw/2:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{bh:.1f}" '
            f'rx="2" fill="var(--blue)" opacity="0.85"><title>slice {E(str(sl.get("slice","")))}: '
            f'{int(q)} @ {_num(sl.get("limit")) or 0:.2f} at t+{at:g}m</title></rect>'
            f'<text x="{x:.1f}" y="{y - 4:.1f}" fill="var(--dim)" font-size="10" '
            f'text-anchor="middle">{int(q)}</text>'
            f'<text x="{x:.1f}" y="{pad + plot_h + 13:.1f}" fill="var(--dim)" font-size="10" '
            f'text-anchor="middle">{at:g}m</text>')
    axis = (f'<line x1="{pad}" y1="{pad+plot_h}" x2="{W-pad}" y2="{pad+plot_h}" '
            f'stroke="var(--line)"/>')
    schedule = (f'<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" style="max-width:100%">'
                f'{axis}{"".join(bars)}'
                f'<text x="{pad}" y="16" fill="var(--dim)" font-size="11">qty per slice '
                f'({E(s.get("algo",""))})</text></svg>')

    # price gauge: bid ── limit ── ask
    q = plan.get("quote", {})
    bid, ask = _num(q.get("bid")), _num(q.get("ask"))
    limit = _num(plan.get("pricing", {}).get("limit"))
    gauge = ""
    if bid is not None and ask is not None and ask > bid:
        gw = 640
        def gx(p):
            return round((p - bid) / (ask - bid) * gw, 1)
        lx = gx(limit) if limit is not None else gw / 2
        lx = max(0, min(gw, lx))
        gauge = (
            f'<svg width="{gw+40}" height="52" viewBox="0 0 {gw+40} 52" style="max-width:100%">'
            f'<line x1="20" y1="26" x2="{gw+20}" y2="26" stroke="var(--line)" stroke-width="2"/>'
            f'<circle cx="20" cy="26" r="4" fill="var(--red)"/>'
            f'<circle cx="{gw+20}" cy="26" r="4" fill="var(--green)"/>'
            f'<line x1="{lx+20}" y1="12" x2="{lx+20}" y2="40" stroke="var(--amber)" stroke-width="2"/>'
            f'<text x="20" y="14" fill="var(--red)" font-size="10">bid {bid:.2f}</text>'
            f'<text x="{gw+20}" y="14" fill="var(--green)" font-size="10" text-anchor="end">ask {ask:.2f}</text>'
            + (f'<text x="{lx+20}" y="50" fill="var(--amber)" font-size="10" text-anchor="middle">limit {limit:.2f}</text>'
               if limit is not None else "")
            + '</svg>')
    return f'<div class="card"><h2>How this executes</h2>{schedule}{gauge}</div>'


def order_token(symbol: str, side: str, qty, limit) -> str:
    raw = f"{symbol}|{side}|{qty}|{limit}"
    return "PROP-" + hashlib.sha1(raw.encode()).hexdigest()[:8].upper()


# ── proposal ───────────────────────────────────────────────────────────────

def build_proposal(card: dict, plan: Optional[dict] = None,
                   macro: Optional[dict] = None) -> str:
    d = card.get("decision", {})
    p = card.get("pillars", {})
    action = d.get("action", "OBSERVE")
    sym = card.get("symbol") or "?"
    B = [f"<h1>{E(sym)}</h1>",
         f'<div class="sub">{E(str(card.get("n_bars","?")))} bars · pillar total '
         f'<b>{card.get("pillar_total",0):+d}</b> / ±6</div>',
         f'<div class="banner {_cls(action)}">► {E(action)}'
         f'<small>{E(d.get("rationale",""))} {E(d.get("framing",""))}</small></div>']

    # pillars (numeric cards + bars)
    B.append('<div class="card"><h2>Three Pillars</h2><div class="pillars">')
    for key, nm in (("trend", "Trend"), ("momentum", "Momentum"),
                    ("macro_sentiment", "Macro")):
        blk = p.get(key, {})
        sc = blk.get("score")
        v = f"{sc:+d}" if sc is not None else "?"
        c = "pos" if (sc or 0) > 0 else "neg" if (sc or 0) < 0 else "zero"
        B.append(f'<div class="pillar"><div class="val {c}">{v}</div>'
                 f'<div class="name">{nm}</div>'
                 f'<div class="det">{E(blk.get("detail",""))}</div></div>')
    B.append("</div>")
    f = d.get("flags", {})
    if any(f.get(k) for k in ("exhaustion", "bearish", "rebound", "death_cross")):
        B.append('<div style="margin-top:12px">')
        for x in f.get("exhaustion", []):
            B.append(f'<span class="flag exh">⚠ exhaustion — {E(x)}</span>')
        for x in f.get("bearish", []):
            B.append(f'<span class="flag bear">▼ bearish — {E(x)}</span>')
        for x in f.get("rebound", []):
            B.append(f'<span class="flag reb">▲ rebound — {E(x)}</span>')
        if f.get("death_cross"):
            B.append('<span class="flag bear">✖ structure — active death-cross</span>')
        B.append("</div>")
    B.append("</div>")

    ev = card.get("event_risk")
    if ev:
        du = int(_num(ev.get("days_until"), 0) or 0)
        when = "TODAY" if du == 0 else "tomorrow" if du == 1 else f"in {du} days"
        vf = "" if ev.get("verified") else " · tentative"
        tm = E((ev.get("timing") or "").upper())
        warn = ev.get("within_warn")
        bg, bd, ic = ("#3a2a10", "#b8860b", "⚠") if warn else ("#111725", "#2b3446", "🗓")
        note = (" — event risk the technical score can't price; size down or wait for the print"
                if warn else "")
        B.append(
            f'<div class="card" style="background:{bg};border-color:{bd}">'
            f'<h2>{ic} Earnings {when}</h2>'
            f'<div><b>{E(ev.get("next_date",""))}</b> {tm}{vf}{note}</div></div>')

    r = card.get("risk")
    if r:
        B.append('<div class="card"><h2>Risk (ATR / vol-target)</h2>')
        rows = []
        if r.get("suggested_stop"):
            rows.append(("Suggested stop",
                         f'{r["suggested_stop"]:.2f} ({r.get("stop_distance_pct","?")}% away, {r.get("stop_type","")})'))
        if r.get("atr"):
            rows.append(("ATR-14", f'{r["atr"]:.2f} ({r.get("atr_pct",0):.2f}%)'))
        if r.get("vol_target_fraction") is not None:
            rows.append(("Vol-target size", f'{r["vol_target_fraction"]*100:.0f}% of sleeve'))
        if r.get("forecast_vol_annual"):
            rows.append(("Forecast vol (ann.)", f'{r["forecast_vol_annual"]*100:.1f}%'))
        if r.get("zscore_20") is not None:
            rows.append(("20d z-score", f'{r["zscore_20"]:+.2f}σ'))
        if r.get("vol_ratio") is not None:
            rows.append(("GARCH vol ratio", f'{r["vol_ratio"]:.2f}× long-run'
                         + (" ⚠ expanding" if r["vol_ratio"] >= 1.3 else "")))
        for k, v in rows:
            B.append(f'<div class="kv"><span class="k">{k}</span><span>{E(str(v))}</span></div>')
        B.append("</div>")

    if macro:
        B.append('<div class="card"><h2>Macro Regime</h2>')
        B.append(f'<div class="kv"><span class="k">Regime</span><span>{E(macro.get("regime",""))}</span></div>')
        B.append(f'<div class="kv"><span class="k">Pillar</span><span>{macro.get("pillar_score",0):+d} · {E(macro.get("pillar_label",""))}</span></div>')
        sr = macro.get("stat_regime")
        if sr:
            B.append(f'<div class="kv"><span class="k">Vol regime (GMM/HMM)</span>'
                     f'<span>{E(sr.get("state","").upper())} · p_turb {sr.get("p_turbulent",0):.2f}</span></div>')
        B.append("</div>")

    approval = ""
    if plan:
        B.append(execution_timeline(plan))
        st = plan.get("status", "")
        col = "var(--red)" if st == "BLOCKED" else "var(--green)"
        B.append('<div class="card"><h2>Execution Plan</h2>')
        B.append(f'<div style="font-size:16px;font-weight:700;margin-bottom:8px">'
                 f'{E(str(plan.get("side","")).upper())} {E(str(plan.get("qty","")))} {E(plan.get("symbol",""))}'
                 f' — <span style="color:{col}">{E(st)}</span></div>')
        q = plan.get("quote", {})
        B.append(f'<div class="kv"><span class="k">Quote</span><span>'
                 f'{_num(q.get("bid"),0):.2f} / {_num(q.get("ask"),0):.2f} · spread '
                 f'{q.get("spread_bps","?")} bps · age {_num(q.get("age_sec"),0):.1f}s</span></div>')
        pr = plan.get("pricing", {})
        B.append(f'<div class="kv"><span class="k">Limit</span><span>{_num(pr.get("limit"),0):.2f} · {E(pr.get("style",""))}</span></div>')
        B.append(f'<div class="kv"><span class="k">Est. all-in cost</span><span>≤{E(str(plan.get("est_all_in_cost_bps","?")))} bps</span></div>')
        B.append('<table style="margin-top:10px"><tr><th>Check</th><th>Status</th><th>Detail</th></tr>')
        for c in plan.get("checks", []):
            B.append(f'<tr><td>{E(c.get("name",""))}</td>'
                     f'<td class="chk-{E(c.get("status",""))}">{E(c.get("status",""))}</td>'
                     f'<td>{E(c.get("detail",""))}</td></tr>')
        B.append("</table></div>")

        # ── Approval form (works from file://) ──
        if st != "BLOCKED":
            # Sanitize identity fields to a fixed charset so the pasted-back
            # confirmation line cannot be hijacked (no newlines / injected
            # order text) via a tampered symbol/side. qty/limit are numeric.
            safe_side = re.sub(r"[^A-Z]", "", str(plan.get("side", "")).upper()) or "?"
            safe_sym = re.sub(r"[^A-Z0-9.\-]", "", str(sym).upper()) or "?"
            limit = _num(pr.get("limit"), 0)
            qty = plan.get("qty", "")
            tok = order_token(safe_sym, safe_side, qty, limit)
            approval = f'''
<div class="approve">
  <h2 style="color:var(--green)">Your decision</h2>
  <div class="row">
    <div><label>Side</label><input id="side" value="{E(safe_side)}" readonly></div>
    <div><label>Symbol</label><input id="sym" value="{E(safe_sym)}" readonly></div>
    <div><label>Qty</label><input id="qty" type="number" min="0" step="1" value="{E(str(qty))}"></div>
    <div><label>Limit</label><input id="limit" type="number" min="0" step="0.01" value="{limit:.2f}"></div>
  </div>
  <div class="btnrow">
    <button class="bapprove" id="btnA">✓ APPROVE</button>
    <button class="breject" id="btnR">✗ REJECT</button>
  </div>
  <div id="out"><div id="outline"></div>
    <div class="hint">Copied to clipboard. Paste this line back to Claude to proceed
    (Claude runs review_*_order simulation first, then place_*_order only if it passes).</div>
  </div>
  <div class="hint">Token {E(tok)} · nothing executes until you paste an APPROVE line back to Claude.</div>
</div>
<script>
(function(){{
  // Identity fields are fixed, server-sanitized constants — never read the
  // raw DOM value for side/symbol (readonly). qty/limit are coerced numeric.
  var TOK={json.dumps(tok)}, SIDE={json.dumps(safe_side)}, SYM={json.dumps(safe_sym)};
  function emit(decision){{
    var qty=Math.max(0,Math.floor(Number(document.getElementById('qty').value)||0));
    var ln=Number(document.getElementById('limit').value);
    var lim=(isFinite(ln)&&ln>=0)?ln.toFixed(2):'0.00';
    var line = decision==='APPROVE'
      ? ('✅ APPROVE '+SIDE+' '+qty+' '+SYM+' @ limit '+lim+' [token '+TOK+']')
      : ('❌ REJECT '+SYM+' [token '+TOK+']');
    var o=document.getElementById('out');
    document.getElementById('outline').textContent=line;
    o.classList.add('show');
    if(navigator.clipboard){{ navigator.clipboard.writeText(line).catch(function(){{}}); }}
  }}
  document.getElementById('btnA').addEventListener('click',function(){{emit('APPROVE');}});
  document.getElementById('btnR').addEventListener('click',function(){{emit('REJECT');}});
}})();
</script>'''

    B.append(approval or ('<div class="confirm">⛔ NOTHING EXECUTES WITHOUT YOUR EXPLICIT '
                          'CONFIRMATION — review_*_order simulation first, then approve.</div>'))
    B.append('<div class="footer">agentic-trading-desk · deterministic scripts, human-approved execution</div>')
    return _page(f"{sym} — proposal", "".join(B))


# ── allocate (30-day budget book) ────────────────────────────────────────────

def build_allocate(a: dict) -> str:
    """Read-only dashboard for allocate.py's 30-day budget book. No approval
    form here — each BUY/SELL gets its own proposal card (review_*_order first)."""
    acct, cyc, s = a.get("account", {}), a.get("cycle", {}), a.get("summary", {})
    prm, dly = a.get("params", {}), a.get("daily", {})
    budget = _num(acct.get("budget"), 0) or 0
    B = [f'<h1>Agentic — 30-day budget book</h1>',
         f'<div class="sub">{E(a.get("as_of",""))} · ${budget:.0f} monthly budget · '
         f'cycle #{cyc.get("cycle_index",0)} · day {cyc.get("day_in_cycle","?")}/'
         f'{cyc.get("cycle_days","?")} · {cyc.get("days_remaining","?")} days left'
         + ('  ·  <b>REFRESH DAY</b>' if cyc.get("refresh_today") else '') + '</div>']

    # Daily budget + spill line
    if dly:
        src = "your input" if dly.get("input_budget") is not None else "paced"
        spill = ""
        if dly.get("strong_signal") and _num(dly.get("spilled"), 0):
            spill = (f' · <b style="color:var(--amber)">+${_num(dly.get("spilled"),0):.2f} SPILL</b>'
                     f' (strong: top score {int(_num(dly.get("top_score"),0)):+d}'
                     f'≥{int(_num(dly.get("spill_score"),0)):+d})')
        paused = dly.get("earnings_paused") or []
        prow = ""
        if paused:
            pd = ", ".join(f'{E(p.get("symbol",""))} ({int(_num(p.get("earnings_days"),0) or 0)}d)'
                           for p in paused)
            pdays = int(_num(prm.get("earnings_pause_days"), 0) or 0)
            prow = (f'<div class="kv"><span class="k">⚠ DCA paused (≤{pdays}d to earnings)</span>'
                    f'<span style="color:var(--amber)">{pd}</span></div>')
        B.append(f'<div class="card"><h2>Today\'s budget</h2>'
                 f'<div class="kv"><span class="k">Daily budget ({E(src)})</span>'
                 f'<span>${_num(dly.get("effective_budget"),0):.2f}</span></div>'
                 f'<div class="kv"><span class="k">Monthly remaining</span>'
                 f'<span>${_num(dly.get("monthly_remaining"),0):.2f}</span></div>'
                 f'<div class="kv"><span class="k">Deploy today</span>'
                 f'<span><b>${_num(dly.get("deploy_today"),0):.2f}</b>{spill}</span></div>'
                 f'{prow}</div>')

    banner = (f'Deploy target {s.get("deployed_target_pct",0):.0f}% · invested '
              f'${s.get("invested_so_far",0):.2f} · <b>deploy today ${s.get("buy_today_dollars",0):.2f}</b>'
              f' · {s.get("funded",0)} funded · {s.get("sells",0)} to sell')
    B.append(f'<div class="banner hold">► 30-DAY PACING<small>{banner}'
             + (f' · cycle {E(cyc.get("cycle_start",""))} → {E(cyc.get("cycle_end",""))}'
                if cyc.get("cycle_start") else '') + '</small></div>')

    # Buys table with target-weight allocation bars + paced tranche
    B.append('<div class="card"><h2>Targets & today\'s tranche</h2>')
    B.append('<table><tr><th>Symbol</th><th>Score</th><th>Target</th>'
             '<th class="r">Target $</th><th class="r">Held $</th>'
             '<th class="r">Buy today $</th><th class="r">Shares</th><th>Action</th></tr>')
    for b in a.get("buys", []):
        w = _num(b.get("target_weight_pct"), 0) or 0
        sc = int(_num(b.get("score"), 0) or 0)
        scc = "pos" if sc > 0 else "neg" if sc < 0 else "zero"
        vhint = ""
        if b.get("forecast_vol"):
            vm = _num(b.get("vol_mult"), 1.0)
            vhint = (f' <span class="hint">· σ̂ {_num(b.get("forecast_vol"),0)*100:.0f}%'
                     f' · {vm:.2f}×</span>')
        ern = ""
        if b.get("earnings_days") is not None:
            edays = int(_num(b.get("earnings_days"), 0) or 0)
            if b.get("earnings_paused"):
                ern = (f' <span class="badge" style="background:#3a2a10;color:#e0b34a">'
                       f'⚠ ERN {edays}d — DCA paused</span>')
            else:
                ern = f' <span class="hint">🗓 ern {edays}d</span>'
        B.append(
            f'<tr><td><b>{E(b.get("symbol",""))}</b></td>'
            f'<td class="{scc}">{sc:+d}</td>'
            f'<td><div class="abar"><span style="width:{min(w,100):.1f}%"></span></div>'
            f'<span class="hint">{w:.1f}%</span>{vhint}</td>'
            f'<td class="r">{_num(b.get("target_dollars"),0):.2f}</td>'
            f'<td class="r">{_num(b.get("current_value"),0):.2f}</td>'
            f'<td class="r"><b>{_num(b.get("buy_today_dollars"),0):.2f}</b></td>'
            f'<td class="r">{_num(b.get("buy_today_shares"),0):.4f}</td>'
            f'<td><span class="badge b-enter">{E(b.get("side",""))}</span> {E(b.get("action",""))}{ern}</td></tr>')
    B.append('</table></div>')

    sells = a.get("sells", [])
    if sells:
        B.append('<div class="card"><h2>Rotate out (sell to cash)</h2>')
        B.append('<table><tr><th>Symbol</th><th>Score</th><th class="r">Held $</th>'
                 '<th class="r">Shares</th><th>Reason</th></tr>')
        for x in sells:
            sh = x.get("sell_today_shares")
            sh_cell = f'{sh:.4f}' if sh is not None else '—'
            B.append(
                f'<tr><td><b>{E(x.get("symbol",""))}</b></td>'
                f'<td class="neg">{int(_num(x.get("score"),0) or 0):+d}</td>'
                f'<td class="r">{_num(x.get("current_value"),0):.2f}</td>'
                f'<td class="r">{sh_cell}</td>'
                f'<td>{E(x.get("reason",""))}</td></tr>')
        B.append('</table></div>')

    if prm.get("excluded_etfs"):
        B.append(f'<div class="card"><h2>Excluded (broad-index ETFs)</h2>'
                 f'<div class="sub" style="margin:0">{E(", ".join(prm["excluded_etfs"]))} '
                 f'— single-name rotation only (per-name cap {prm.get("per_name_cap_pct",0):.0f}%)</div></div>')

    B.append('<div class="confirm">⛔ OVERVIEW ONLY — each BUY/SELL needs its own '
             'proposal card: review_equity_order (simulation) → your APPROVE → place_equity_order.</div>')
    B.append('<div class="footer">agentic-trading-desk · 30-day paced budget · human-approved execution</div>')
    return _page("Agentic — 30-day budget book", "".join(B))


# ── scanner ─────────────────────────────────────────────────────────────────

def build_scanner(data: dict) -> str:
    rows = data.get("rows") or data.get("signals") or []
    macro = data.get("macro") or {}
    B = ['<h1>Scanner</h1>',
         f'<div class="sub">{E(data.get("as_of",""))} · {len(rows)} tickers'
         + (f' · macro {macro.get("pillar_score",0):+d} {E(macro.get("regime",""))}' if macro else "")
         + '</div>']
    B.append('<div class="grid">')
    for row in rows:
        sym = row.get("symbol", "?")
        action = row.get("action", "OBSERVE")
        total = row.get("pillar_total", 0)
        close = _num(row.get("close"))
        sc = row.get("score", row)  # nested {trend,..} or flat
        held = row.get("holding")
        B.append('<div class="scard">')
        B.append(f'<div class="top"><span class="sym">{E(sym)}</span>'
                 f'<span class="badge b-{_cls(action)}">{E(action)}</span></div>')
        B.append('<div style="display:flex;justify-content:space-between;align-items:center">'
                 f'<span class="px">{close:.2f}' if close is not None else '<div style="display:flex;justify-content:space-between;align-items:center"><span class="px">—')
        B.append(f' · total <b>{total:+d}</b></span>{sparkline(row.get("spark") or row.get("close_series") or [])}</div>')
        B.append(pillar_bar("Trend", sc.get("trend")))
        B.append(pillar_bar("Momentum", sc.get("momentum")))
        B.append(pillar_bar("Macro", sc.get("macro")))
        chips = []
        if held:
            chips.append('<span class="chip" style="color:var(--blue)">holding</span>')
        if row.get("stop") is not None:
            sp = row.get("stop_pct")
            chips.append(f'<span class="chip">stop {row["stop"]:.2f}'
                         + (f' ({sp:+g}%)' if sp is not None else "") + '</span>')
        if row.get("forecast_vol") is not None:
            chips.append(f'<span class="chip">vol {row["forecast_vol"]*100:.1f}%</span>')
        if row.get("size_fraction") is not None:
            chips.append(f'<span class="chip">size {row["size_fraction"]*100:.0f}%</span>')
        if chips:
            B.append('<div class="chips">' + "".join(chips) + '</div>')
        B.append('</div>')
    B.append('</div>')
    B.append('<div class="footer">Read-only scanner view · to act on one, ask Claude for its proposal card.</div>')
    return _page("Scanner", "".join(B))


# ── portfolio ────────────────────────────────────────────────────────────────

def build_portfolio(data: dict) -> str:
    pos = data.get("positions") or []
    B = ['<h1>Portfolio</h1>',
         f'<div class="sub">{E(data.get("account",""))} · {E(data.get("as_of",""))}</div>']
    # summary
    B.append('<div class="card"><h2>Summary</h2>')
    for k, label in (("portfolio_value", "Market value"),
                     ("buying_power", "Buying power"),
                     ("settled_cash", "Settled cash (T+1)")):
        if data.get(k) is not None:
            B.append(f'<div class="kv"><span class="k">{label}</span>'
                     f'<span>${_num(data[k],0):,.2f}</span></div>')
    tot_pnl = sum(_num(p.get("pnl"), 0) for p in pos)
    pc = "pos" if tot_pnl >= 0 else "neg"
    B.append(f'<div class="kv"><span class="k">Unrealized P&L</span>'
             f'<span class="{pc}">${tot_pnl:,.2f}</span></div></div>')
    # positions
    B.append('<div class="card"><h2>Positions</h2>'
             '<table><tr><th>Symbol</th><th>Weight</th><th class="r">Qty</th>'
             '<th class="r">Avg</th><th class="r">Last</th><th class="r">Value</th>'
             '<th class="r">P&L</th><th>Signal</th></tr>')
    for p in pos:
        sym = p.get("symbol", "?")
        prot = p.get("protected")
        weight = _num(p.get("weight_pct"), 0)
        pnl = _num(p.get("pnl"), 0)
        pnlpct = p.get("pnl_pct")
        pc = "pos" if pnl >= 0 else "neg"
        action = p.get("action", "")
        name_cell = E(sym) + (' <span class="badge b-prot">PROTECTED</span>' if prot else "")
        sig = (f'<span class="badge b-{_cls(action)}">{E(action)}</span>' if action else "")
        B.append(
            f'<tr><td>{name_cell}</td>'
            f'<td><div class="abar"><span style="width:{max(0,min(100,weight)):.1f}%"></span></div>'
            f'<span style="font-size:11px;color:var(--dim)">{weight:.1f}%</span></td>'
            f'<td class="r">{E(str(p.get("qty","")))}</td>'
            f'<td class="r">{_num(p.get("avg_cost"),0):.2f}</td>'
            f'<td class="r">{_num(p.get("last"),0):.2f}</td>'
            f'<td class="r">${_num(p.get("mkt_value"),0):,.0f}</td>'
            f'<td class="r {pc}">${pnl:,.0f}'
            + (f' ({pnlpct:+g}%)' if pnlpct is not None else "") + '</td>'
            f'<td>{sig}</td></tr>')
    B.append('</table></div>')
    if any(p.get("protected") for p in pos):
        B.append('<div class="confirm" style="background:#1e1533;border-color:var(--purple);color:var(--purple)">'
                 '🛡 PROTECTED positions are shown as exposure only — never scored for exit.</div>')
    B.append('<div class="footer">Portfolio snapshot · trading actions require a proposal card + your approval.</div>')
    return _page("Portfolio", "".join(B))


# ── daily briefing ───────────────────────────────────────────────────────

def _brief_card(row: dict, accent: str) -> str:
    sym = row.get("symbol", "?")
    action = row.get("action", "")
    total = row.get("pillar_total", 0)
    close = _num(row.get("close"))
    sc = row.get("score", {})
    B = [f'<div class="scard" style="border-left:3px solid {accent}">']
    B.append(f'<div class="top"><span class="sym">{E(sym)}</span>'
             f'<span class="badge b-{_cls(action)}">{E(action)}</span></div>')
    px = f'{close:.2f}' if close is not None else '—'
    B.append(f'<div style="display:flex;justify-content:space-between;align-items:center">'
             f'<span class="px">{px} · total <b>{total:+d}</b></span>'
             f'{sparkline(row.get("spark") or [])}</div>')
    B.append(pillar_bar("Trend", sc.get("trend")))
    B.append(pillar_bar("Momentum", sc.get("momentum")))
    B.append(pillar_bar("Macro", sc.get("macro")))
    pats = row.get("patterns") or []
    if pats:
        B.append('<div style="margin-top:8px">')
        for pt in pats[:4]:
            B.append(f'<span class="flag" style="color:var(--dim);font-size:12px">• {E(pt)}</span>')
        B.append('</div>')
    chips = []
    if row.get("holding"):
        chips.append('<span class="chip" style="color:var(--blue)">holding</span>')
    if row.get("stop") is not None:
        sp = row.get("stop_pct")
        chips.append(f'<span class="chip">stop {row["stop"]:.2f}'
                     + (f' ({sp:+g}%)' if sp is not None else "") + '</span>')
    if row.get("forecast_vol") is not None:
        chips.append(f'<span class="chip">vol {row["forecast_vol"]*100:.1f}%</span>')
    if row.get("size_fraction") is not None:
        chips.append(f'<span class="chip">size {row["size_fraction"]*100:.0f}%</span>')
    ev = row.get("earnings")
    if ev:
        du = int(_num(ev.get("days_until"), 0) or 0)
        warn = ev.get("within_warn")
        col = "var(--amber)" if warn else "var(--dim)"
        icon = "⚠" if warn else "🗓"
        chips.append(f'<span class="chip" style="color:{col}">{icon} earnings {du}d '
                     f'({E(ev.get("next_date",""))})</span>')
    if chips:
        B.append('<div class="chips">' + "".join(chips) + '</div>')
    if row.get("note"):
        B.append(f'<div style="margin-top:8px;font-size:12px;color:var(--dim)">{E(row["note"])}</div>')
    B.append('</div>')
    return "".join(B)


def build_briefing(b: dict) -> str:
    sec = b.get("sections", {})
    sm = b.get("summary", {})
    macro = b.get("macro") or {}
    B = ['<h1>Daily Briefing</h1>',
         f'<div class="sub">{E(b.get("as_of",""))} · {sm.get("tickers",0)} watchlist tickers scanned</div>']

    # macro banner
    reg = macro.get("regime", "")
    ps = macro.get("pillar_score", 0)
    mcls = "enter" if ps > 0 else "exit" if ps < 0 else "wait"
    sr = macro.get("stat_regime") or {}
    srtxt = (f' · vol regime {E(str(sr.get("state",""))).upper()} (p_turb {_num(sr.get("p_turbulent"),0):.2f})'
             if sr else "")
    B.append(f'<div class="banner {mcls}">Market — macro {ps:+d} {E(reg)}'
             f'<small>Cross-asset regime sets today\'s Macro-Sentiment pillar for every ticker{E(srtxt)}</small></div>')

    # summary chips
    B.append('<div class="card"><h2>At a glance</h2><div class="chips" style="font-size:13px">')
    for label, key, col in (("opportunities", "opportunities", "var(--green)"),
                            ("warnings", "warnings", "var(--red)"),
                            ("holds", "holds", "var(--blue)"),
                            ("watch", "watch", "var(--dim)")):
        B.append(f'<span class="chip" style="color:{col};font-size:13px">'
                 f'{sm.get(key,0)} {label}</span>')
    B.append('</div></div>')

    def section(title, key, accent, empty):
        rows = sec.get(key) or []
        B.append(f'<h2 style="margin:18px 0 8px">{E(title)}</h2>')
        if not rows:
            B.append(f'<div class="card" style="color:var(--dim)">{E(empty)}</div>')
            return
        B.append('<div class="grid">')
        for r in rows:
            B.append(_brief_card(r, accent))
        B.append('</div>')

    section("🎯 Opportunities — flat, fresh trigger", "opportunities",
            "var(--green)", "No clean entry triggers today.")
    section("⚠ Warnings — holding, momentum turning", "warnings",
            "var(--red)", "No exit/trim warnings on open positions.")
    section("↔ Holds — riding the cycle", "holds",
            "var(--blue)", "No active holds.")
    section("· Watch — no action", "watch",
            "var(--dim)", "Nothing on the watch bench.")

    B.append('<div class="footer">Briefing is analysis only · act on one by asking Claude '
             'for its proposal card (execution timeline + approval form).</div>')
    return _page("Daily Briefing", "".join(B))


# ── backtest ──────────────────────────────────────────────────────────────

def build_backtest(bt: dict) -> str:
    # Accept native backtest.py --json ({backtest,walk_forward,sensitivity})
    # or a flat dict. All *_pct metrics are already in percent units.
    res = bt.get("backtest", bt)
    wf = bt.get("walk_forward") or res.get("walk_forward") or []
    grid = bt.get("sensitivity") or res.get("sensitivity") or []
    m = res.get("metrics", {})
    period = res.get("period", {})
    B = ['<h1>Backtest</h1>',
         f'<div class="sub">{E(bt.get("symbol",""))} · {res.get("bars_tested","?")} bars · '
         f'{E(period.get("start",""))} → {E(period.get("end",""))} · '
         'no lookahead · next-close fills</div>']
    curve = res.get("equity_curve") or []
    bh = res.get("buyhold_equity") or []
    if curve:
        W, H, pad = 1000, 260, 34
        def poly(series, col):
            s = [v for v in (_num(x) for x in series) if v is not None]
            if len(s) < 2:
                return ""
            allv = s + [v for v in (_num(x) for x in (bh or [])) if v is not None]
            lo, hi = min(allv), max(allv)
            rng = (hi - lo) or 1.0
            n = len(s)
            pts = []
            for i, v in enumerate(s):
                x = round(pad + i / (n - 1) * (W - 2 * pad), 1)
                y = round(pad + (H - 2 * pad) - (v - lo) / rng * (H - 2 * pad), 1)
                pts.append(f"{x},{y}")
            return f'<polyline fill="none" stroke="{col}" stroke-width="1.6" points="{" ".join(pts)}"/>'
        svg = (f'<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" style="max-width:100%">'
               f'<line x1="{pad}" y1="{H-pad}" x2="{W-pad}" y2="{H-pad}" stroke="var(--line)"/>'
               + poly(bh, "var(--dim)") + poly(curve, "var(--green)")
               + f'<text x="{pad}" y="18" fill="var(--green)" font-size="11">strategy</text>'
               + f'<text x="{pad+70}" y="18" fill="var(--dim)" font-size="11">buy &amp; hold</text>'
               + '</svg>')
        B.append(f'<div class="card"><h2>Equity curve</h2>{svg}</div>')

    if m:
        _LBL = {"total_return_pct": "Total return", "cagr_pct": "CAGR",
                "sharpe": "Sharpe", "sortino": "Sortino",
                "max_drawdown_pct": "Max drawdown", "n_trades": "Trades",
                "win_rate_pct": "Win rate", "profit_factor": "Profit factor",
                "avg_trade_pct": "Avg trade", "avg_bars_held": "Avg bars held",
                "exposure_pct": "Exposure"}
        B.append('<div class="card"><h2>Stats</h2>')
        for k, label in _LBL.items():
            if m.get(k) is None:
                continue
            v = f'{m[k]}%' if k.endswith("_pct") else str(m[k])
            B.append(f'<div class="kv"><span class="k">{label}</span>'
                     f'<span>{E(v)}</span></div>')
        if res.get("buy_hold_return_pct") is not None:
            B.append(f'<div class="kv"><span class="k">Buy &amp; hold return</span>'
                     f'<span>{res["buy_hold_return_pct"]}%</span></div>')
        B.append('</div>')

    if wf:
        B.append('<div class="card"><h2>Walk-forward segments (out-of-sample)</h2>'
                 '<table><tr><th>Segment</th><th class="r">Strat</th>'
                 '<th class="r">B&amp;H</th><th class="r">Edge</th></tr>')
        for seg in wf:
            st = _num((seg.get("metrics") or {}).get("total_return_pct"), 0)
            bh_r = _num(seg.get("buy_hold_return_pct"), 0)
            edge = st - bh_r
            ec = "pos" if edge >= 0 else "neg"
            B.append(f'<tr><td>{E(str(seg.get("segment","")))}</td>'
                     f'<td class="r">{st:+.1f}%</td><td class="r">{bh_r:+.1f}%</td>'
                     f'<td class="r {ec}">{edge:+.1f}%</td></tr>')
        B.append('</table></div>')

    if grid:
        B.append('<div class="card"><h2>Sensitivity (lag × cost)</h2>'
                 '<table><tr><th>lag</th><th>cost_bps</th><th class="r">return</th>'
                 '<th class="r">sharpe</th><th class="r">max dd</th></tr>')
        for g in grid:
            ret = _num(g.get("total_return_pct"), 0)
            rc = "pos" if ret >= 0 else "neg"
            B.append(f'<tr><td>{E(str(g.get("lag","")))}</td><td>{E(str(g.get("cost_bps","")))}</td>'
                     f'<td class="r {rc}">{ret:+.1f}%</td>'
                     f'<td class="r">{E(str(g.get("sharpe","—")))}</td>'
                     f'<td class="r">{E(str(g.get("max_drawdown_pct","—")))}%</td></tr>')
        B.append('</table></div>')
    B.append('<div class="footer">Past performance under exact score.py rules · not a forecast.</div>')
    return _page("Backtest", "".join(B))


# ── self-test fixtures ──────────────────────────────────────────────────────

def _selftest_card():
    import score as S
    close = [round(100 + i * 0.25 + 6 * math.sin(i / 12), 2) for i in range(260)]
    close += [close[-1] * 1.05, close[-1] * 1.10]
    return S.score_symbol(close, macro_score=1, symbol="SELFTEST", holding=True)


def _selftest_plan():
    import execution_plan as Ex
    return Ex.plan({"symbol": "SELFTEST", "side": "sell", "qty": 800,
                    "quote": {"bid": 183.10, "ask": 183.18, "last": 183.12,
                              "bid_size": 900, "ask_size": 400, "age_sec": 2.0},
                    "adv": 1_500_000, "urgency": "normal", "horizon_min": 20})


def _selftest_scanner():
    def spark(base):
        return [round(base + 4 * math.sin(i / 5), 2) for i in range(40)]
    return {"as_of": "2026-07-05T20:00:00Z",
            "macro": {"pillar_score": 1, "regime": "risk-on"},
            "rows": [
                {"symbol": "NVDA", "action": "HOLD / OBSERVE", "pillar_total": -1,
                 "score": {"trend": 1, "momentum": -2, "macro": 0}, "close": 965.0,
                 "stop": 1018.14, "stop_pct": -5.51, "forecast_vol": 0.449,
                 "size_fraction": 0.10, "holding": True, "spark": spark(940)},
                {"symbol": "AAPL", "action": "RE-ENTRY (new cycle)", "pillar_total": 4,
                 "score": {"trend": 2, "momentum": 2, "macro": 0}, "close": 212.0,
                 "stop": 205.4, "stop_pct": -3.1, "forecast_vol": 0.21,
                 "size_fraction": 0.15, "holding": False, "spark": spark(200)},
                {"symbol": "MSFT", "action": "OBSERVE", "pillar_total": 2,
                 "score": {"trend": 2, "momentum": 0, "macro": 0}, "close": 425.08,
                 "forecast_vol": 0.089, "spark": spark(415)},
            ]}


def _selftest_portfolio():
    return {"account": "Agentic (cash)", "as_of": "2026-07-05T20:00:00Z",
            "portfolio_value": 48250.0, "buying_power": 12100.0, "settled_cash": 9800.0,
            "positions": [
                {"symbol": "NVDA", "qty": 10, "avg_cost": 900, "last": 965,
                 "mkt_value": 9650, "pnl": 650, "pnl_pct": 7.2, "weight_pct": 20.0,
                 "action": "HOLD", "pillar_total": -1},
                {"symbol": "AAPL", "qty": 40, "avg_cost": 190, "last": 212,
                 "mkt_value": 8480, "pnl": 880, "pnl_pct": 11.6, "weight_pct": 17.6,
                 "action": "HOLD"},
                {"symbol": "RSU-CORP", "qty": 100, "avg_cost": 50, "last": 62,
                 "mkt_value": 6200, "pnl": 1200, "pnl_pct": 24.0, "weight_pct": 12.8,
                 "protected": True},
            ]}


def _selftest_backtest():
    strat = [round(1.0 * (1.0009) ** i + 0.05 * math.sin(i / 15), 5) for i in range(250)]
    bh = [round(1.0 * (1.0007) ** i, 5) for i in range(250)]
    return {"symbol": "SELFTEST",
            "backtest": {
                "bars_tested": 250,
                "period": {"start": "2025-07-01", "end": "2026-07-01"},
                "equity_curve": strat, "buyhold_equity": bh,
                "buy_hold_return_pct": 18.1,
                "metrics": {"total_return_pct": 24.8, "cagr_pct": 24.8, "sharpe": 1.12,
                            "sortino": 1.48, "max_drawdown_pct": -9.3, "n_trades": 27,
                            "win_rate_pct": 56.0, "profit_factor": 1.9,
                            "avg_trade_pct": 0.9, "avg_bars_held": 6.2, "exposure_pct": 61.0}},
            "walk_forward": [
                {"segment": 1, "buy_hold_return_pct": 4.2, "metrics": {"total_return_pct": 6.1}},
                {"segment": 2, "buy_hold_return_pct": 5.1, "metrics": {"total_return_pct": 3.8}},
                {"segment": 3, "buy_hold_return_pct": 3.3, "metrics": {"total_return_pct": 7.2}},
                {"segment": 4, "buy_hold_return_pct": 3.9, "metrics": {"total_return_pct": 4.5}}],
            "sensitivity": [
                {"lag": 1, "cost_bps": 5.0, "total_return_pct": 24.8, "sharpe": 1.12, "max_drawdown_pct": -9.3},
                {"lag": 1, "cost_bps": 10.0, "total_return_pct": 19.1, "sharpe": 0.94, "max_drawdown_pct": -10.1},
                {"lag": 2, "cost_bps": 5.0, "total_return_pct": 17.3, "sharpe": 0.81, "max_drawdown_pct": -11.4},
                {"lag": 2, "cost_bps": 10.0, "total_return_pct": 12.1, "sharpe": 0.63, "max_drawdown_pct": -12.0}]}


# ── CLI ──────────────────────────────────────────────────────────────────────

def _read(path):
    with open(path) as f:
        return json.load(f)


def main() -> int:
    ap = argparse.ArgumentParser(description="Unified HTML view layer for the trading desk skill.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("proposal", help="single-ticker advice + execution + approval form")
    sp.add_argument("card", nargs="?", help="score.py --json output (omit for self-test)")
    sp.add_argument("--plan", help="execution_plan.py --json output")
    sp.add_argument("--macro", help="macro_pillar.py --json output")
    sp.add_argument("-o", "--out", default="proposal.html")

    ss = sub.add_parser("scanner", help="multi-ticker grid")
    ss.add_argument("data", nargs="?", help="{rows:[...]} or auto_status.json (omit for self-test)")
    ss.add_argument("-o", "--out", default="scanner.html")

    spf = sub.add_parser("portfolio", help="positions + allocation")
    spf.add_argument("data", nargs="?", help="{positions:[...]} (omit for self-test)")
    spf.add_argument("-o", "--out", default="portfolio.html")

    sb = sub.add_parser("backtest", help="equity curve + walk-forward + sensitivity")
    sb.add_argument("data", nargs="?", help="backtest.py --json output (omit for self-test)")
    sb.add_argument("-o", "--out", default="backtest.html")

    sbr = sub.add_parser("briefing", help="daily watchlist briefing")
    sbr.add_argument("data", nargs="?", help="daily_briefing.py --json output (omit for self-test)")
    sbr.add_argument("-o", "--out", default="briefing.html")

    sal = sub.add_parser("allocate", help="30-day budget book (allocate.py output)")
    sal.add_argument("data", nargs="?", help="allocate.py --json output (omit for self-test)")
    sal.add_argument("-o", "--out", default="allocate.html")

    args = ap.parse_args()

    if args.cmd == "proposal":
        card = _read(args.card) if args.card else _selftest_card()
        plan = _read(args.plan) if args.plan else (None if args.card else _selftest_plan())
        macro = _read(args.macro) if args.macro else None
        htmlout = build_proposal(card, plan, macro)
    elif args.cmd == "scanner":
        htmlout = build_scanner(_read(args.data) if args.data else _selftest_scanner())
    elif args.cmd == "portfolio":
        htmlout = build_portfolio(_read(args.data) if args.data else _selftest_portfolio())
    elif args.cmd == "backtest":
        htmlout = build_backtest(_read(args.data) if args.data else _selftest_backtest())
    elif args.cmd == "briefing":
        if args.data:
            brief = _read(args.data)
        else:
            import daily_briefing as DB
            brief = DB.build(DB._selftest_batch())
        htmlout = build_briefing(brief)
    elif args.cmd == "allocate":
        if args.data:
            book = _read(args.data)
        else:
            import allocate as AL
            book = AL.allocate({
                "as_of": "2026-07-06", "rows": [
                    {"symbol": "XOM", "action": "RE-ENTRY (new cycle)", "pillar_total": 5, "holding": False, "close": 137.09, "size_fraction": 1.0},
                    {"symbol": "BA", "action": "RE-ENTRY (new cycle)", "pillar_total": 4, "holding": False, "close": 226.49, "size_fraction": 0.45},
                    {"symbol": "NOW", "action": "TACTICAL BOUNCE", "pillar_total": 3, "holding": False, "close": 902.0, "size_fraction": 0.5},
                    {"symbol": "NVDA", "action": "STAY OUT / AVOID", "pillar_total": -1, "holding": True, "close": 194.83, "size_fraction": 0.44},
                ]}, cash=500, cycle_start="2026-07-06", as_of="2026-07-06",
                held={"NVDA": 40.0})
        htmlout = build_allocate(book)
    else:
        ap.error("unknown command")
        return 2

    with open(args.out, "w") as f:
        f.write(htmlout)
    if not getattr(args, "data", None) and not getattr(args, "card", None):
        print("[self-test sample]", file=sys.stderr)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
