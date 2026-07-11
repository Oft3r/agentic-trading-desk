#!/usr/bin/env python3
"""Unit test: regime_action confirmation counter (N=3)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_desk as R


def card(raw_long):
    # raw_long True means close above rising EMA200; False means below.
    return {"indicators": {"close": 760 if raw_long else 700,
                           "ema200": 750, "ema200_slope": 5}}


print("Scenario A: holding, bear signal must take 3 DAYS to flip (twice-daily safe)")
st = {"holding": True, "regime_state": True, "regime_run": 0, "regime_run_date": None}
for label, raw, day, expect in [
    ("d1 bear",  False, "2026-07-11", "HOLD"),
    ("d1 again", False, "2026-07-11", "HOLD"),   # same-day rerun: no double count
    ("d2 bear",  False, "2026-07-12", "HOLD"),
    ("d3 bear",  False, "2026-07-13", "EXIT"),   # 3rd distinct day -> flip
]:
    a = R.regime_action(card(raw), st.get("holding"), state=st, today=day)
    ok = "PASS" if a.startswith(expect) else f"FAIL(expected {expect})"
    print(f"  {label:9} -> {a:22} run={st['regime_run']} state={st['regime_state']}  {ok}")

print("Scenario B: a bullish day mid-streak resets the counter")
st2 = {"holding": True, "regime_state": True, "regime_run": 0, "regime_run_date": None}
for label, raw, day, expect in [
    ("d1 bear", False, "2026-07-11", "HOLD"),
    ("d2 BULL", True,  "2026-07-12", "HOLD"),
    ("d3 bear", False, "2026-07-13", "HOLD"),   # run restarted at 1, no flip
]:
    a = R.regime_action(card(raw), st2.get("holding"), state=st2, today=day)
    ok = "PASS" if a.startswith(expect) else f"FAIL(expected {expect})"
    print(f"  {label:9} -> {a:22} run={st2['regime_run']} state={st2['regime_state']}  {ok}")

print("Scenario C: flat, regime OFF, bull signal needs 3 days to re-enter")
st3 = {"holding": False, "regime_state": False, "regime_run": 0, "regime_run_date": None}
for label, raw, day, expect in [
    ("d1 bull", True, "2026-07-14", "STAY OUT"),
    ("d2 bull", True, "2026-07-15", "STAY OUT"),
    ("d3 bull", True, "2026-07-16", "RE-ENTRY"),
]:
    a = R.regime_action(card(raw), st3.get("holding"), state=st3, today=day)
    ok = "PASS" if a.startswith(expect) else f"FAIL(expected {expect})"
    print(f"  {label:9} -> {a:22} run={st3['regime_run']} state={st3['regime_state']}  {ok}")
