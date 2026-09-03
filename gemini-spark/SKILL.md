---
name: agentic-trading-desk
description: >-
  Personal trading desk for short-term technical analysis on stocks/ETFs
  via Robinhood MCP. Computes deterministic indicators (EMA/RSI/MACD/TRIX/Bollinger)
  from raw bars, scores with the three-pillar framework, and evaluates cycle
  exhaustion and rebound trading setups within user-defined risk limits.
---

# Agentic Trading Desk

Operations manual for technical trading analysis and execution management.
**I (Agent) perform calls to the Robinhood MCP; the scripts act as my deterministic calculator; the framework evaluates and prepares execution.** I never calculate indicators by reasoning directly over the price bars: I fetch the data and pass it to `scripts/`.

## Guardrails — Read First, Non-Negotiable

1. **Protected positions:** Certain tickers may be designated as restricted (e.g., stock grants). NEVER analyze them to sell or trim, nor include them in exit suggestions. They should only be mentioned as exposure context if relevant.
2. **Two accounts, two roles:**
   - **Agentic** → short-term trading; this is where execution applies under the Mandate.
   - **Individual** (margin account) → core buy-and-hold; only analyze holding quality, no active trading.
   - I identify the tradable account by the broker's own flag from `get_accounts` (exactly one account is agent-tradable; the others reject orders outright). I never infer it from the account nickname, and I never hardcode an account number.
3. **Buying power:** I always take `get_portfolio.buying_power` as the authoritative figure rather than deriving it myself. Account type matters and can change: on a cash account only SETTLED cash is spendable (T+1), while a limited-margin account can trade unsettled proceeds immediately.
4. **HTML visualization only on Fridays** as part of the weekly review ritual. Do not offer or generate it on other days unless the user explicitly asks for it.
5. **Macro source (optional / best-effort):** Investing.com. If inaccessible or blocked by network egress, do NOT halt or block execution: proceed with deterministic market data without the yield spread.
6. **Execution Mandate:** All trade execution follows the pre-configured Execution Mandate limits. Always review using `review_*_order` (simulation) before executing `place_*_order`.

## Execution Mandate & Boundaries

The system operates strictly within these boundaries. A limit exceeded is a directive to **not trade** and report:

**What may be evaluated and executed**
- Only decisions emitted by `score.py` in this same session, with the macro pillar computed by `macro_pillar.py` the same day. Zero discretion: if the script did not emit EXIT, EXIT / TRIM, RE-ENTRY (new cycle) or TACTICAL REBOUND (counter-trend), there is no trade. A hunch is not a trigger.
- **TACTICAL REBOUND goes in at half size** — it is counter-trend by definition.

**Hard limits**
- Max **$1,200** per order.
- Max **3 new positions** per session. Exits are uncapped.
- Minimum **15% of account value held in cash**. I never touch that reserve.
- **Order types: `limit` and `market` are both supported**:
  - *Passive limit*: `limit_price` strictly inside the spread, within 0.3% of the last trade. Best price, may not fill.
  - *Marketable limit*: cross the spread but keep a hard cap — buy at `min(ask, last x 1.003)`, sell at `max(bid, last x 0.997)`. Fills like a market order in regular hours while bounding the worst case.
  - *Market*: fills at prevailing book price. **Regular hours only** — the Robinhood MCP rejects `market` in extended hours. Outside regular hours, use a limit order or wait for the next session.
  - In every run summary, state the order type chosen and the explicit rationale.
- **Quote staleness:** Before pricing any order, check `venue_bid_time` / `venue_ask_time` against current time and confirm a recent trade print. If top of book has not updated in **over 2 minutes** during regular hours, or there is no post-close print when the session is closed, the quote is stale: do NOT price against it and do not trade the name.
- **Order preflight:** Before every `place_equity_order`, check each of these fields:
  - `type`: `limit` or `market` only.
  - `session`: `market`, fractional, and `dollar_amount` all require regular hours.
  - `quote age`: top of book updated < 2 min ago; recent trade exists.
  - `limit_price`: limit orders only: within 0.3% of last trade.
  - `size`: `quantity` or `dollar_amount` (exactly one).
  - `notional`: ≤ $1,200.
- No new position in a single name within 2 sessions of confirmed earnings (`get_earnings_calendar`). ETFs exempt.
- Run `review_equity_order` before every `place_equity_order`. If the simulation differs by more than 1% in price or quantity from computed targets, **abort and report**.

**Circuit breakers**
- If account value at session open is down **more than 4%** against the prior close: no new buys that day. Exits remain allowed.
- If data does not reconcile — missing bars, an obviously stale quote, positions that do not match `get_equity_positions` — **do not trade**.

