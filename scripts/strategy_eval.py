#!/usr/bin/env python3
"""
strategy_eval.py — periodic quantitative evaluator for the Agentic Trading Desk.
================================================================================
Reads the accumulated signal log (desk_signals_SPY.jsonl), marks each signal to
REAL forward SPY prices from Robinhood, and reports whether the strategy is
actually making money — then STAGES parameter-tuning proposals for the user to
approve. It never edits live config, never places orders, never writes memory.

Design goals (memory-safe by construction):
  * All knowledge lives on disk in the project, not in persistent memory:
      - reads:  desk_signals_SPY.jsonl, desk_config.json
      - writes: desk_config.proposed.json (staged proposal — human adopts)
                eval_reports/strategy_eval_<date>.txt (dated history)
  * Statistically honest: below MIN_SAMPLE closed trades it reports metrics but
    explicitly REFUSES to propose parameter changes (no tuning to noise).

What it measures
----------------
1. Directional hit-rate by action type: do RE-ENTRY / HOLD calls actually
   precede up-moves? Do EXIT / stay-out calls precede down-moves?
2. Forward returns at multiple horizons (next close, +3 bars, +5 bars).
3. Signal quality vs. pillar_total (does a higher composite score predict a
   better forward return? Spearman-style rank check, stdlib only).
4. Risk-parameter sweep: replays every logged BULLISH entry through a range of
   {stop_loss, trail_arm, trail_giveback} settings using real forward bars and
   reports which setting would have produced the best realized P/L — proposed,
   not applied.

Fail-safe: needs a valid Robinhood token (caller refreshes) and forward price
data. On any data/auth failure it prints a short notice and exits 0.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import rh_data  # noqa: E402
import score as S  # noqa: E402  (reuse the LIVE three-pillar scorer for backtests)

TICKER = os.environ.get("DESK_TICKER", "SPY")
STATE_DIR = Path(os.environ.get("DESK_POS_DIR",
                                str(Path.home() / ".hermes" / "state")))
SIGNAL_LOG = STATE_DIR / f"desk_signals_{TICKER}.jsonl"

CONFIG_FILE = SCRIPT_DIR / "desk_config.json"
PROPOSAL_FILE = SCRIPT_DIR / "desk_config.proposed.json"
REPORT_DIR = SCRIPT_DIR / "eval_reports"

# Statistical guardrails: never tune on tiny samples.
MIN_SAMPLE_METRICS = 4     # below this we don't even show hit-rates as meaningful
MIN_SAMPLE_TUNING = 20     # below this we REFUSE to propose parameter changes

BULLISH_PREFIXES = ("RE-ENTRY", "TACTICAL REBOUND", "HOLD (RIDE")

# Parameter sweep grids (fractions).
STOP_GRID = [0.06, 0.08, 0.11, 0.14]
ARM_GRID = [0.03, 0.05, 0.07]
GIVE_GRID = [0.02, 0.03, 0.04]


def is_bullish(action: str) -> bool:
    a = (action or "").upper()
    if a.startswith("EXIT"):
        return False
    return any(a.startswith(p) for p in BULLISH_PREFIXES)


def load_signals() -> list[dict]:
    if not SIGNAL_LOG.exists():
        return []
    out = []
    for line in SIGNAL_LOG.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def load_config() -> dict:
    try:
        raw = json.loads(CONFIG_FILE.read_text())
        return raw.get("risk_params", {})
    except Exception:  # noqa: BLE001
        return {}


def build_date_close_map(data: "rh_data.RHData") -> dict:
    """Map 'YYYY-MM-DD' -> close from real SPY daily bars (via Robinhood)."""
    bars = data._bars([TICKER], 800).get(TICKER, [])  # closes old->new
    # We need dates too; re-fetch with dates preserved.
    # rh_data._bars strips dates, so pull raw here for the date alignment.
    from datetime import timedelta
    start = (datetime.now(timezone.utc) - timedelta(days=800)
             ).strftime("%Y-%m-%dT00:00:00Z")
    res = data.mcp.call_tool("get_equity_historicals", {
        "symbols": [TICKER], "start_time": start,
        "interval": "day", "bounds": "regular", "adjustment_type": "split",
    })
    out = {}
    seq = []
    for r in res.get("data", {}).get("results", []):
        for b in r.get("bars", []):
            d = (b.get("begins_at") or "")[:10]
            cp = b.get("close_price")
            if d and cp is not None:
                out[d] = float(cp)
                seq.append((d, float(cp)))
    return {"by_date": out, "ordered": seq}


def forward_return(seq: list, entry_date: str, horizon: int) -> float | None:
    """Return pct change from the close ON/AFTER entry_date to +horizon bars."""
    idx = next((i for i, (d, _) in enumerate(seq) if d >= entry_date), None)
    if idx is None or idx + horizon >= len(seq):
        return None
    p0 = seq[idx][1]
    p1 = seq[idx + horizon][1]
    if p0 <= 0:
        return None
    return p1 / p0 - 1.0


def sweep_bullish(entries: list, seq: list) -> dict:
    """Replay bullish entries under each (stop,arm,give) combo on real forward
    bars; return the combo with the best mean realized P/L and the baseline."""
    def realized(entry_date, entry_price, stop, arm, give):
        idx = next((i for i, (d, _) in enumerate(seq) if d >= entry_date), None)
        if idx is None:
            return None
        peak = entry_price
        armed = False
        # Walk forward up to 20 bars; exit on stop/trail, else mark last bar.
        last = entry_price
        for j in range(idx, min(idx + 20, len(seq))):
            px = seq[j][1]
            last = px
            ret = px / entry_price - 1.0
            peak = max(peak, px)
            if peak / entry_price - 1.0 >= arm:
                armed = True
            if ret <= -stop:
                return -stop
            if armed and px / peak - 1.0 <= -give:
                return px / entry_price - 1.0
        return last / entry_price - 1.0

    results = []
    for stop in STOP_GRID:
        for arm in ARM_GRID:
            for give in GIVE_GRID:
                pls = []
                for e in entries:
                    r = realized(e["date"], e["price"], stop, arm, give)
                    if r is not None:
                        pls.append(r)
                if pls:
                    results.append({
                        "stop_loss_pct": stop, "trail_arm_pct": arm,
                        "trail_giveback_pct": give,
                        "mean_pl_pct": round(statistics.mean(pls) * 100, 3),
                        "n": len(pls),
                    })
    results.sort(key=lambda x: x["mean_pl_pct"], reverse=True)
    return {"ranked": results}


def rank_corr(pairs: list) -> float | None:
    """Spearman rank correlation between pillar_total and forward return."""
    pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
    if len(pairs) < 5:
        return None
    def ranks(vals):
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        r = [0.0] * len(vals)
        for rank, i in enumerate(order):
            r[i] = rank
        return r
    xs = ranks([p[0] for p in pairs])
    ys = ranks([p[1] for p in pairs])
    n = len(pairs)
    d2 = sum((xs[i] - ys[i]) ** 2 for i in range(n))
    return 1 - (6 * d2) / (n * (n * n - 1))


def action_diversity(signals: list) -> list[str]:
    """Break down the emitted actions + macro regimes so a signal engine that is
    stuck on one verdict (e.g. only ever "HOLD (ride the cycle)") is flagged
    loudly. Pure counting over the logged signals — no price data needed."""
    from collections import Counter
    L = ["Signal-mix diagnostics:"]
    n = len(signals)
    act = Counter((s.get("action") or "?") for s in signals)
    base = Counter((s.get("base_action") or "?") for s in signals)
    reg = Counter((s.get("macro_regime") or "?") for s in signals)

    # Emitted (post-risk-overlay) action distribution.
    top_action, top_n = act.most_common(1)[0]
    L.append(f"  Distinct emitted actions: {len(act)}")
    for a, c in act.most_common():
        L.append(f"    {c:>3}× ({c/n*100:>3.0f}%)  {a}")
    # Loud flag when the engine is monotonous.
    if len(act) == 1:
        L.append(f"  ⚠️ STUCK: every one of {n} signals is the SAME action "
                 f"('{top_action}'). Either the market is genuinely one-regime, "
                 "or a decision threshold in score.py never triggers. Investigate "
                 "with the backtest below.")
    elif top_n / n >= 0.9:
        L.append(f"  ⚠️ LOW DIVERSITY: {top_n/n*100:.0f}% of signals are "
                 f"'{top_action}'. The engine rarely changes its mind.")

    # base_action vs emitted: did the risk overlay ever override the framework?
    overrides = sum(1 for s in signals
                    if s.get("base_action") and s.get("action")
                    and s["base_action"] != s["action"])
    L.append(f"  Risk-overlay overrides of the base signal: {overrides}/{n}")

    # Macro-regime spread.
    if len(reg) == 1 and "?" not in reg:
        only = next(iter(reg))
        L.append(f"  Macro regime: constant '{only}' across all {n} signals.")
    else:
        L.append("  Macro-regime mix: " +
                 ", ".join(f"{r}×{c}" for r, c in reg.most_common()))

    # Pillar-total spread (is the composite actually moving?).
    pts = [s.get("pillar_total") for s in signals
           if isinstance(s.get("pillar_total"), int)]
    if pts:
        L.append(f"  Pillar-total range: {min(pts):+d}..{max(pts):+d} "
                 f"(mean {statistics.mean(pts):+.1f}, n={len(pts)})")
    return L


def backtest_pillar_edge(data: "rh_data.RHData", lookback_days: int = 800,
                         horizons=(1, 3, 5), min_bars: int = 220) -> list[str]:
    """Reconstruct the LIVE technical scorer over real historical SPY bars and
    test whether a higher composite pillar score actually precedes better
    forward returns. This interrogates the tiny-sample -0.20 correlation the
    weekly report flags, over hundreds of bars instead of ~6 logged signals.

    HONEST SCOPE: reconstructs Trend + Momentum (the deterministic technical
    core of score.py, macro_score=None). The Macro-Sentiment pillar is NOT
    reconstructed per-date here (it needs 8 aligned ETF series + the FRED curve
    as-of each historical date — fragile to rebuild), so this measures the
    TECHNICAL composite's predictive power, which is the part that fires the
    exhaustion/rebound decisions. Stated in the output so it isn't over-read.
    """
    L = ["─" * 46,
         "🧪 HISTORICAL BACKTEST — does a higher technical score predict "
         "higher forward return?"]
    try:
        m = build_date_close_map(data)
        seq = m["ordered"]  # [(date, close)] old->new
    except Exception as e:  # noqa: BLE001
        L.append(f"  ⚠️ Could not fetch SPY history for the backtest ({e}). Skipped.")
        return L

    closes = [c for _, c in seq]
    n = len(closes)
    if n < min_bars + max(horizons) + 20:
        L.append(f"  ⚠️ Only {n} SPY bars available; need "
                 f"≥{min_bars + max(horizons) + 20} for a meaningful backtest. Skipped.")
        return L

    # Walk forward: at each bar i (>= min_bars) score the technical composite
    # using ONLY bars up to i, then look up realized forward returns.
    per_h = {h: [] for h in horizons}   # (composite, fwd_return) pairs
    composites = []
    action_at = []  # (composite, forward_1d) grouped by decided action
    from collections import defaultdict
    by_action = defaultdict(list)
    for i in range(min_bars, n - max(horizons)):
        window = closes[:i + 1]
        try:
            card = S.score_symbol(window, macro_score=None, symbol=TICKER,
                                  holding=None)
        except Exception:  # noqa: BLE001
            continue
        comp = card["pillar_total"]            # trend + momentum (macro=None)
        composites.append(comp)
        p0 = closes[i]
        act = card["decision"]["action"]
        for h in horizons:
            if i + h < n and p0 > 0:
                fr = closes[i + h] / p0 - 1.0
                per_h[h].append((comp, fr))
                if h == 1:
                    by_action[act].append(fr)

    if not composites:
        L.append("  ⚠️ Backtest produced no scored bars. Skipped.")
        return L

    L.append(f"  Scored {len(composites)} historical SPY bars "
             f"(technical composite, Trend+Momentum, range "
             f"{min(composites):+d}..{max(composites):+d}).")

    # Spearman rank corr at each horizon: composite vs forward return.
    L.append("  Rank correlation (technical composite → forward return):")
    for h in horizons:
        rc = rank_corr(per_h[h])
        if rc is None:
            L.append(f"    +{h}d: (insufficient data)")
            continue
        tag = ("✅ positive (score is predictive)" if rc > 0.10 else
               "❌ NEGATIVE (higher score → worse — real miscalibration)"
               if rc < -0.10 else "➖ ~zero (score has little forward edge)")
        L.append(f"    +{h}d: {rc:+.3f}  {tag}  (n={len(per_h[h])})")

    # Mean forward-1d return by decided action — which verdicts actually work?
    if by_action:
        L.append("  Mean next-day return by decided action (backtest):")
        rows = sorted(by_action.items(),
                      key=lambda kv: statistics.mean(kv[1]), reverse=True)
        for act, frs in rows:
            if len(frs) >= 5:
                L.append(f"    {statistics.mean(frs)*100:+.2f}%  "
                         f"(n={len(frs):>4})  {act}")

    # Decile-style check: top-third vs bottom-third composite forward returns.
    pairs1 = sorted(per_h[1], key=lambda x: x[0])
    if len(pairs1) >= 30:
        k = len(pairs1) // 3
        low = [fr for _, fr in pairs1[:k]]
        high = [fr for _, fr in pairs1[-k:]]
        lo_m, hi_m = statistics.mean(low) * 100, statistics.mean(high) * 100
        L.append(f"  Bottom-third score fwd-1d: {lo_m:+.2f}%   "
                 f"Top-third: {hi_m:+.2f}%   spread: {hi_m - lo_m:+.2f}pp")
        if hi_m - lo_m < 0:
            L.append("    ⚠️ Top-scoring bars UNDER-perform bottom-scoring bars: "
                     "the composite is inversely related to short-term returns "
                     "over this history. This is a mean-reversion signature "
                     "(high momentum → short-term pullback), not necessarily a "
                     "bug — but it means the score is a poor SHORT-horizon timing "
                     "signal. Consider longer horizons or a contrarian read.")
    L.append("  NOTE: technical composite only (macro pillar excluded — see code). "
             "Longer forward horizons matter for a trend/rotation strategy.")
    return L


def strategy_scoreboard() -> list[str]:
    """Head-to-head on the out-of-sample TEST slice: active strategy mode vs
    buy-and-hold vs the alternative engine. Reuses backtest.py's harness and the
    cached scored bars (rebuilds only if the cache is missing/stale). Keeps the
    daily digest honest: it always shows whether the live strategy actually
    beats just holding SPY, risk-adjusted."""
    L = ["─" * 46, "🏁 STRATEGY SCOREBOARD (out-of-sample TEST slice)"]
    try:
        import backtest as BT
        import experiments as EX
        cache = BT.BARS_CACHE
        stale = (not cache.exists()) or (
            (datetime.now().timestamp() - cache.stat().st_mtime) > 5 * 86400)
        bars = BT.build_scored_bars(force=stale)
        test = BT.slice_bars(bars, 0.65, 1.0)
        try:
            cfg = json.loads((SCRIPT_DIR / "desk_config.json").read_text())
            mode = cfg.get("strategy_mode", "regime_v4")
        except Exception:  # noqa: BLE001
            mode = "regime_v4"
        bh = BT.buy_hold(test)
        regime = EX.simulate_regime(test, gate=EX._regime_long,
                                    risk=BT.RISK_DEFAULTS)
        pillar = BT.simulate(test)
        L.append(f"  Window: {test[0]['date']} → {test[-1]['date']} "
                 f"({len(test)} bars). Active mode: {mode}")
        L.append("  " + BT.fmt(bh, "buy & hold"))
        L.append("  " + BT.fmt(regime, "regime_v4" + (" (ACTIVE)"
                               if mode == "regime_v4" else "")))
        L.append("  " + BT.fmt(pillar, "three_pillar" + (" (ACTIVE)"
                               if mode == "three_pillar" else " (old)")))
        if regime["sharpe"] >= bh["sharpe"] - 0.05 and regime["max_dd_pct"] > bh["max_dd_pct"]:
            L.append("  ✅ regime_v4 matches buy-hold risk-adjusted with a much "
                     "smaller drawdown — the intended edge.")
        elif bh["total_return_pct"] > regime["total_return_pct"]:
            L.append("  ℹ️ buy-hold has higher raw return here (bull run); "
                     "regime_v4's advantage is drawdown control / Sharpe.")
    except Exception as e:  # noqa: BLE001
        L.append(f"  ⚠️ Scoreboard unavailable ({type(e).__name__}: {e}).")
    return L


def main() -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    signals = load_signals()
    L = [f"🔬 STRATEGY EVALUATION — {TICKER}   ({today})"]

    if not signals:
        L.append("No signals logged yet. Nothing to evaluate; will build up as "
                 "the daily jobs run. (Signals accumulate in "
                 f"{SIGNAL_LOG.name}.)")
        print("\n".join(L))
        return 0

    # Fetch real forward prices (fail-safe).
    try:
        data = rh_data.RHData()
        m = build_date_close_map(data)
        seq = m["ordered"]
    except Exception as e:  # noqa: BLE001
        L.append(f"⚠️ Could not fetch SPY price history ({e}). "
                 "Evaluation skipped; will retry next cycle.")
        print("\n".join(L))
        return 0

    n_total = len(signals)
    L.append(f"Signals on record: {n_total}")
    L.append("─" * 46)

    # 0) Signal-mix diagnostics (no price data needed) — flags a stuck engine.
    L.extend(action_diversity(signals))
    L.append("─" * 46)

    # 1) Directional hit-rate + forward returns by action class.
    horizons = {"next": 1, "+3d": 3, "+5d": 5}
    buckets = {"bullish": [], "exit_flat": []}
    pillar_pairs = []
    for s in signals:
        fr1 = forward_return(seq, s["date"], 1)
        cls = "bullish" if is_bullish(s.get("action", "")) else "exit_flat"
        buckets[cls].append(fr1)
        pillar_pairs.append((s.get("pillar_total"), fr1))

    def hit_rate(vals, want_up: bool):
        vals = [v for v in vals if v is not None]
        if not vals:
            return None, 0
        good = sum(1 for v in vals if (v >= 0) == want_up)
        return good / len(vals), len(vals)

    br, bn = hit_rate(buckets["bullish"], True)
    er, en = hit_rate(buckets["exit_flat"], False)
    L.append("Directional hit-rate (next-close):")
    if br is not None:
        L.append(f"  Bullish calls  : {br*100:.0f}% correct  (n={bn})")
    else:
        L.append("  Bullish calls  : no closed samples yet")
    if er is not None:
        L.append(f"  Exit/stay-out  : {er*100:.0f}% correct  (n={en})")
    else:
        L.append("  Exit/stay-out  : no closed samples yet")

    # Mean forward returns of bullish calls at each horizon.
    L.append("Mean forward return after bullish calls:")
    for name, h in horizons.items():
        frs = [forward_return(seq, s["date"], h) for s in signals
               if is_bullish(s.get("action", ""))]
        frs = [f for f in frs if f is not None]
        if frs:
            L.append(f"  {name:>4}: {statistics.mean(frs)*100:+.2f}%  (n={len(frs)})")
        else:
            L.append(f"  {name:>4}: (not enough forward data yet)")

    # 2) Pillar-score predictive power.
    rc = rank_corr(pillar_pairs)
    if rc is not None:
        verdict = ("positive — higher score → better forward return"
                   if rc > 0.15 else
                   "negative — higher score → worse (investigate!)"
                   if rc < -0.15 else "weak/none at this sample size")
        L.append(f"Pillar-score vs forward return: rank corr {rc:+.2f} ({verdict})")

    # 2b) Historical backtest — interrogate the correlation over hundreds of
    # real SPY bars (reuses the already-connected `data` handle).
    L.extend(backtest_pillar_edge(data))

    # 2c) Strategy head-to-head: active mode vs buy-hold vs the alternative,
    # on the out-of-sample TEST slice. Uses the cached scored bars if present.
    L.extend(strategy_scoreboard())

    L.append("─" * 46)

    # 3) Risk-parameter sweep + staged proposal (guarded by sample size).
    bullish_entries = [s for s in signals if is_bullish(s.get("action", ""))]
    closed_like = [s for s in bullish_entries
                   if forward_return(seq, s["date"], 1) is not None]
    cur = load_config()
    L.append(f"Current live params: stop {cur.get('stop_loss_pct', 0.11):.0%} · "
             f"arm {cur.get('trail_arm_pct', 0.05):.0%} · "
             f"giveback {cur.get('trail_giveback_pct', 0.03):.0%}")

    if len(closed_like) < MIN_SAMPLE_TUNING:
        L.append(f"⏳ Only {len(closed_like)} evaluable bullish entries "
                 f"(need ≥{MIN_SAMPLE_TUNING} before proposing parameter "
                 "changes). Refusing to tune to noise. Metrics above are "
                 "informational; live parameters left UNCHANGED.")
        # Clean up any stale proposal so nothing misleading lingers.
        try:
            if PROPOSAL_FILE.exists():
                PROPOSAL_FILE.unlink()
        except OSError:
            pass
    else:
        sweep = sweep_bullish(bullish_entries, seq)
        ranked = sweep["ranked"]
        if ranked:
            best = ranked[0]
            base = next((r for r in ranked
                         if r["stop_loss_pct"] == cur.get("stop_loss_pct", 0.11)
                         and r["trail_arm_pct"] == cur.get("trail_arm_pct", 0.05)
                         and r["trail_giveback_pct"] == cur.get("trail_giveback_pct", 0.03)),
                        None)
            L.append(f"Best backtested combo: stop {best['stop_loss_pct']:.0%} · "
                     f"arm {best['trail_arm_pct']:.0%} · give "
                     f"{best['trail_giveback_pct']:.0%} → mean P/L "
                     f"{best['mean_pl_pct']:+.2f}% (n={best['n']})")
            if base:
                L.append(f"Current combo mean P/L: {base['mean_pl_pct']:+.2f}%")
            # Only propose if materially better than current.
            improve = best["mean_pl_pct"] - (base["mean_pl_pct"] if base else -999)
            changed = (base is None) or any(
                best[k] != cur.get(k) for k in
                ("stop_loss_pct", "trail_arm_pct", "trail_giveback_pct"))
            if changed and improve >= 0.25:  # >=0.25pp edge to bother
                proposal = {
                    "_README": "STAGED proposal from strategy_eval.py. NOT active. "
                               "To adopt: copy approved values into desk_config.json "
                               "'risk_params', then delete this file. Backtested on "
                               "your own logged signals vs real SPY forward bars.",
                    "generated": today,
                    "based_on_n_entries": best["n"],
                    "current": cur,
                    "proposed_risk_params": {
                        "stop_loss_pct": best["stop_loss_pct"],
                        "trail_arm_pct": best["trail_arm_pct"],
                        "trail_giveback_pct": best["trail_giveback_pct"],
                    },
                    "expected_edge_pp": round(improve, 3),
                }
                PROPOSAL_FILE.write_text(json.dumps(proposal, indent=2))
                L.append("📝 PROPOSAL STAGED → desk_config.proposed.json "
                         f"(≈{improve:+.2f}pp edge). Review & adopt manually; "
                         "live params UNCHANGED until you do.")
            else:
                L.append("✅ Current parameters are at/near optimal for the data "
                         "so far. No change proposed.")

    L.append("─" * 46)
    L.append("Signal-only research. No orders placed; no live parameter changed "
             "without your approval.")

    report = "\n".join(L)
    # Persist a dated report (history on disk, not memory).
    try:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        (REPORT_DIR / f"strategy_eval_{today}.txt").write_text(report)
    except OSError:
        pass
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
