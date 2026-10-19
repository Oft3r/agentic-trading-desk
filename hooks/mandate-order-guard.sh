#!/usr/bin/env bash
# mandate-order-guard.sh — PreToolUse guard for the Agentic Trading Desk Mandate.
#
# Enforces, deterministically, the order rules the Mandate states in prose but
# cannot itself enforce:
#
#   Orders only during regular hours (Mon-Fri 09:30-15:58 ET). Outside them,
#   every order is denied: the agentic account holds fractional positions and
#   sizes entries below one share, and Robinhood accepts fractional and
#   dollar-based orders only in regular_hours (verified 2026-10-19 via
#   review_equity_order: a fractional limit with market_hours=extended_hours is
#   rejected; without market_hours it is accepted as a regular_hours order that
#   queues for the next open). So nothing the desk trades can fill after hours.
#
#   type=market, market_hours unset or regular_hours, time_in_force=gfd.
#   BUYS sized in dollar_amount (the notional must be visible in the payload)
#   and <= $1,200. SELLS may use quantity (closing a whole position).
#   stop_market / stop_limit / limit denied.
#
# Notional cap applies to buys only. A sell can only reduce exposure: Robinhood
# does not support short selling, so a sell is bounded by the shares held, and
# capping it would block exiting a position that has grown past $1,200.
#
# Everything fails CLOSED: a field the guard cannot parse is a deny, never a pass.
#
# Holidays / half-days: this guard cannot know the market calendar. A market
# order on a weekday holiday passes the clock check and is queued by the broker.
# time_in_force=gfd is the backstop — it expires rather than filling into an
# open nobody evaluated.
#
# Install: PreToolUse hook, matcher "mcp__.*Robinhood__place_equity_order"
# (regex: the server prefix differs between installs, e.g. mcp__Robinhood__ vs
# mcp__claude_ai_Robinhood__). See README "Enforcing the Mandate" for where to
# register it; scripts/session-setup.sh installs jq and smoke-tests the guard.

set -uo pipefail

CAP=1200

# Fail closed on a missing dependency. Every check below is implemented with jq.
# Without it each field parse returns empty, deny() cannot emit its JSON either,
# and the script exits 0 with no output — which the hook runner reads as ALLOW.
if ! command -v jq >/dev/null 2>&1; then
  printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"MANDATE BLOCK: jq is not installed on this host, so the order guard cannot evaluate any rule and is failing closed. Install jq (apt-get install -y jq) in the environment setup script before enabling autonomous execution."}}'
  exit 0
fi

payload=$(cat)

deny() {
  jq -nc --arg r "$1" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: $r
    }
  }'
  exit 0
}

# Unparseable payload: deny rather than letting every field read as empty.
if ! printf '%s' "$payload" | jq -e '.tool_input | type == "object"' >/dev/null 2>&1; then
  deny "MANDATE BLOCK: hook payload is not valid JSON with a tool_input object; the guard cannot evaluate the order and is failing closed."
fi

field() { printf '%s' "$payload" | jq -r --arg k "$1" '.tool_input[$k] // empty | tostring'; }

otype=$(field type)
lprice=$(field limit_price)
damt=$(field dollar_amount)
qty=$(field quantity)
tif=$(field time_in_force)
mhours=$(field market_hours)
sym=$(field symbol); sym=${sym:-?}
side=$(field side)

# True if $1 parses as a finite number > 0.
is_pos_num() {
  jq -en --arg v "$1" '($v | tonumber) as $n | ($n > 0) and ($n | isinfinite | not)' >/dev/null 2>&1
}

# --- Session clock (America/New_York; DST handled by the zoneinfo database) --
# MANDATE_FAKE_ET, set to "HHMM:D" (D = 1..7, Mon..Sun), overrides the clock.
# It exists so session-setup.sh can probe both sides of the clock
# deterministically regardless of when the session boots. Never set it in
# normal operation.
if [ -n "${MANDATE_FAKE_ET:-}" ]; then
  now_hm=${MANDATE_FAKE_ET%%:*}
  dow=${MANDATE_FAKE_ET##*:}
else
  # Without zoneinfo data, `TZ=America/New_York date` silently falls back to UTC
  # and the regular-hours window shifts 4-5 hours (pre-market would read as
  # open). The zone abbreviation proves the conversion actually happened.
  tzabbr=$(TZ=America/New_York date +%Z)
  case "$tzabbr" in
    EDT|EST) ;;
    *) deny "MANDATE BLOCK: this host has no America/New_York zone data (date reports '$tzabbr'), so the ET session clock cannot be read; failing closed. Install tzdata." ;;
  esac
  read -r now_hm dow < <(TZ=America/New_York date '+%H%M %u')