**Still prohibited**
- Protected positions, always.
- Any account other than the broker-designated agent-tradable one.
- Averaging down.
- Options, crypto, margin, and short selling.

## Robinhood MCP Recipe (Order of Calls)

Load the tools with `tool_search` before using them.

**Session self-audit (first call of every run):**
`get_equity_orders` on the agent-tradable account with `placed_agent="agentic"` since the previous session. Check `type`, `time_in_force`, and notional on every order returned, and surface any discrepancy in the summary report.

**To analyze a ticker:**
1. `Robinhood:get_equity_historicals` → ~290 daily bars (closes). Request a range yielding ≥220 bars (ideal for EMA200).
2. `Robinhood:get_equity_quotes` → live price / last session close.
3. If holding a position: `Robinhood:get_equity_positions` for size and P&L → set `holding: true` in scoring.

**For the Macro-Sentiment pillar (once per session, shared):**
1. `get_equity_historicals` for the 8 ETFs: SPY, RSP, IWM, HYG, LQD, TLT, XLY, XLP.
2. Attempt to fetch the 10Y-2Y yield spread from Investing.com (web) and inject it as `yield_spread`. If blocked by network egress policy or unavailable, omit it: `macro_pillar.py` automatically redistributes its 20% weight among the other components.

**For portfolio management:**
- `Robinhood:get_portfolio` → market value and buying power.
- `Robinhood:get_equity_positions` → open positions by account.
- `Robinhood:get_realized_pnl` → realized P&L.

## Computation Flow (Run via Code Execution)

Scripts are pure Python stdlib; they do not require external network access. They live in `scripts/` inside this skill's directory.

**Step 0 — append today's close if needed:**
If daily historical bars do not include the current session, append `last_trade_price` from `get_equity_quotes` as today's close before scoring.

**Step 1 — Macro (once per session):**
Assemble JSON with closes of the 8 ETFs + `yield_spread` (optional):
```bash
python3 scripts/macro_pillar.py macro_input.json --json
```
Save `pillar_score` (-2..+2). That number is the Macro-Sentiment score for all tickers today.

**Step 2 — Per ticker:**
Assemble `{symbol, close:[...], macro_score, holding}`:
```bash
python3 scripts/score.py ticker_input.json
```
This returns the three-pillar scorecard and decision.

## Three-Pillar Framework

Each pillar ranges from **-2 to +2**:
- **Trend** — EMA 20/50/200 structure + price position vs. EMAs + long-term slope.
- **Momentum** — Wilder's RSI-14 + MACD histogram + TRIX-15 vs. signal.
- **Macro-Sentiment** — from `macro_pillar.py` (cross-asset regime).

Decisions:
- **EXIT / TRIM**: Bullish momentum exhausted (RSI turning from overbought, MACD histogram shrinking, price near upper Bollinger band).
- **EXIT**: Bearish momentum relentless (structural weakness, negative MACD histogram, falling TRIX).
- **RE-ENTRY (new cycle)**: Flat account; rebound arrives with healthy EMA structure.
- **TACTICAL REBOUND (counter-trend)**: Flat account; rebound appears within a death-cross: reduced size, close target, conditional exit on next session / daily close if rebound falters.
- **HOLD (ride the cycle)**: Holding position with intact positive trend and momentum; maintain while watching for exhaustion.
- **HOLD (under review)**: Holding position; weak signals or adverse momentum, prepare to exit if conditions degrade.
- **WAIT (do not chase)**: Flat account; healthy trend but no fresh entry trigger.
- **STAY OUT / AVOID**: Flat account; relentless bearish trend, no rebound trigger.
- **HOLD / OBSERVE** or **OBSERVE**: Mixed signals; no clear trigger.

## External Context (News + Analysts — Non-Blocking / Optional)

All external context is optional, best-effort, and non-blocking:
1. **News/macro:** Investing.com (if available).
2. **Analyst ratings:** Google Finance (if available).
3. **Network egress safety:** If external web lookups fail, time out, or are blocked by network egress policies, skip them immediately and proceed with deterministic Robinhood data and local Python scripts.
4. Report qualitative context alongside the three-pillar scorecard — it never alters numerical indicator scores.

## Indicator Details

- **EMA** seed = SMA of the first N bars.
- **RSI-14** with **Wilder's** smoothing.
- **MACD** 12/26/9; line, signal, histogram.
- **TRIX-15** = % ROC of triple EMA, with EMA-9 signal.
- **Bollinger Bands** 20/2 with population standard deviation; report %B.
- Slopes are measured against 5 bars ago (`--slope-lookback`).

