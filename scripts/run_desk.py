#!/usr/bin/env python3
"""
run_desk.py — end-to-end orchestrator for the Agentic Trading Desk.
================================================================================
Executes the README "Example Workflow" for a single ticker WITHOUT Robinhood MCP,
using free deterministic public data (Yahoo Finance historicals + FRED 10Y-2Y),
then applies a Controlled Risk-Management overlay.

WORKFLOW (README section "Example Workflow"):
  1. Data Fetching     -> Yahoo daily historicals (~500 bars) + last close.
  2. Macro Pillar      -> 8 ETFs from Yahoo + 10Y-2Y spread from FRED -> macro_pillar.py
  3. Ticker Scoring    -> assemble {symbol, close, macro_score, holding} -> score.py
  4. Qualitative Ctx   -> best-effort (does NOT alter scores; non-fatal).
  5. Presentation      -> Scorecard + flags + suggested action + RISK block.

FAIL-SAFE (user requirement #5): if ANY *critical* step (1, 2, or 3) fails to
run, NO trade signal is emitted. The script prints an ABORT notice and the
position state is left untouched. Step 4 is reinforcement only and is non-fatal.

RISK MANAGEMENT (user requirements #6 & #7 — my documented modification to the
base strategy, informed by momentum/rotation best practice):
  * A persistent PAPER position is tracked in a state file so signals chain
    correctly across the twice-daily runs (entry -> ride -> exit).
  * HARD STOP-LOSS at -11% from entry: catastrophic-loss protection. Highest
    priority — overrides every framework signal.
  * TRAILING TAKE-PROFIT: once the trade is up >= TRAIL_ARM_PCT, an exit fires
    if price gives back >= TRAIL_GIVEBACK_PCT from its post-entry peak. This
    locks in ROI (success = exit after achieving a return), consistent with the
    framework's own "exit on exhaustion" philosophy.
  * The base three-pillar EXIT/RE-ENTRY signals are otherwise respected.

The deterministic engines (indicators.py / score.py / macro_pillar.py) are
UNCHANGED. All modifications live in this overlay so the validated math stays
intact and auditable.

Data source: Robinhood Agent MCP (get_equity_historicals / get_equity_quotes /
get_equity_positions) via rh_data.RHData, exactly as the README's Example
Workflow specifies. The 10Y-2Y yield spread is injected from FRED (T10Y2Y) —
Robinhood does not expose the curve, and the README already sources the spread
externally. FRED is fetched via `curl` subprocess (system python3 urllib has
broken SSL on this host).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Make the engine modules importable regardless of CWD.
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import indicators as I          # noqa: E402,F401  (used transitively by score)
import macro_pillar as M        # noqa: E402
import score as S               # noqa: E402
import rh_data                  # noqa: E402  (Robinhood MCP data provider)

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
TICKER = os.environ.get("DESK_TICKER", "SPY")
MACRO_ETFS = ["SPY", "RSP", "IWM", "HYG", "LQD", "TLT", "XLY", "XLP"]
LOOKBACK_DAYS = 800            # ~546 trading bars, well above the 220 EMA200 ideal

# Risk-management parameters (the modification) -----------------------------
# Externalized to desk_config.json so the strategy evaluator can PROPOSE tuning
# and the user can ADOPT it by editing one file — no code change, no memory
# write. If the file is missing or corrupt we fall back to safe defaults so a
# bad edit can never silently disable risk management.
CONFIG_FILE = SCRIPT_DIR / "desk_config.json"
_RISK_DEFAULTS = {
    "stop_loss_pct": 0.11,       # hard stop: exit if down >=11% from entry
    "trail_arm_pct": 0.05,       # arm the trailing stop once up >=5% from entry
    "trail_giveback_pct": 0.03,  # once armed, exit if price falls >=3% from peak
}


def _load_risk_config() -> dict:
    cfg = dict(_RISK_DEFAULTS)
    try:
        if CONFIG_FILE.exists():
            raw = json.loads(CONFIG_FILE.read_text())
            rp = raw.get("risk_params", raw) if isinstance(raw, dict) else {}
            for k in _RISK_DEFAULTS:
                v = rp.get(k)
                # Only accept sane positive fractions; ignore anything else.
                if isinstance(v, (int, float)) and 0 < float(v) < 1:
                    cfg[k] = float(v)
    except Exception:  # noqa: BLE001  — never let a bad config break a run
        return dict(_RISK_DEFAULTS)
    return cfg


_RISK = _load_risk_config()
STOP_LOSS_PCT = _RISK["stop_loss_pct"]
TRAIL_ARM_PCT = _RISK["trail_arm_pct"]
TRAIL_GIVEBACK_PCT = _RISK["trail_giveback_pct"]


def _load_strategy_mode() -> str:
    """Which decision engine drives entries/exits. Config-toggleable + safe.

    'regime_v4'   : validated trend-regime strategy — long while price is above
                    a RISING EMA200, flat otherwise, with the stop/trailing
                    overlay. Backtested 2014-2026 (see backtest.py): out-of-
                    sample Sharpe ~0.96, maxDD ~-10%, vs the original engine's
                    Sharpe ~0.02 / maxDD -24% (which LOST money out-of-sample).
    'three_pillar': the original counter-trend rebound engine (kept for audit /
                    fallback). Default to regime_v4 (the evidence-backed winner)
                    but fall back to three_pillar on any unexpected value.
    """
    try:
        if CONFIG_FILE.exists():
            raw = json.loads(CONFIG_FILE.read_text())
            mode = raw.get("strategy_mode", "regime_v4")
            if mode in ("regime_v4", "three_pillar"):
                return mode
    except Exception:  # noqa: BLE001
        pass
    return "regime_v4"


STRATEGY_MODE = _load_strategy_mode()

STATE_DIR = Path(os.environ.get("DESK_POS_DIR",
                                str(Path.home() / ".hermes" / "state")))
STATE_FILE = STATE_DIR / f"desk_position_{TICKER}.json"
SIGNAL_LOG = STATE_DIR / f"desk_signals_{TICKER}.jsonl"  # append-only signal record

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15")

ENTRY_ACTIONS = {"RE-ENTRY (new cycle)", "TACTICAL REBOUND (counter-trend)"}
EXIT_ACTIONS = {"EXIT / TRIM", "EXIT"}


def _load_confirm_days() -> int:
    try:
        raw = json.loads(CONFIG_FILE.read_text())
        n = raw.get("regime_confirm_days", 3)
        if isinstance(n, int) and 1 <= n <= 10:
            return n
    except Exception:  # noqa: BLE001
        pass
    return 3


REGIME_CONFIRM_DAYS = _load_confirm_days()


def regime_action(card: dict, holding: Optional[bool],
                  state: Optional[dict] = None,
                  today: Optional[str] = None) -> Optional[str]:
    """Translate the validated trend-regime signal into the engine's action
    vocabulary so the risk overlay + presentation work unchanged.

    Raw regime = LONG while close > EMA200 AND EMA200 is rising; FLAT otherwise.
    ANTI-WHIPSAW CONFIRMATION (validated in experiments_v2.py): the effective
    regime STATE flips only after REGIME_CONFIRM_DAYS consecutive *days* of the
    opposite raw signal. Backtest 2014-2026: N=3 lifts full-history Sharpe
    0.75→0.84, cuts maxDD -23.5%→-20.2%, trades 59→41; N=3 and N=4 both hold
    out-of-sample (TEST Sharpe 0.99/0.97 vs 0.96), N>=5 overfits TRAIN.

    Persistence lives in the position `state` dict (regime_state, regime_run,
    regime_run_date) so the twice-daily runs count each DAY once; an intraday
    flip-back resets the counter (conservative). Returns an action string, or
    None to defer to the three-pillar base action (EMA200 warmup missing).
    """
    ind = card.get("indicators", {})
    c = ind.get("close"); e200 = ind.get("ema200"); s200 = ind.get("ema200_slope")
    if c is None or e200 is None or s200 is None:
        return None  # not enough data — defer, fail-safe
    raw = (c > e200) and (s200 > 0)

    if state is None:                      # stateless fallback: instant flip
        effective = raw
    else:
        effective = state.get("regime_state")
        if effective is None:
            effective = raw                # first run under confirmation logic
            state["regime_state"] = effective
            state["regime_run"] = 0
            state["regime_run_date"] = None
        run = state.get("regime_run", 0)
        last_date = state.get("regime_run_date")
        if raw != effective:
            if today is not None and today != last_date:
                run += 1
                state["regime_run_date"] = today
            if run >= REGIME_CONFIRM_DAYS:
                effective = raw
                run = 0
                state["regime_run_date"] = None
        else:
            run = 0                        # aligned signal resets the streak
            state["regime_run_date"] = None
        state["regime_state"] = effective
        state["regime_run"] = run

    if effective:
        # Want to be long. If flat, emit an ENTRY; if already holding, ride.
        return "HOLD (ride the cycle)" if holding else "RE-ENTRY (new cycle)"
    # Out of regime: want to be flat. If holding, EXIT; if flat, stay out.
    return "EXIT" if holding else "STAY OUT / AVOID"

# Reuse the provider's StepError so the fail-safe catches everything uniformly.
StepError = rh_data.StepError


# --------------------------------------------------------------------------
# FRED yield-spread fetch (curl subprocess; retry on transient failure)
# --------------------------------------------------------------------------
def _curl(url: str, tries: int = 3, timeout: int = 25) -> str:
    last = ""
    for attempt in range(1, tries + 1):
        try:
            r = subprocess.run(
                ["curl", "-s", "--fail", "--max-time", str(timeout), url],
                capture_output=True, text=True,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout
            last = f"rc={r.returncode} err={r.stderr.strip()[:120]}"
        except Exception as e:  # noqa: BLE001
            last = str(e)
        time.sleep(1.5 * attempt)
    raise StepError(f"curl failed for {url} ({last})")


def fred_yield_spread(n_tail: int = 60) -> list[float]:
    """Last `n_tail` daily observations of the 10Y-2Y spread (T10Y2Y)."""
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=T10Y2Y"
    raw = _curl(url)
    vals: list[float] = []
    for line in raw.splitlines()[1:]:
        parts = line.split(",")
        if len(parts) == 2 and parts[1] not in (".", ""):
            try:
                vals.append(float(parts[1]))
            except ValueError:
                pass
    if not vals:
        raise StepError("FRED yield-spread parse produced no values")
    return vals[-n_tail:]


# --------------------------------------------------------------------------
# Position state (paper) — persistence for risk overlay
# --------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:  # noqa: BLE001
            pass
    return {"holding": False, "entry_price": None, "entry_date": None,
            "peak_price": None, "history": []}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def log_signal(today: str, price: float, card: dict, macro, risk: dict,
               base_action: str) -> None:
    """Append one record per run so the end-of-day evaluator can grade signals.

    Records the emitted action, the entry price of THIS signal, the pillar
    scores, and a UTC timestamp. One line = one signal (11am ET or 3pm ET).
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "date": today,
        "symbol": TICKER,
        "price": round(price, 4),
        "action": risk["final_action"],
        "base_action": base_action,
        "pillar_total": card.get("pillar_total"),
        "pillars": {k: card["pillars"][k]["score"]
                    for k in ("trend", "momentum", "macro_sentiment")},
        "macro_regime": getattr(macro, "regime", None),
        "opened": risk.get("opened", False),
        "closed": risk.get("closed", False),
        "unreal_pct": risk.get("unreal_pct"),
        "stop_level": risk.get("stop_level"),
    }
    with open(SIGNAL_LOG, "a") as f:
        f.write(json.dumps(rec) + "\n")


