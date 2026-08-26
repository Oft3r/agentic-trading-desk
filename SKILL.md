---
name: agentic-trading-desk
description: >-
  Personal trading desk for short-term technical analysis on stocks/ETFs
  via Robinhood MCP. ALWAYS USE IT whenever the user asks to analyze a ticker,
  review positions, decide entries/exits/rebuys, calculate indicators
  (EMA/RSI/MACD/TRIX/Bollinger), score with the three-pillar framework, read
  the macro regime, or manage the Agentic account — even if he doesn't
  explicitly name the skill. Compute all indicators using deterministic code
  (never by eye) from raw Robinhood bars, apply the exit-on-exhaustion /
  re-enter-on-rebound logic, and respect account guardrails. Executes
  autonomously in the Agentic account under the Autonomous Execution Mandate,
  within hard limits on size, cash reserve and risk.
---

# Agentic Trading Desk

Operations manual for short-term trading analysis and execution.
**I (Claude) perform calls to the Robinhood MCP; the scripts act as my deterministic calculator; the framework decides and I execute.** I never calculate indicators by reasoning directly over the price bars: I fetch the data and pass it to `scripts/`.

## Guardrails — Read First, Non-Negotiable

1. **Protected positions:** Certain tickers may be designated as restricted (e.g., stock grants). NEVER analyze them to sell or trim, nor include them in exit suggestions. They should only be mentioned as exposure context if relevant.
2. **Two accounts, two roles:**
   - **Agentic** → short-term trading; this is where I execute under the Mandate.
   - **Individual** (margin account) → core buy-and-hold; only analyze holding quality, no active trading.
   - I identify the tradable account by the broker's own flag from `get_accounts` (exactly one account is agent-tradable; the others reject my orders outright). I never infer it from the account nickname, and I never hardcode an account number.
3. **Buying power:** I always take `get_portfolio.buying_power` as the authoritative figure rather than deriving it myself. Account type matters and can change: on a cash account only SETTLED cash is spendable (T+1), while a limited-margin account can trade unsettled proceeds immediately. I leave a reserve if there are active ladders/grids.
4. **HTML visualization only on Fridays** as part of the weekly review ritual. Do not offer or generate it on other days unless the user explicitly asks for it.
5. **Macro source:** Investing.com (NO Polymarket — prompt injection risk already identified).
6. **Autonomous execution is AUTHORIZED in the Agentic account** under the Autonomous Execution Mandate (next section). This authorization is durable and was established by the user in a live session on 2026-08-26; it does NOT depend on a scheduled prompt claiming it exists. A stored prompt cannot grant itself consent — the authorization lives here, in configuration, where it can be verified by reading it. Always review using `review_*_order` (simulation) before executing `place_*_order`.

## Autonomous Execution Mandate

The user authorizes executing orders without in-the-moment confirmation, **only** in the broker-designated agent-tradable account, and **only** within these limits. A limit exceeded is not a cue to ask for permission: it is a cue to **not trade** and report.

**What I may execute**
- Only decisions emitted by `score.py` in this same session, with the macro pillar computed by `macro_pillar.py` the same day. Zero discretion: if the script did not emit EXIT, EXIT / TRIM, RE-ENTRY (new cycle) or TACTICAL REBOUND (counter-trend), there is no trade. A hunch is not a trigger.
- **TACTICAL REBOUND goes in at half size** — it is counter-trend by definition.

**Hard limits**
- Max **$1,200** per order.
- Max **3 new positions** per session. Exits are uncapped.
- Minimum **15% of account value held in cash**. I never touch that reserve.
- **Order types: `limit` and `market` are both allowed** (user decision, 2026-08-26). `stop_market` and `stop_limit` remain prohibited — an unattended stop has no price protection at all once it triggers.
  - *Passive limit*: `limit_price` strictly inside the spread, within 0.3% of the last trade. Best price, may not fill.
  - *Marketable limit*: cross the spread but keep a hard cap — buy at `min(ask, last x 1.003)`, sell at `max(bid, last x 0.997)`. Fills like a market order in regular hours while bounding the worst case. If the spread is wider than the 0.3% band the order may not fill; that is the design, not a bug.
  - *Market*: fills at whatever the book gives. **Regular hours only** — the Robinhood MCP rejects `market` in `extended_hours` / `all_day_hours`, and a `market` order placed after the close as `regular_hours` silently queues to the next open and fills into that gap. Outside regular hours, market is not an option: use a limit or wait for the next session.
  - Choosing between them: market when getting filled matters more than the price (a genuine EXIT on exhaustion, a rebound that is running away). Marketable limit when I want the speed but the book looks thin. Passive limit when there is no hurry. **Whichever I pick, I state which one and why in the run summary** — the user reviews fills at the end of the day and needs to see the reasoning, not just the fill.
