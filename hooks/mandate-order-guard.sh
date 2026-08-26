#!/usr/bin/env bash
# mandate-order-guard.sh — PreToolUse guard for the Agentic Trading Desk Mandate.
#
# Enforces, deterministically, the order-type rule the Mandate states in prose but
# cannot itself enforce. The rule (set by Eli 2026-08-26) is clock-dependent:
#
#   Regular hours (Mon-Fri 09:30-15:58 ET) -> type=market. dollar_amount allowed.
#   Outside regular hours                  -> type=limit with an explicit
#                                             limit_price, sized in SHARES.
#                                             dollar_amount forbidden, because the
#                                             Robinhood MCP only accepts it with
#                                             type=market and would silently turn a
#                                             priced order into an unattended
#                                             market fill hours later.
#   Always                                 -> stop_market / stop_limit denied,
#                                             time_in_force must be gfd,
#                                             notional <= $1,200.
#
# The clock split is the whole point: market orders fire only while the book is
# deep and the session is live; everything else stays priced.
#
# Holidays: this guard cannot know the market calendar. A market order on a
# weekday holiday passes the clock check and is queued by the broker.
# time_in_force=gfd is the backstop — it expires rather than filling into an
# open nobody evaluated.
#
# Install: PreToolUse hook, matcher "mcp__Robinhood__place_equity_order".
# Wired by .claude/settings.json in this skill; scripts/session-setup.sh installs jq
# and smoke-tests both branches of this guard on every session boot.

set -uo pipefail

# Fail closed on a missing dependency. Every check below is implemented with jq.
# Without it each field parse returns empty, deny() cannot emit its JSON either,
# and the script exits 0 with no output — which the hook runner reads as ALLOW.
# A guard that silently permits everything when a dependency is missing is worse
# than no guard, so check jq first and deny without using it.
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

otype=$(printf '%s' "$payload" | jq -r '.tool_input.type // empty')
lprice=$(printf '%s' "$payload" | jq -r '.tool_input.limit_price // empty')
damt=$(printf '%s'  "$payload" | jq -r '.tool_input.dollar_amount // empty')
qty=$(printf '%s'   "$payload" | jq -r '.tool_input.quantity // empty')
tif=$(printf '%s'   "$payload" | jq -r '.tool_input.time_in_force // empty')
sym=$(printf '%s'   "$payload" | jq -r '.tool_input.symbol // "?"')
side=$(printf '%s'  "$payload" | jq -r '.tool_input.side // "?"')

# --- Session clock (America/New_York; DST handled by the zoneinfo database) --
# MANDATE_FAKE_ET, set to "HHMM:D" (D = 1..7, Mon..Sun), overrides the clock.
# It exists so session-setup.sh can probe BOTH branches deterministically
# regardless of when the session boots. Never set in normal operation.
if [ -n "${MANDATE_FAKE_ET:-}" ]; then
  now_hm=${MANDATE_FAKE_ET%%:*}
  dow=${MANDATE_FAKE_ET##*:}
else
  now_hm=$(TZ=America/New_York date +%H%M)
  dow=$(TZ=America/New_York date +%u)
fi

# 10# forces base-10 so a leading zero (e.g. 0930) is not parsed as octal.
rth=0
if [ "$dow" -ge 1 ] && [ "$dow" -le 5 ] \
   && [ $((10#$now_hm)) -ge 930 ] && [ $((10#$now_hm)) -le 1558 ]; then
  rth=1
fi

# --- Always prohibited ------------------------------------------------------
case "$otype" in
  stop_market|stop_limit)
    deny "MANDATE BLOCK ($side $sym): '$otype' is prohibited at all times. Robinhood also rejects stops on fractional shares; stops in this account are manual levels re-checked each session, not resting orders."
    ;;
esac

if [ -z "$otype" ]; then
  deny "MANDATE BLOCK ($side $sym): no order type set. Pass type=market during regular hours (Mon-Fri 09:30-15:58 ET), or type=limit with an explicit limit_price outside them."
fi

if [ -n "$tif" ] && [ "$tif" != "gfd" ]; then
  deny "MANDATE BLOCK ($side $sym): time_in_force is '$tif'. The Mandate requires gfd — an order that outlives the session that placed it is an unattended fill at a price nobody evaluated."
fi

# --- Clock-dependent rules --------------------------------------------------
if [ "$rth" -eq 1 ]; then
  if [ "$otype" != "market" ]; then
    deny "MANDATE BLOCK ($side $sym): it is ${now_hm} ET on weekday ${dow} — regular hours — where the Mandate calls for type=market. Got '$otype'. If the analysis says enter, enter; a passive limit resting on a price is not an entry."
  fi
else
  if [ "$otype" != "limit" ]; then
    deny "MANDATE BLOCK ($side $sym): it is ${now_hm} ET on weekday ${dow} — outside regular hours (Mon-Fri 09:30-15:58 ET) — where only marketable limit orders are permitted. Got '$otype'. Nobody is watching the fill and extended-hours books are thin, so the order must carry its own price."
  fi
  if [ -z "$lprice" ]; then
    deny "MANDATE BLOCK ($side $sym): type=limit with no limit_price, outside regular hours. Pass an explicit limit_price at or just through the far side of the spread, within 0.3% of the last trade."
  fi
  if [ -n "$damt" ]; then
    deny "MANDATE BLOCK ($side $sym): dollar_amount is set outside regular hours. The Robinhood MCP accepts dollar_amount only with type=market, so this would silently become an unattended market order. Size in shares instead: quantity = dollars / limit_price, truncated to 6 decimals."
  fi
fi

# --- Notional cap ($1,200) --------------------------------------------------
# Enforced here rather than left to prose: it is the one limit checkable from the
# payload alone, and the one whose breach costs real money.
notional=""
if [ -n "$damt" ]; then
  notional="$damt"
elif [ -n "$qty" ] && [ -n "$lprice" ]; then
  notional=$(jq -nr --arg q "$qty" --arg p "$lprice" '($q|tonumber) * ($p|tonumber)' 2>/dev/null)
fi

if [ -n "$notional" ]; then
  over=$(jq -nr --arg n "$notional" 'if ($n|tonumber) > 1200 then "1" else "0" end' 2>/dev/null)
  if [ "$over" = "1" ]; then
    deny "MANDATE BLOCK ($side $sym): notional \$${notional} exceeds the \$1,200 per-order cap."
  fi
fi

# Allowed: fall through silently so the normal permission flow proceeds.
exit 0