# --------------------------------------------------------------------------
# Risk overlay: decide the FINAL action given the base framework decision
# --------------------------------------------------------------------------
def apply_risk_overlay(state: dict, price: float, today: str,
                       base_action: str) -> dict:
    """
    Returns dict: {final_action, risk_reason, unreal_pct, stop_level,
                   trail_status, opened, closed, realized_pct}
    Mutates `state` (position open/close, peak update) but does NOT save.
    """
    out = {"opened": False, "closed": False, "realized_pct": None,
           "risk_reason": None}

    if state.get("holding"):
        entry = state["entry_price"]
        peak = max(state.get("peak_price") or entry, price)
        state["peak_price"] = peak
        unreal = price / entry - 1.0
        gain_from_entry_peak = peak / entry - 1.0
        dd_from_peak = price / peak - 1.0
        armed = gain_from_entry_peak >= TRAIL_ARM_PCT

        final = None
        reason = None
        if unreal <= -STOP_LOSS_PCT:
            final = "EXIT — STOP-LOSS HIT"
            reason = (f"Hard stop: down {unreal*100:.1f}% from entry "
                      f"(limit -{STOP_LOSS_PCT*100:.0f}%). Capital protection "
                      f"overrides all framework signals.")
        elif armed and dd_from_peak <= -TRAIL_GIVEBACK_PCT:
            final = "EXIT — TRAILING STOP (lock ROI)"
            reason = (f"Trailing take-profit: peak gain was "
                      f"+{gain_from_entry_peak*100:.1f}%, price gave back "
                      f"{dd_from_peak*100:.1f}% from peak. Locking in "
                      f"+{unreal*100:.1f}% ROI.")
        elif base_action in EXIT_ACTIONS:
            final = base_action
            reason = ("Framework exit trigger (three-pillar exhaustion/bearish). "
                      f"Realizing {unreal*100:+.1f}%.")

        if final and final.startswith("EXIT"):
            state["holding"] = False
            state["history"].append({
                "entry_price": entry, "entry_date": state["entry_date"],
                "exit_price": round(price, 4), "exit_date": today,
                "pnl_pct": round(unreal * 100, 2), "reason": final,
            })
            state["entry_price"] = None
            state["entry_date"] = None
            state["peak_price"] = None
            out.update(closed=True, realized_pct=round(unreal * 100, 2))
        else:
            # No risk/framework exit -> keep holding, defer to base action.
            final = base_action
            reason = "Position within risk limits; ride per framework."

        out.update(final_action=final, risk_reason=reason,
                   unreal_pct=round(unreal * 100, 2),
                   stop_level=round(entry * (1 - STOP_LOSS_PCT), 4),
                   trail_status=("ARMED" if armed else "not armed"))
        return out

    # ---- Flat ----
    if base_action in ENTRY_ACTIONS:
        state["holding"] = True
        state["entry_price"] = round(price, 4)
        state["entry_date"] = today
        state["peak_price"] = round(price, 4)
        out.update(opened=True, final_action=base_action,
                   risk_reason=(f"Opening paper position at {price:.4f}. "
                                f"Hard stop set at {price*(1-STOP_LOSS_PCT):.4f} "
                                f"(-{STOP_LOSS_PCT*100:.0f}%); trailing profit "
                                f"arms at +{TRAIL_ARM_PCT*100:.0f}%."),
                   unreal_pct=0.0,
                   stop_level=round(price * (1 - STOP_LOSS_PCT), 4),
                   trail_status="not armed")
        return out

    out.update(final_action=base_action,
               risk_reason="Flat; no entry trigger. No position at risk.",
               unreal_pct=None, stop_level=None, trail_status="flat")
    return out


