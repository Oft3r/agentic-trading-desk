#!/usr/bin/env bash
# session-setup.sh — prove the Mandate order guard is alive before any order.
#
# 1. Installs jq if missing (the guard fails closed without it).
# 2. Marks hooks/mandate-order-guard.sh executable. If a user-level copy exists
#    (~/.claude/hooks/mandate-order-guard.sh, registered in ~/.claude/settings.json
#    so the guard also runs when the desk is loaded as a skill), it must be
#    byte-identical to this one: a stale copy is exactly how an Aug-26 guard
#    that let after-hours and limit orders through stayed live into October.
# 3. Probes every guard copy on both sides of the clock (pinned via MANDATE_FAKE_ET):
#    orders the Mandate prohibits must be DENIED, orders it permits must PASS.
#    Probing only for denials is not enough — a guard that blocks everything
#    looks identical to a working one from a single deny probe.
#
# Exit 0 and the line "order guard verified" means the guard is evaluating rules.
# Any other outcome means it is not: the session may still analyze, but must not
# execute orders. Wired as a SessionStart hook in .claude/settings.json (its
# stdout lands in the session context); in skill mode SKILL.md runs it by hand.

set -uo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
guard="$here/../hooks/mandate-order-guard.sh"

fail() { echo "ORDER GUARD NOT VERIFIED: $1"; echo "Do not execute orders this session."; exit 1; }

# --- 1. jq -------------------------------------------------------------------
if ! command -v jq >/dev/null 2>&1; then
  if [ "$(id -u)" -eq 0 ]; then
    apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq jq >/dev/null 2>&1
  elif command -v sudo >/dev/null 2>&1; then
    # -n: never prompt; an interactive password prompt would hang a scheduled run.
    sudo -n apt-get update -qq >/dev/null 2>&1 && sudo -n apt-get install -y -qq jq >/dev/null 2>&1
  fi
  command -v jq >/dev/null 2>&1 || fail "jq is not installed and could not be installed (no root / passwordless sudo, or no network to the package archive)."
fi

# --- 2. guard present and executable; user-level copy in sync --------------------
[ -f "$guard" ] || fail "guard script not found at $guard"
chmod +x "$guard" 2>/dev/null || true
[ -x "$guard" ] || fail "guard script is not executable and chmod failed"
guards=("$guard")
user_guard="${HOME:-/nonexistent}/.claude/hooks/mandate-order-guard.sh"
if [ -f "$user_guard" ]; then
  cmp -s "$guard" "$user_guard" \
    || fail "user-level guard $user_guard differs from the skill's copy ($guard). Update it: install -m 755 '$guard' '$user_guard'"
  guards+=("$user_guard")
fi

# --- 3. probes -----------------------------------------------------------------
RTH="1000:2"     # Tuesday 10:00 ET
AFTER="2000:2"   # Tuesday 20:00 ET
WEEKEND="1000:6" # Saturday 10:00 ET

# expect: deny | allow
probe() {
  local expect=$1 clock=$2 label=$3 input=$4 out rc
  out=$(printf '%s' "{\"tool_input\":$input}" | MANDATE_FAKE_ET=$clock bash "$g" 2>&1); rc=$?
  if [ "$rc" -ne 0 ]; then
    fail "probe '$label' crashed the guard $g (rc=$rc): $out"
  fi
  if [ "$expect" = "deny" ]; then
    printf '%s' "$out" | jq -e '.hookSpecificOutput.permissionDecision == "deny"' >/dev/null 2>&1 \
      || fail "probe '$label' should be DENIED but $g allowed it (output: '${out}')"
  else
    [ -z "$out" ] || fail "probe '$label' should be ALLOWED but $g denied it: $out"
  fi
  n=$((n + 1))
}

n=0
for g in "${guards[@]}"; do
# Prohibited — must be denied
probe deny "$RTH"     "stop_market"                 '{"type":"stop_market","stop_price":500,"quantity":1,"symbol":"SPY","side":"sell","time_in_force":"gfd"}'
probe deny "$RTH"     "limit during regular hours"  '{"type":"limit","limit_price":500,"quantity":1,"symbol":"SPY","side":"buy","time_in_force":"gfd"}'
probe deny "$RTH"     "market buy by quantity"      '{"type":"market","quantity":500,"symbol":"SPY","side":"buy","time_in_force":"gfd"}'
probe deny "$RTH"     "notional over cap"           '{"type":"market","dollar_amount":5000,"symbol":"SPY","side":"buy","time_in_force":"gfd"}'
probe deny "$RTH"     "time_in_force gtc"           '{"type":"market","dollar_amount":250,"symbol":"SPY","side":"buy","time_in_force":"gtc"}'
probe deny "$RTH"     "time_in_force missing"       '{"type":"market","dollar_amount":250,"symbol":"SPY","side":"buy"}'
probe deny "$RTH"     "extended_hours session"      '{"type":"market","dollar_amount":250,"symbol":"SPY","side":"buy","time_in_force":"gfd","market_hours":"extended_hours"}'
probe deny "$RTH"     "non-numeric dollar_amount"   '{"type":"market","dollar_amount":"abc","symbol":"SPY","side":"buy","time_in_force":"gfd"}'
probe deny "$AFTER"   "market after hours"          '{"type":"market","dollar_amount":250,"symbol":"SPY","side":"buy","time_in_force":"gfd"}'
probe deny "$AFTER"   "sell after hours"            '{"type":"market","quantity":0.4,"symbol":"DIA","side":"sell","time_in_force":"gfd"}'
probe deny "$AFTER"   "whole-share limit after hours" '{"type":"limit","limit_price":534.50,"quantity":1,"symbol":"DIA","side":"buy","time_in_force":"gfd","market_hours":"extended_hours"}'
probe deny "$WEEKEND" "market on weekend"           '{"type":"market","dollar_amount":250,"symbol":"SPY","side":"buy","time_in_force":"gfd"}'
# Permitted — must pass
probe allow "$RTH"    "market buy dollar_amount"    '{"type":"market","dollar_amount":250,"symbol":"DIA","side":"buy","time_in_force":"gfd","market_hours":"regular_hours"}'
probe allow "$RTH"    "market sell by quantity"     '{"type":"market","quantity":0.4,"symbol":"DIA","side":"sell","time_in_force":"gfd"}'
probe allow "$RTH"    "market sell dollar_amount"   '{"type":"market","dollar_amount":250,"symbol":"DIA","side":"sell","time_in_force":"gfd"}'

done

echo "order guard verified: ${#guards[@]} guard copy/copies x 15 probes = $n passed (12 prohibited orders denied, 3 permitted allowed each): ${guards[*]}"
exit 0
