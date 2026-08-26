#!/usr/bin/env bash
# mandate-order-guard.sh — PreToolUse guard for the Agentic Trading Desk Mandate.
#
# Enforces, deterministically, the limits the Mandate states in prose but cannot
# itself enforce. Reads the PreToolUse hook payload on stdin and denies the tool
# call when a hard limit is violated.
#
# Updated 2026-08-26: `market` orders and `dollar_amount` sizing are ALLOWED by
# user decision, so the guard no longer blocks them. What it still enforces:
#   - stop_market / stop_limit are prohibited (no price protection once triggered)
#   - limit orders must carry an explicit limit_price
#   - dollar_amount only with type=market (the MCP schema rejects it otherwise;
#     failing here gives a clear reason instead of an opaque API error)
#   - notional ceiling of $1,200 per order
#
# Install: PreToolUse hook, matcher "mcp__Robinhood__place_equity_order".

set -uo pipefail

MAX_NOTIONAL=1200

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
sym=$(printf '%s'   "$payload" | jq -r '.tool_input.symbol // "?"')
side=$(printf '%s'  "$payload" | jq -r '.tool_input.side // "?"')

case "$otype" in
  stop_market|stop_limit)
    deny "MANDATE BLOCK ($side $sym): order type '$otype' is prohibited. An unattended stop has no price protection once it triggers — it becomes a market order at the worst possible moment, with nobody watching. Use limit or market instead."
    ;;
  limit)
    [ -n "$lprice" ] || deny "MANDATE BLOCK ($side $sym): type=limit with no limit_price. Pass an explicit limit_price within 0.3% of the last trade."
    [ -z "$damt" ]   || deny "MANDATE BLOCK ($side $sym): dollar_amount is set on a limit order. The Robinhood MCP accepts dollar_amount only with type=market. Size in shares instead: quantity = dollar_amount / limit_price (up to 6 decimals)."
    ;;
  market)
    : # allowed as of 2026-08-26 (user decision)
    ;;
  "")
    deny "MANDATE BLOCK ($side $sym): order type is unset. Pass type=limit or type=market explicitly."
    ;;
  *)
    deny "MANDATE BLOCK ($side $sym): unrecognized order type '$otype'. Only limit and market are permitted."
    ;;
esac

# Notional ceiling. dollar_amount is the notional directly; otherwise
# quantity x limit_price. A market order sized in shares carries no price in the
# payload, so it cannot be checked here — the SKILL.md preflight covers that case.
notional=""
if [ -n "$damt" ]; then
  notional="$damt"
elif [ -n "$qty" ] && [ -n "$lprice" ]; then
  notional=$(awk -v q="$qty" -v p="$lprice" 'BEGIN{printf "%.2f", q*p}')
fi

if [ -n "$notional" ]; then
  over=$(awk -v n="$notional" -v m="$MAX_NOTIONAL" 'BEGIN{print (n>m) ? 1 : 0}')
  [ "$over" = "0" ] || deny "MANDATE BLOCK ($side $sym): notional \$$notional exceeds the \$$MAX_NOTIONAL per-order ceiling. Reduce the size and re-issue."
fi

# Allowed: fall through silently so the normal permission flow proceeds.
exit 0