# --------------------------------------------------------------------------
# Presentation
# --------------------------------------------------------------------------
def actionable_levels(card: dict, state: dict, price: float,
                      peak: Optional[float]) -> list[str]:
    """Concrete PRICE LEVELS the user can act on (limit orders / alerts),
    derived deterministically from the live indicators + position state.
    Every level answers 'at what price does the signal change?'."""
    L = ["🎯 ACTIONABLE LEVELS"]
    ind = card.get("indicators", {})
    e200 = ind.get("ema200")
    holding = state.get("holding")
    regime_state = state.get("regime_state")
    run = state.get("regime_run", 0)

    if e200 is not None:
        rel = (price / e200 - 1.0) * 100
        L.append(f"  • Regime line (EMA200): {e200:.2f}  "
                 f"(price {rel:+.1f}% {'above' if rel >= 0 else 'below'})")
        if regime_state:
            L.append(f"  • Regime flips OFF: {REGIME_CONFIRM_DAYS} consecutive "
                     f"closes < {e200:.2f}"
                     + (f"  ⏳ streak {run}/{REGIME_CONFIRM_DAYS} in progress"
                        if run > 0 else ""))
        elif regime_state is False:
            L.append(f"  • Regime flips ON: {REGIME_CONFIRM_DAYS} consecutive "
                     f"closes > {e200:.2f} with EMA200 rising"
                     + (f"  ⏳ streak {run}/{REGIME_CONFIRM_DAYS} in progress"
                        if run > 0 else ""))
    if holding and state.get("entry_price"):
        entry = state["entry_price"]
        pk = peak or entry
        L.append(f"  • Hard stop (sell alert): {entry * (1 - STOP_LOSS_PCT):.2f}")
        arm_level = entry * (1 + TRAIL_ARM_PCT)
        if pk >= arm_level:
            L.append(f"  • Trailing exit (armed): sell if price < "
                     f"{pk * (1 - TRAIL_GIVEBACK_PCT):.2f} "
                     f"(peak {pk:.2f} − {TRAIL_GIVEBACK_PCT*100:.0f}%)")
        else:
            L.append(f"  • Trailing arms at: {arm_level:.2f} "
                     f"(+{TRAIL_ARM_PCT*100:.0f}% from entry {entry:.2f})")
    elif not holding and e200 is not None and regime_state is False:
        L.append(f"  • Watch for entry: price reclaiming {e200:.2f} starts the "
                 f"{REGIME_CONFIRM_DAYS}-day confirmation clock")
    return L