fi

if ! [[ "$now_hm" =~ ^[0-9]{4}$ && "$dow" =~ ^[1-7]$ ]]; then
  deny "MANDATE BLOCK: could not read the ET session clock ('$now_hm', '$dow'); failing closed."
fi

# 10# forces base-10 so a leading zero (e.g. 0930) is not parsed as octal.
if [ "$dow" -gt 5 ] \
   || [ $((10#$now_hm)) -lt 930 ] || [ $((10#$now_hm)) -gt 1558 ]; then
  deny "MANDATE BLOCK ($side $sym): it is ${now_hm} ET on weekday ${dow} — outside regular hours (Mon-Fri 09:30-15:58 ET). The Mandate allows no orders outside regular hours: Robinhood accepts fractional and dollar-based orders only in regular_hours, so anything placed now either is rejected or queues for the next open at a price nobody evaluated. Re-run the analysis in the next regular session."
fi

# --- Order shape ------------------------------------------------------------
case "$otype" in
  market) ;;
  "") deny "MANDATE BLOCK ($side $sym): no order type set. Pass type=market." ;;
  *)  deny "MANDATE BLOCK ($side $sym): type '$otype' is not allowed. The Mandate permits only type=market during regular hours; stops are manual levels re-checked each session, and a passive limit resting on a price is not an entry." ;;
esac

case "$side" in
  buy|sell) ;;
  *) deny "MANDATE BLOCK (? $sym): side is '${side:-unset}'. It must be 'buy' or 'sell'." ;;
esac

case "$mhours" in
  ""|regular_hours) ;;
  *) deny "MANDATE BLOCK ($side $sym): market_hours is '$mhours'. Only regular_hours is allowed." ;;
esac

if [ "$tif" != "gfd" ]; then
  deny "MANDATE BLOCK ($side $sym): time_in_force is '${tif:-unset}'. The Mandate requires an explicit gfd — an order that outlives the session that placed it is an unattended fill at a price nobody evaluated."
fi

if [ -n "$lprice" ]; then
  deny "MANDATE BLOCK ($side $sym): limit_price is set on a market order. Remove it."
fi

for pair in "dollar_amount:$damt" "quantity:$qty"; do
  name=${pair%%:*}; val=${pair#*:}
  if [ -n "$val" ] && ! is_pos_num "$val"; then
    deny "MANDATE BLOCK ($side $sym): $name '$val' is not a positive number."
  fi
done

if [ -n "$damt" ] && [ -n "$qty" ]; then
  deny "MANDATE BLOCK ($side $sym): both dollar_amount and quantity are set. Size the order one way only."
fi
if [ -z "$damt" ] && [ -z "$qty" ]; then
  deny "MANDATE BLOCK ($side $sym): neither dollar_amount nor quantity is set."
fi

# --- Buys: dollar-sized and capped ($1,200) ----------------------------------
if [ "$side" = "buy" ]; then
  if [ -z "$damt" ]; then
    deny "MANDATE BLOCK (buy $sym): market buys must be sized in dollar_amount. A market buy by quantity has no price in the payload, so the \$${CAP} per-order cap cannot be checked."
  fi
  over=$(jq -nr --arg n "$damt" --argjson cap "$CAP" \
           'if ($n|tonumber) > $cap then "1" else "0" end' 2>/dev/null)
  case "$over" in
    0) ;;
    1) deny "MANDATE BLOCK (buy $sym): notional \$${damt} exceeds the \$${CAP} per-order cap." ;;
    *) deny "MANDATE BLOCK (buy $sym): could not compute the order notional; failing closed." ;;
  esac
fi

# Allowed: fall through silently so the normal permission flow proceeds.
exit 0
