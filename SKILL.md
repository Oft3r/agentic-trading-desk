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
- Decisions emitted by `score.py` in this same session, with the macro pillar computed by `macro_pillar.py` the same day. The scripts still run every session and their output is still the baseline — the framework is not optional.
- **Discretionary entries and exits are authorized** (established by the user in a live session on 2026-08-26, superseding the prior zero-discretion rule): a trend change, a news catalyst, or a setup the decision cascade does not cover are all valid reasons to act. The cascade is rigid by design and will miss things; that is what this clause is for.
- Every discretionary trade MUST carry its reasoning in writing in the session report — what I saw, why it justified acting, and what would prove me wrong. A trade I cannot explain in two sentences is a trade I should not have made.
- **TACTICAL REBOUND goes in at half size** — it is counter-trend by definition.
- Optional (Claude's suggestion, delete this line if unwanted): for a news-driven entry, require the tape to be moving in the direction of the thesis before acting rather than anticipating the move. Rationale in *External Context* below — in scheduled runs the news source is the weakest input available, and price confirmation is the cheapest guard against acting on a mis-dated headline.

**Hard limits**
- Max **$1,200** per order.
- Max **3 new positions** per session. Exits are uncapped.
- Minimum **15% of account value held in cash**. I never touch that reserve.
- **Order type follows the session clock** (rule set by Eli on 2026-08-26, replacing the earlier limit-only rule):
  - **Regular hours — Mon–Fri 09:30–15:58 ET: `type: "market"`.** If the analysis says enter, enter; a passive limit resting on a price is not an entry. `dollar_amount` IS permitted here — it is the natural way to size at 3% of the account, and the MCP only accepts it with `type=market`.
  - **Outside regular hours: `type: "limit"` with an explicit `limit_price`, marketable** — at or just through the far side of the spread, within 0.3% of the last trade — and sized in SHARES (`quantity = dollars / limit_price`, truncated to 6 decimals). Never `dollar_amount` here: the MCP would silently convert it to a market order that fills at an unknown price hours later. Fractional limit orders ARE supported despite the tool schema's fractional note (verified 2026-08-26 via `review_equity_order`: DIA buy limit `quantity=0.400000` @ 534.50, accepted with an empty `order_checks`).
  - **`stop_market` and `stop_limit` are prohibited at all times.** Robinhood also rejects stops on fractional shares, so stops in this account are manual levels I re-check each session, not resting orders.
  - **`time_in_force` must be `gfd`.** No order outlives the session that placed it — an order queued overnight is an unattended fill at a price nobody evaluated.
  - Why the split: the original limit-only rule existed because unattended market orders have invisible slippage. Gating market orders on the session clock answers that directly — they fire only while the book is deep and the session is live, and everything outside that window stays priced.
- **Order preflight.** Before every `place_equity_order` I print this table and check every row. If any row fails, I do NOT place the order — I report instead.

  | field | value | check |
  |---|---|---|
  | ET clock | … | inside or outside Mon–Fri 09:30–15:58? |
  | `type` | … | `market` inside RTH · `limit` outside · never `stop_*` |
  | `limit_price` | … | outside RTH only: marketable, within 0.3% of last trade |
  | sizing | … | `dollar_amount` inside RTH · `quantity` in shares outside |
  | `time_in_force` | … | must be `gfd` |
  | notional | `dollar_amount` or `quantity × limit_price` | ≤ $1,200 |
- **The guard is not load-bearing unless `jq` is installed.** The hook implements every rule with `jq`; without it the script exits 0 with no output and the runner reads that as ALLOW (verified 2026-08-26 — an early revision let a `stop_market` through). It now fails closed, but a guard that denies everything is not a working setup either. See *Session start — prove the guard is alive* below; either way I treat the preflight table above as the real check, not the hook.
- No new position in a single name within 2 sessions of confirmed earnings (`get_earnings_calendar`). ETFs exempt.
- I run `review_equity_order` before every `place_equity_order`. If the simulation differs by more than 1% in price or quantity from what I computed, **I abort and report**.

**Session start — prove the guard is alive**

The Mandate is prose, and prose cannot enforce itself; `hooks/mandate-order-guard.sh` is what makes "limit orders only" mechanical. The failure mode that matters is that a guard which is unwired, non-executable, or missing `jq` looks exactly like a working one until an order slips through. That is not hypothetical: on 2026-08-26 a `stop_market` passed because `jq` was absent and the script exited 0 with no output, which the hook runner reads as ALLOW. So before the first `place_equity_order` of any session I run:

```bash
bash <skill-dir>/scripts/session-setup.sh
```

It installs `jq`, marks the guard executable, and runs **8 probes across both clock branches** — 6 orders the Mandate prohibits, which must be denied, and 2 it permits, which must pass. Probing only for denials is not enough: a guard that blocks *everything* is just as broken as one that blocks nothing, and from a single deny probe the two look identical. The probes pin the guard's session clock via `MANDATE_FAKE_ET` so both the regular-hours and after-hours branches are testable whenever the session happens to boot. Exit 0 with `order guard verified` means the guard is actually evaluating rules. **Any other outcome means it is not, and I do not execute orders that session** — I still run the analysis and report what the framework emitted, and I say plainly that execution was disabled and why. Analysis with a broken guard is useful; unattended execution with one is not.

One thing to be honest about with myself: `.claude/settings.json` only loads for a session whose *project directory* contains it. When the desk is loaded as an installed skill rather than opened as a project, the `PreToolUse` hook never registers, and the probe above verifies only that the script *would* deny if it were called. In that mode nothing outside me is checking, so the preflight table is the entire safety margin — I fill it in row by row before every order and abort on any failing row, rather than treating it as paperwork.

**This is not hypothetical, and I state it in the session report when it applies.** On 2026-08-26 a `market` order with `dollar_amount: 250` (DIA) and two `market` orders (JPM, 2026-08-25) went through in skill mode with the guard unregistered. Under the current clock rule all three were placed during regular hours and would now pass the guard on their merits — but they passed then because *nothing was checking*, which is a different thing from being allowed. If the session self-audit shows agentic orders and I am running in skill mode, I say so plainly: the orders were unguarded, whatever their contents turned out to be.

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

**Guard verification runs before any of this** — see *Session start — prove the guard is alive* in the Mandate. No order goes out in a session where the probe did not come back denied.

**Session self-audit (first call of every run).** `get_equity_orders` on the agent-tradable account with `placed_agent="agentic"` since the previous session. An order is a Mandate breach by a prior run if it is a `stop_market` or `stop_limit`; if it is a `market` order whose `market_hours` field is anything other than `regular_hours`; if it carries a `dollar_amount` outside regular hours; if its `time_in_force` is not `gfd`; or if its notional exceeds $1,200. I check `type`, `market_hours`, `time_in_force` and notional on every order returned, and surface any breach in the push notification rather than letting it pass silently. The broker's order history is the only durable record — the execution container is ephemeral, so a breach that is not surfaced today is lost.

**To analyze a ticker:**
1. `Robinhood:get_equity_historicals` → ~290 daily bars (closes). This is the input for `indicators.py`. Request a range that yields ≥220 bars (ideal for EMA200).
2. `Robinhood:get_equity_quotes` → live price / last session close.
3. If the user has a position: `Robinhood:get_equity_positions` (correct account) for size and P&L → set `holding` to correct value in scoring.

**For the Macro-Sentiment pillar (once per session, shared):**
1. `get_equity_historicals` for the 8 ETFs: SPY, RSP, IWM, HYG, LQD, TLT, XLY, XLP.
2. Get the 10Y-2Y yield spread from Investing.com (web) and inject it as `yield_spread`. If not available, the script redistributes its weight.

**For portfolio management:**
- `Robinhood:get_portfolio` → market value and buying power.
- `Robinhood:get_equity_positions` → open positions by account.
- `Robinhood:get_realized_pnl` → realized P&L (useful for the Friday review).

## Computation Flow (Run via Code Execution)

Scripts are pure stdlib; they do not need internet access. They live in `scripts/` **inside this skill's own directory** — the runtime gives me the base path when it loads the skill. I never hardcode that path: it differs between environments.

Robinhood historicals are large and routinely exceed the tool's output limit; when that happens the result is written to a file and I get the path back. **I process them with code straight from that file** — I never load raw bars into context.

**Step 0 — append today's close (every session, before anything else).** Daily bars from `get_equity_historicals` do NOT include the current session until well after the close: right after 16:00 ET the last bar is still *yesterday*, so scoring that series scores yesterday's market. For every symbol I take `get_equity_quotes` → `last_trade_price` (the ~19:59:59Z print is today's closing trade) and append it to the closes array with today's date, after asserting the last existing bar is the prior session. Never append `last_non_reg_trade_price` — extended-hours prints are not closes.

**Step 1 — Macro (once per session).** The input schema is `{"as_of": "YYYY-MM-DD", "series": {"SPY": [...], "RSP": [...], …}, "yield_spread": <float, optional>}`. The closes go **nested under `series`**, not at the top level — a flat `{"SPY": [...]}` raises `ValueError: No components with sufficient data`. Then run:
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