def build_report(card: dict, macro: "M.MacroResult", risk: dict,
                 price: float, qual_note: str,
                 state: Optional[dict] = None) -> str:
    L = []
    L.append(f"📊 AGENTIC TRADING DESK — {TICKER}")
    L.append(f"Analyze {TICKER} for a potential entry")
    L.append(datetime.now().strftime("%a %Y-%m-%d %H:%M %Z"))
    L.append("")
    L.append(S.render(card))
    L.append("")
    L.append(f"Macro regime: {macro.regime} "
             f"(composite {macro.composite:+.2f}, pillar {macro.pillar_score:+d})")
    L.append("")
    L.append("─" * 54)
    L.append("🛡️  CONTROLLED RISK MANAGEMENT")
    ra = risk["final_action"]
    L.append(f"  ► SUGGESTED ACTION: {ra}")
    if risk.get("opened"):
        L.append(f"  • Paper ENTRY opened @ {price:.4f}")
    if risk.get("closed"):
        L.append(f"  • Paper position CLOSED @ {price:.4f}  "
                 f"(realized {risk['realized_pct']:+.2f}%)")
    if risk.get("unreal_pct") is not None and not risk.get("closed"):
        L.append(f"  • Unrealized P&L: {risk['unreal_pct']:+.2f}%")
    if risk.get("stop_level") is not None:
        L.append(f"  • Hard stop-loss level: {risk['stop_level']:.4f} "
                 f"(-{STOP_LOSS_PCT*100:.0f}% catastrophic protection)")
    L.append(f"  • Trailing take-profit: {risk['trail_status']}")
    if risk.get("risk_reason"):
        L.append(f"  • {risk['risk_reason']}")
    L.append("─" * 54)
    if state is not None and STRATEGY_MODE == "regime_v4":
        L.extend(actionable_levels(card, state, price, state.get("peak_price")))
        L.append("─" * 54)
    if qual_note:
        L.append(f"Qualitative context: {qual_note}")
    L.append("Reminder: signal only — you decide & approve every order.")
    return "\n".join(L)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def run() -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state = load_state()
    rh_position = None
    try:
        # STEP 1 — Data Fetching (critical): Robinhood MCP historicals + quote
        data = rh_data.RHData()                       # connects + MCP handshake
        close = data.daily_closes(TICKER, LOOKBACK_DAYS)
        quote = data.last_quote(TICKER)               # live price / last close
        price = quote["price"]
        # append the live price as the most-recent bar if it's a new session
        if close and abs(price - close[-1]) / close[-1] > 1e-9:
            close = close + [price]
        rh_position = data.holding(TICKER)            # real RH position (context)

        # STEP 2 — Macro Pillar (critical): 8 ETFs from RH + FRED yield spread
        series = data.multi_daily_closes(MACRO_ETFS, LOOKBACK_DAYS)
        spread = fred_yield_spread()
        macro_input = {"as_of": today, "yield_spread": spread, "series": series}
        macro = M.score_macro(macro_input)
        macro_score = macro.pillar_score

        # STEP 3 — Ticker Scoring (critical). The framework `holding` flag is
        # driven by our persisted paper position so the enter→ride→exit signal
        # cycle stays internally coherent across the twice-daily runs.
        card = S.score_symbol(close, macro_score=macro_score,
                              symbol=TICKER, holding=state.get("holding"))
        base_action = card["decision"]["action"]

        # STRATEGY SELECTION: in the validated 'regime_v4' mode, the trend-regime
        # signal DRIVES entries/exits (it beat the original counter-trend engine
        # out-of-sample by a wide margin — see backtest.py). The three-pillar
        # scorecard is still computed and shown for context. 'three_pillar' mode
        # keeps the original behavior. Fail-safe: if the regime signal can't be
        # computed (EMA200 warmup), we defer to the three-pillar base_action.
        if STRATEGY_MODE == "regime_v4":
            ra = regime_action(card, state.get("holding"),
                               state=state, today=today)
            if ra is not None:
                base_action = ra
                card["decision"]["action"] = ra
                card["decision"]["rationale"] = (
                    "Trend-regime strategy (validated): "
                    + ("price above a rising EMA200 → be long."
                       if ra in ("HOLD (ride the cycle)", "RE-ENTRY (new cycle)")
                       else "price below/at a falling EMA200 → stand aside."))
                card["decision"]["framing"] = (
                    "Regime_v4: position sizing follows the long/flat trend gate; "
                    "the risk overlay (hard stop + trailing take-profit) still "
                    "applies. Three-pillar scores shown above are context only.")

    except StepError as e:
        # FAIL-SAFE: a critical workflow step did not run -> NO signal emitted.
        print(f"⚠️ AGENTIC TRADING DESK — {TICKER}: RUN ABORTED\n"
              f"A required Example-Workflow step failed: {e}\n"
              f"No trade signal emitted this run; position state unchanged.")
        return 0  # exit 0 so the notice is delivered, but no signal is produced

    # STEP 4 — Qualitative context (non-fatal reinforcement)
    qbits = ["news/analyst reinforcement omitted (does not affect the "
             "deterministic score)"]
    if rh_position is True:
        qbits.append("NOTE: a real Robinhood position in this symbol exists")
    elif rh_position is False:
        qbits.append("no real Robinhood position held")
    qual_note = "; ".join(qbits)

    # STEP 5 — Risk overlay + presentation
    risk = apply_risk_overlay(state, price, today, base_action)
    save_state(state)
    log_signal(today, price, card, macro, risk, base_action)
    print(build_report(card, macro, risk, price, qual_note, state=state))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
