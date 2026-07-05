#!/usr/bin/env python3
"""
auto_engine.py
==============
Batch scoring + risk management engine for auto-pilot mode.

Reads pre-fetched ticker data (close arrays), runs the full score.py stack
for every watchlist ticker, applies risk guards, and produces an action
signal for each. Called by:
  - FastAPI background task (analysis only; no direct Robinhood access)
  - Claude Code auto loop (full execution: fetch → score → execute via MCP)

Input JSON (--batch mode):
  {
    "as_of": "2026-07-05",
    "macro_score": 1,
    "portfolio_value": 50000,
    "cash_available": 10000,
    "tickers": {
      "AAPL": {"close": [...], "holding": false, "position_value": 0},
      "NVDA": {"close": [...], "holding": true,  "position_value": 5200}
    }
  }

Single-ticker input (--ticker mode, same schema as score.py):
  {"symbol": "AAPL", "close": [...], "macro_score": 1, "holding": false}

Config read from ../state/auto_config.json (or --config path).
State read/written from ../state/auto_status.json (or --state path).

Risk guards (all independently configurable):
  max_position_pct    - caps size_fraction so one name ≤ N% of portfolio
  min_score_entry     - absolute pillar total required to trigger an entry
  max_daily_trades    - circuit breaker: stop new entries after N trades today
  cooldown_bars       - bars a ticker must sit out after an exit/entry
  max_daily_loss_pct  - hard stop: disable auto if daily PnL < -X%
  dry_run             - never mark execute=True; signals only

stdlib only. Python 3.9+. Deterministic.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Allow importing score.py from the same directory
_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
import score as S  # noqa: E402

STATE_DIR = _HERE.parent / "state"
DEFAULT_CONFIG = STATE_DIR / "auto_config.json"
DEFAULT_STATUS = STATE_DIR / "auto_status.json"

# Actions that trigger a BUY
ENTRY_ACTIONS = {"RE-ENTRY (new cycle)", "TACTICAL REBOUND (counter-trend)"}
# Actions that trigger a SELL
EXIT_ACTIONS  = {"EXIT / TRIM", "EXIT"}
# Actions that are decisive (entry OR exit) — not just observe/hold
DECISIVE_ACTIONS = ENTRY_ACTIONS | EXIT_ACTIONS


def _load_json(path: Path) -> dict:
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# Risk guard evaluation
# --------------------------------------------------------------------------

def _risk_guard(symbol: str, action: str, card: dict,
                config: dict, status: dict,
                portfolio_value: float, position_value: float
                ) -> tuple[bool, str]:
    """
    Returns (allowed, reason). allowed=False → signal is blocked.
    Checks run in priority order; first failure wins.
    """
    today = _today()
    daily = status.get("daily_stats", {})
    dry   = config.get("dry_run", True)

    # Hard stop: daily loss limit
    if daily.get("hard_stopped") and daily.get("date") == today:
        return False, f"hard-stopped: daily loss limit hit ({daily.get('pnl_pct', 0):.2f}%)"

    # Hard stop: max daily trades (entries only)
    if action in ENTRY_ACTIONS:
        trades_today = daily.get("trades", 0) if daily.get("date") == today else 0
        max_t = int(config.get("max_daily_trades", 5))
        if trades_today >= max_t:
            return False, f"max daily trades reached ({trades_today}/{max_t})"

    # Min score for entries
    if action in ENTRY_ACTIONS:
        total = card.get("pillar_total") or 0
        min_sc = int(config.get("min_score_entry", 3))
        if total < min_sc:
            return False, f"score {total} < min_score_entry {min_sc}"

    # Cooldown
    cooldowns = status.get("cooldowns", {})
    cd_bars   = int(config.get("cooldown_bars", 5))
    if symbol in cooldowns:
        elapsed = cooldowns[symbol].get("bars_elapsed", 0)
        if elapsed < cd_bars:
            return False, f"cooldown: {elapsed}/{cd_bars} bars since last action"

    # Position size guard (entries): new position must be ≤ max_position_pct of portfolio
    if action in ENTRY_ACTIONS and portfolio_value > 0:
        max_pct = float(config.get("max_position_pct", 0.10))
        r = card.get("risk") or {}
        vol_frac = r.get("vol_target_fraction") or max_pct
        # cap fraction at the portfolio limit
        if vol_frac > max_pct:
            # allowed but capped — not a block
            pass

    # Dry run: never actually execute
    if dry:
        return True, "dry_run: signal generated, not executed"

    return True, ""


# --------------------------------------------------------------------------
# Cooldown tracker update
# --------------------------------------------------------------------------

def _update_cooldown(status: dict, symbol: str, acted: bool) -> None:
    cooldowns = status.setdefault("cooldowns", {})
    # Tick every existing cooldown by 1 bar
    for sym, cd in list(cooldowns.items()):
        cd["bars_elapsed"] = cd.get("bars_elapsed", 0) + 1
        if cd["bars_elapsed"] >= cd.get("cd_bars", 5):
            del cooldowns[sym]
    if acted:
        cooldowns[symbol] = {"bars_elapsed": 0, "cd_bars": status.get("_cd_bars", 5)}


# --------------------------------------------------------------------------
# Core batch scoring
# --------------------------------------------------------------------------

def score_batch(batch: dict, config: dict, status: dict) -> dict:
    """
    Score all tickers in `batch["tickers"]`, apply risk guards,
    return a signals report and updated status.
    """
    as_of = batch.get("as_of") or _now_iso()
    macro  = int(batch.get("macro_score", config.get("macro_score", 0)))
    pv     = float(batch.get("portfolio_value", config.get("portfolio_value", 0)))
    cash   = float(batch.get("cash_available",  config.get("cash_available",  0)))
    dry    = config.get("dry_run", True)

    signals: list[dict] = []
    blocked: list[dict] = []
    today   = _today()
    daily   = status.setdefault("daily_stats", {})
    if daily.get("date") != today:
        daily.update({"date": today, "trades": 0, "pnl_pct": 0.0, "hard_stopped": False})

    tickers = batch.get("tickers", {})
    for symbol, tdata in tickers.items():
        closes  = [float(x) for x in tdata.get("close", [])]
        holding = bool(tdata.get("holding", False))
        pos_val = float(tdata.get("position_value", 0))
        high    = [float(x) for x in tdata["high"]] if tdata.get("high") else None
        low     = [float(x) for x in tdata["low"]]  if tdata.get("low")  else None

        if len(closes) < 30:
            blocked.append({"symbol": symbol, "reason": "insufficient data (<30 bars)"})
            continue

        card = S.score_symbol(closes, macro_score=macro, symbol=symbol,
                              holding=holding, high=high, low=low, with_garch=False)
        action = card["decision"]["action"]
        r      = card.get("risk") or {}

        # Risk guard
        allowed, reason = _risk_guard(symbol, action, card, config, status, pv, pos_val)

        # Compute capped size fraction
        max_pct  = float(config.get("max_position_pct", 0.10))
        vol_frac = r.get("vol_target_fraction")
        if vol_frac is not None:
            vol_frac = min(vol_frac, max_pct)
        elif pv > 0:
            vol_frac = max_pct

        sig: dict = {
            "symbol":     symbol,
            "action":     action,
            "pillar_total": card["pillar_total"],
            "score":      {"trend": card["pillars"]["trend"]["score"],
                           "momentum": card["pillars"]["momentum"]["score"],
                           "macro": card["pillars"]["macro_sentiment"]["score"]},
            "decisive":   action in DECISIVE_ACTIONS,
            "holding":    holding,
            "close":      card["indicators"]["close"],
            "stop":       r.get("suggested_stop"),
            "stop_pct":   r.get("stop_distance_pct"),
            "size_fraction": vol_frac,
            "forecast_vol":  r.get("forecast_vol_annual"),
            "execute":    False,
            "blocked":    not allowed,
            "block_reason": reason,
            "as_of":      as_of,
        }

        if allowed and action in DECISIVE_ACTIONS:
            if not dry:
                sig["execute"] = True
                if action in ENTRY_ACTIONS:
                    daily["trades"] = daily.get("trades", 0) + 1
                _update_cooldown(status, symbol, acted=True)
            else:
                sig["execute"] = False
                sig["block_reason"] = sig["block_reason"] or "dry_run mode"
            signals.append(sig)
        elif allowed and action not in DECISIVE_ACTIONS:
            signals.append(sig)  # observe/hold/wait — no execute needed
        else:
            sig["decisive"] = action in DECISIVE_ACTIONS
            blocked.append(sig)
            _update_cooldown(status, symbol, acted=False)

    # Check daily loss limit
    loss_limit = float(config.get("max_daily_loss_pct", -2.0))
    if daily.get("pnl_pct", 0) < loss_limit:
        daily["hard_stopped"] = True
        status.setdefault("log", []).append({
            "ts": _now_iso(), "event": "HARD_STOP",
            "detail": f"daily PnL {daily['pnl_pct']:.2f}% < limit {loss_limit}%"
        })

    # Sort: decisive actionable first, then by abs(score)
    signals.sort(key=lambda s: (not s["decisive"], not s["execute"],
                                -abs(s["pillar_total"])))
    return {"as_of": as_of, "macro_score": macro, "dry_run": dry,
            "signals": signals, "blocked": blocked,
            "daily_stats": daily, "config_snapshot": {
                "min_score_entry": config.get("min_score_entry"),
                "max_daily_trades": config.get("max_daily_trades"),
                "cooldown_bars": config.get("cooldown_bars"),
            }}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Auto-pilot batch scorer + risk engine.")
    ap.add_argument("input", nargs="?",
                    help="Batch JSON or single-ticker JSON. No file: self-test.")
    ap.add_argument("--config",     default=str(DEFAULT_CONFIG))
    ap.add_argument("--state",      default=str(DEFAULT_STATUS))
    ap.add_argument("--save-state", action="store_true",
                    help="Write updated status back to --state path.")
    ap.add_argument("--json",       action="store_true")
    ap.add_argument("--dry-run",    action="store_true",
                    help="Override config: force dry_run=True.")
    args = ap.parse_args()

    config = _load_json(Path(args.config))
    status = _load_json(Path(args.state))
    if args.dry_run:
        config["dry_run"] = True

    if args.input:
        with open(args.input) as f:
            raw = json.load(f)
        # Single-ticker passthrough: wrap in batch format
        if "tickers" not in raw:
            sym = raw.get("symbol", "TICKER")
            raw = {"tickers": {sym: raw},
                   "macro_score": raw.get("macro_score", 0),
                   "as_of": _now_iso()}
    else:
        raw = _synthetic_batch()
        print("[self-test with synthetic data]\n", file=sys.stderr)

    result = score_batch(raw, config, status)

    if args.save_state:
        status.update({
            "last_scan": result["as_of"],
            "scan_count": status.get("scan_count", 0) + 1,
            "signals":    result["signals"],
            "blocked":    result["blocked"],
            "daily_stats": result["daily_stats"],
            "cooldowns":  status.get("cooldowns", {}),
        })
        _save_json(Path(args.state), status)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _render(result)
    return 0


def _render(r: dict) -> None:
    print(f"AUTO ENGINE  ·  {r['as_of']}  {'DRY RUN' if r['dry_run'] else 'LIVE'}")
    print("=" * 64)
    print(f"macro_score={r['macro_score']}  |  "
          f"trades_today={r['daily_stats'].get('trades',0)}  |  "
          f"hard_stopped={r['daily_stats'].get('hard_stopped',False)}")
    print(f"{'TICKER':<8} {'SCORE':>6} {'ACTION':<30} {'EXE':>5}  DETAIL")
    print("-" * 64)
    for s in r["signals"]:
        exe  = "YES" if s["execute"] else ("DRY" if not s["blocked"] and s["decisive"] else "—")
        note = s["block_reason"] or ""
        print(f"{s['symbol']:<8} {s['pillar_total']:>+6d}  {s['action']:<30} {exe:>5}  {note}")
    if r["blocked"]:
        print("-" * 64)
        print("BLOCKED:")
        for b in r["blocked"]:
            sym = b.get("symbol","?"); action = b.get("action",""); reason = b.get("reason") or b.get("block_reason","")
            print(f"  {sym:<8}  {action:<30}  {reason}")


def _synthetic_batch() -> dict:
    import math
    n = 262

    def _series(start, drift, amp, freq, phase):
        v = start
        out = []
        for i in range(n):
            v += drift + amp * math.sin(i / freq + phase)
            out.append(round(v, 2))
        return out

    return {
        "as_of": _now_iso(), "macro_score": 1,
        "portfolio_value": 50000, "cash_available": 12000,
        "tickers": {
            "AAPL": {"close": _series(170, 0.10, 3, 18, 0), "holding": False},
            "NVDA": {"close": _series(780, 0.5,  8, 12, 1) + [920, 948, 965], "holding": True},
            "TSLA": {"close": _series(200, -0.2, 12, 8,  2), "holding": True},
            "MSFT": {"close": _series(390, 0.15, 4, 20, 3), "holding": False},
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