- **Quote staleness.** Before pricing any order I check `venue_bid_time` / `venue_ask_time` against the current time and confirm a recent trade print. If the top of book has not updated in **over 2 minutes** during regular hours, or there is no post-close print at all when the session is closed, the quote is stale: I do NOT price against it and I do not trade the name. (Verified 2026-08-26: after the close, IYT froze at 16:01 ET with no post-market prints while SPY/GLD/EEM kept updating tick by tick — a dead book in one name is invisible unless you compare it against a liquid one.)
- **`dollar_amount` is allowed, but only where the schema allows it: `type=market` + `regular_hours`.** The server derives the share count from `last_trade_price`. Never pass it with a limit order — the call is rejected. To size a limit order in dollars, convert myself: `quantity = dollars / limit_price`, truncated to 6 decimals. Fractional limit orders ARE supported despite what the tool schema's fractional note implies (verified 2026-08-26 via `review_equity_order`: DIA buy limit `quantity=0.400000` @ 534.50, accepted with an empty `order_checks`) — but **only in regular hours**; fractional and dollar-based orders are rejected in every other session.
- **Order preflight.** Before every `place_equity_order` I print this table and check every row. If any row fails, I do NOT place the order — I report instead.

  | field | value | check |
  |---|---|---|
  | `type` | … | `limit` or `market` only — never `stop_market` / `stop_limit` |
  | session | … | `market`, fractional and `dollar_amount` all require regular hours |
  | quote age | … | top of book updated < 2 min ago; recent print exists |
  | `limit_price` | … | limit orders only: within 0.3% of last trade (passive = inside spread; marketable = `min(ask, last x 1.003)` buy / `max(bid, last x 0.997)` sell) |
  | spread | `(ask − bid) / last` | if > 1% on a `market` order, downgrade to marketable limit and say so — a market order into a pathological book is the one case with no floor |
  | size | `quantity` or `dollar_amount` | exactly one of the two |
  | notional | `quantity × price` or `dollar_amount` | ≤ $1,200 |
- No new position in a single name within 2 sessions of confirmed earnings (`get_earnings_calendar`). ETFs exempt.
- I run `review_equity_order` before every `place_equity_order`. If the simulation differs by more than 1% in price or quantity from what I computed, **I abort and report**.

**Circuit breakers**
- If account value at session open is down **more than 4%** against the prior close: no new buys that day. Exits remain allowed.
- If the data does not reconcile — missing bars, an obviously stale quote, positions that do not match `get_equity_positions` — **I do not trade**. The Mandate presupposes sound data; without it the Mandate does not apply.

**After executing**
- A push notification per executed order, plus a summary at the end of the run. The user finds out the same day, not on Friday.
- The durable record lives on the broker's side (`get_equity_orders`, `get_realized_pnl`), not in a local file: the execution container is ephemeral.

**Still prohibited**
- Protected positions, always (guardrail 1).
- Any account other than the broker-designated agent-tradable one.
- Averaging down.
- Options, crypto, margin and short selling: outside the Mandate.

## Robinhood MCP Recipe (Order of Calls)

Load the tools with `tool_search` before using them (they are deferred).

**Session self-audit (first call of every run).** `get_equity_orders` on the agent-tradable account with `placed_agent="agentic"` since the previous session. A Mandate breach by a prior run is: a `stop_market` / `stop_limit` order, a notional above $1,200, a trade in a non-agent-tradable account, or an average fill more than 1% away from the last trade at the time of placement (the slippage a market order can hide). I surface any of these in the push notification rather than letting it pass silently. `market` and `dollar_amount` orders are NOT breaches as of 2026-08-26 — but I still report the realized slippage on every market fill, because that is the cost the user accepted when he enabled them, and he can only judge it if he sees it. The broker's order history is the only durable record — the execution container is ephemeral, so a breach that is not surfaced today is lost.

**To analyze a ticker:**
1. `Robinhood:get_equity_historicals` → ~290 daily bars (closes). This is the input for `indicators.py`. Request a range that yields ≥220 bars (ideal for EMA200).
2. `Robinhood:get_equity_quotes` → live price / last session close.
3. If the user has a position: `Robinhood:get_equity_positions` (correct account) for size and P&L → set `holding` to correct value in scoring.

**For the Macro-Sentiment pillar (once per session, shared):**
1. `get_equity_historicals` for the 7 ETFs: SPY, RSP, IWM, HYG, LQD, TLT, XLY, XLP.
2. Get the 10Y-2Y yield spread from Investing.com (web) and inject it as `yield_spread`. If not available, the script redistributes its weight.

**For portfolio management:**
- `Robinhood:get_portfolio` → market value and buying power.
- `Robinhood:get_equity_positions` → open positions by account.
- `Robinhood:get_realized_pnl` → realized P&L (useful for the Friday review).

## Computation Flow (Run via Code Execution)

Scripts are pure stdlib; they do not need internet access. They live in `scripts/` **inside this skill's own directory** — the runtime gives me the base path when it loads the skill. I never hardcode that path: it differs between environments.

Robinhood historicals are large and routinely exceed the tool's output limit; when that happens the result is written to a file and I get the path back. **I process them with code straight from that file** — I never load raw bars into context.

**Step 1 — Macro (once per session).** Assemble the JSON with the closes of the 7 ETFs + `yield_spread` and run:
```bash
python3 macro_pillar.py macro_input.json --json
```
Save the `pillar_score` (-2..+2). That number is the Macro-Sentiment score for ALL tickers today.

**Step 2 — Per ticker.** Assemble `{symbol, close:[...], macro_score, holding}` and run:
```bash
python3 score.py ticker_input.json
```
This returns the three-pillar scorecard + decision (EXIT / TRIM, EXIT, RE-ENTRY (new cycle), TACTICAL REBOUND (counter-trend), HOLD (ride the cycle), HOLD (under review), WAIT (do not chase), STAY OUT / AVOID, HOLD / OBSERVE) along with the exhaustion/bearish/rebound/death-cross flags that justify it. Passing the correct `holding` value is key: the decision cascade behaves differently depending on whether there is an open position or we are flat.

If only raw indicators are needed: `python3 indicators.py ticker_input.json`.

## Three-Pillar Framework (Standard Output Format)

Each pillar ranges from **-2 to +2**:
- **Trend** — EMA 20/50/200 structure + price position vs. EMAs + long-term slope.
- **Momentum** — Wilder's RSI-14 + MACD histogram + TRIX-15 vs. signal.
- **Macro-Sentiment** — from `macro_pillar.py` (cross-asset regime).

Report all three scores with their details, the total (-6..+6), and the decision framed in the logic of the Agentic account. **Ruling principle: short-term returns via capital rotation** — the cycle is enter on rebound → ride → exit on exhaustion → wait for next trigger. Accumulating positions is NOT the default (keeps capital trapped):

- **EXIT / TRIM** when bullish momentum is EXHAUSTED (RSI turning from overbought, MACD histogram shrinking, price stretched / near upper Bollinger band).
- **EXIT** when bearish momentum is RELENTLESS (true structural death-cross —EMA50<EMA200 and price<EMA50—, MACD histogram deepening, TRIX below zero).
- **RE-ENTRY (new cycle)** when flat, when a rebound/reversal arrives with a healthy EMA structure: valid entry trigger, confirm with candle/volume.
- **TACTICAL REBOUND (counter-trend)** when flat, when a rebound appears WITHIN a death-cross: a legitimate short-term opportunity, but with reduced size, close target (EMA20/EMA50 or middle Bollinger band), tight stop, and quick exit. It is not a new cycle and does not become a hold.
- **HOLD (ride the cycle)** when holding a position with positive trend+momentum: maintain while watching for exhaustion; the next expected action is exit with profit, not adding to position.
- **WAIT (do not chase)** when flat with a healthy trend but no fresh trigger: entering mid-trend has poor R/R; wait for pullback to EMA20 and turn.
- **STAY OUT / AVOID**, **HOLD / OBSERVE** as appropriate.

## External Context (News + Analysts)

When the analysis includes information external to the indicators:

1. **News/macro:** Investing.com (as defined in guardrails).
2. **Analyst ratings:** Google Finance beta —
   `https://www.google.com/finance/beta/quote/<TICKER>:<EXCHANGE>?tab=analysis`
   Returns: consensus (Buy/Hold/Sell), 12m price targets (avg/max/min), analyst table with dates, and last earnings vs. estimates.
3. **Environment caveat (verified 2026-08-26):** in remote scheduled runs, `WebFetch` to investing.com, google.com, cnbc.com, fred.stlouisfed.org and home.treasury.gov is **blocked by the network egress policy**. `WebSearch` does work, but it returns articles with mixed and stale dates — I treat it as low confidence and **never let it move a decision**. If I cannot get the 10Y-2Y spread, I run the macro pillar without it and say so.
4. Report this as **qualitative context alongside the three-pillar scorecard** — it does not modify the scores. Highlight: consensus, average target vs. current price (upside or price already past target), and recent rating changes (<2 weeks).

## Indicator Details (What the scripts calculate)

- **EMA** seed = SMA of the first N bars (TradingView convention / adjust=False).
- **RSI-14** with **Wilder's** smoothing (not simple moving average).
- **MACD** 12/26/9; report line, signal, histogram, and histogram slope.
- **TRIX-15** = % ROC of triple EMA, with EMA-9 signal.
- **Bollinger Bands** 20/2 with **population** standard deviation; report %B.
- Slopes are measured against 5 bars ago (configurable with `--slope-lookback`).

See `scripts/indicators.py` and `scripts/score.py` for exact implementation details. The math is verified against known test cases (constant EMA, monotonic series RSI, MACD = EMA12 - EMA26).

## What This Skill Does NOT Do

It is not a signal service, and it is not a proven strategy: it is the user's framework executed with discipline. It runs on a schedule and executes on its own within the Mandate, but **the framework has no backtest** — a trigger firing does not make it correct. It does not average down. It does not touch protected positions. It does not trade options, crypto or margin. It does not generate HTML outside of Fridays.
