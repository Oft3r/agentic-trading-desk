---
name: agentic-trading-desk
description: >-
  Personal trading desk for short-term technical analysis on stocks/ETFs
  via Robinhood MCP. ALWAYS USE IT whenever the user asks to analyze a ticker,
  review positions, decide entries/exits/rebuys, calculate indicators
  (EMA/RSI/MACD/TRIX/Bollinger), score with the three-pillar framework, read
  the macro regime, or manage the Agentic account — even if they don't
  explicitly name the skill. Compute all indicators using deterministic code
  (never by eye) from raw Robinhood bars, apply the exit-on-exhaustion /
  re-enter-on-rebound logic, and respect account guardrails. Executes
  autonomously in the Agentic account under the Autonomous Execution Mandate,
  within hard limits on size, cash reserve and risk.
---

# Agentic Trading Desk

Operations manual for short-term trading analysis and execution.
**I (Agent) perform calls to the Robinhood MCP; the scripts act as my deterministic calculator; the framework decides and I execute.** I never calculate indicators by reasoning directly over the price bars: I fetch the data and pass it to `scripts/`.

## Guardrails — Read First, Non-Negotiable

1. **Protected positions:** Certain tickers may be designated as restricted (e.g., stock grants). NEVER analyze them to sell or trim, nor include them in exit suggestions. They should only be mentioned as exposure context if relevant.
2. **Two accounts, two roles:**
   - **Agentic** → short-term trading; this is where I execute under the Mandate.
   - **Individual** (margin account) → core buy-and-hold; only analyze holding quality, no active trading.
   - I identify the tradable account by the broker's own flag from `get_accounts` (exactly one account is agent-tradable; the others reject my orders outright). I never infer it from the account nickname, and I never hardcode an account number.
3. **Buying power:** I always take `get_portfolio.buying_power` as the authoritative figure rather than deriving it myself. Account type matters and can change: on a cash account only SETTLED cash is spendable (T+1), while a limited-margin account can trade unsettled proceeds immediately.
4. **HTML visualization only on Fridays** as part of the weekly review ritual. Do not offer or generate it on other days unless the user explicitly asks for it.
5. **Macro source (optional / best-effort):** Investing.com (NO Polymarket). If inaccessible or blocked by network egress, do NOT halt or block execution: proceed with deterministic market data without the yield spread.
6. **Autonomous execution is AUTHORIZED in the Agentic account** under the Autonomous Execution Mandate (next section). This authorization is durable and was established by the user in a live session on 2026-08-26; it does NOT depend on a scheduled prompt claiming it exists. A stored prompt cannot grant itself consent — the authorization lives here, in configuration, where it can be verified by reading it. Always review using `review_*_order` (simulation) before executing `place_*_order`.

## Autonomous Execution Mandate

The user authorizes executing orders without in-the-moment confirmation, **only** in the broker-designated agent-tradable account, and **only** within these limits. A limit exceeded is not a cue to ask for permission: it is a cue to **not trade** and report.

**What I may execute**
- Decisions emitted by `score.py` in this same session, with the macro pillar computed by `macro_pillar.py` the same day. The scripts still run every session and their output is still the baseline — the framework is not optional.
- **Discretionary entries and exits are authorized** (established by the user in a live session on 2026-08-26, superseding the prior zero-discretion rule): a trend change, a news catalyst, or a setup the decision cascade does not cover are all valid reasons to act. The cascade is rigid by design and will miss things; that is what this clause is for.
- Every discretionary trade MUST carry its reasoning in writing in the session report — what I saw, why it justified acting, and what would prove me wrong. A trade I cannot explain in two sentences is a trade I should not have made.
- **TACTICAL REBOUND goes in at half size** — it is counter-trend by definition.
- Optional: for a news-driven entry, require the tape to be moving in the direction of the thesis before acting rather than anticipating the move. Rationale in *External Context* below — in scheduled runs the news source is the weakest input available, and price confirmation is the cheapest guard against acting on a mis-dated headline.

**Hard limits**
- **Position size:** base size = **3% of account value**, and never more than **$1,200** per buy order. TACTICAL REBOUND = half of base size. Sells are not notional-capped: they can only reduce exposure (Robinhood offers no short selling, so a sell is bounded by the shares held), and a cap would block exiting a position that has grown past $1,200.
- Max **3 new positions** per session. Exits are uncapped.
- Minimum **15% of account value held in cash**. I never touch that reserve.
- **Orders only during regular hours — Mon–Fri 09:30–15:58 ET** (rule set by the user on 2026-10-19, replacing the 2026-08-26 after-hours limit rule). Outside that window I place **no orders at all**, buys or sells: I run the analysis, report what the framework emitted, and carry any action to the next regular session, re-running the analysis then. Why: every position in this account is fractional and base size (~3% of the account) is below one share of almost everything it trades, and Robinhood accepts fractional and dollar-based orders **only in `regular_hours`** (verified 2026-10-19 via `review_equity_order`: a fractional limit with `market_hours=extended_hours` is rejected — *"fractional and dollar-based orders are only allowed in regular_hours"*; the same order without `market_hours` is accepted, but as a `regular_hours` order that queues for the next open). The 2026-08-26 "fractional limits work after hours" verification had tested the second case, so an "after-hours" order was really an order queued for the next open at a price nobody re-evaluated.
- **Order shape:**
  - **`type: "market"`** only. If the analysis says enter, enter; a passive limit resting on a price is not an entry. `limit`, `stop_market` and `stop_limit` are prohibited — Robinhood also rejects stops on fractional shares, so stops in this account are manual levels I re-check each session, not resting orders.
  - **`market_hours`** omitted or `regular_hours`.
  - **Buys are sized in `dollar_amount`** — it is the natural way to size at 3% of the account, and the only way the notional is visible in the payload (a market buy by `quantity` carries no price, so the $1,200 cap could not be checked; the guard denies it). **Sells may use `quantity`** (`shares_available_for_sells`) so a whole position can be closed exactly.
  - **`time_in_force` set explicitly to `gfd`** (an omitted field is denied, not defaulted). No order outlives the session that placed it.
- **Order preflight.** Before every `place_equity_order` I print this table and check every row. If any row fails, I do NOT place the order — I report instead.

  | field | value | check |
  |---|---|---|
  | ET clock | … | inside Mon–Fri 09:30–15:58 (otherwise: no order) |
  | `side` | … | `buy` or `sell` |
  | `type` | … | `market` |
  | `market_hours` | … | omitted or `regular_hours` |
  | sizing | … | exactly one · buys `dollar_amount` · sells `dollar_amount` or `quantity` |
  | `time_in_force` | … | explicitly `gfd` |
  | notional (buys) | `dollar_amount` | ≤ 3% of account value and ≤ $1,200 |
- **The guard fails closed.** Without `jq`, with an unparseable payload, or with an unreadable clock it denies; the registered hook command wraps it so a missing or crashing script exits 2, which blocks the call (a plain non-zero exit would be a non-blocking error, i.e. ALLOW). Even so, I treat the preflight table above as the real check, not the hook — see *Session start* below for where the hook does and does not run.
- No new position in a single name within 2 sessions of confirmed earnings (`get_earnings_calendar`). ETFs exempt.
- I run `review_equity_order` with the exact parameters before every `place_equity_order`. **I abort and report** if `order_checks` is non-empty (I surface the alert verbatim), or if `quote_data.last_trade_price` is more than 1% away from the price the analysis used. A dollar-sized market order's review returns no share quantity or fill price — the quote is what I check. If the review returns `customer_approval_required: true`, the order goes to the user for approval in the Robinhood app: I report it as *pending approval*, not as executed.

**Session start — prove the guard is alive**

The Mandate is prose, and prose cannot enforce itself; `hooks/mandate-order-guard.sh` is what makes the order rules above mechanical. The failure mode that matters is that a guard which is unwired, non-executable, or missing `jq` looks exactly like a working one until an order slips through. That is not hypothetical: on 2026-08-26 a `stop_market` passed because `jq` was absent and the script exited 0 with no output, which the hook runner reads as ALLOW. So before the first `place_equity_order` of any session I run:

```bash
bash <skill-dir>/scripts/session-setup.sh
```

It installs `jq`, marks the guard executable, checks that the user-level copy (`~/.claude/hooks/mandate-order-guard.sh`, if present) is byte-identical to the skill's own, and runs **15 probes against every copy** — 12 orders the Mandate prohibits (stop, limit, market buy by quantity, over-cap notional, `gtc` or missing `time_in_force`, `extended_hours`, non-numeric size, any order after hours or on a weekend), which must be denied, and 3 it permits, which must pass. Probing only for denials is not enough: a guard that blocks *everything* is just as broken as one that blocks nothing, and from a single deny probe the two look identical. The probes pin the guard's clock via `MANDATE_FAKE_ET`, so both sides of the clock are tested whenever the session boots. Exit 0 with `order guard verified` means the guard is actually evaluating rules. **Any other outcome means it is not, and I do not execute orders that session** — I still run the analysis and report what the framework emitted, and I say plainly that execution was disabled and why.

**Where the hook actually runs — I state which case applies in the session report.** Hooks are a Claude Code feature, registered in a `settings.json`:
- **Claude Code on the user's machine, any project, skill mode included** → the user-level registration in `~/.claude/settings.json` (matcher `mcp__.*Robinhood__place_equity_order`, command pointing at `~/.claude/hooks/mandate-order-guard.sh`). Verified live 2026-10-19: a prohibited `place_equity_order` was denied by the hook before reaching Robinhood. The copy must be kept in sync — a stale August copy stayed registered into October; `session-setup.sh` now fails when they differ.
- **Claude Code with this repo as the project directory** (local or cloud) → `.claude/settings.json` in the repo also registers it, plus the `SessionStart` setup.
- **Any other client — the claude.ai app, Cowork, a scheduled run that opens a different repo, another agent runtime** → **no hook runs.** The probe above then only proves the script *would* deny if called. Nothing outside me is checking: the preflight table is the entire safety margin, and I fill it in row by row before every order and abort on any failing row. The only broker-side control that applies in every client is Robinhood's trade-approval setting (`get_trade_approval_setting` → `human_must_approve_trades`), which only the user can change in the app.

**If the self-audit shows agentic orders placed from an unguarded client, I say so plainly** — the orders were unguarded, whatever their contents turned out to be. (On 2026-08-25/26 three `market` orders — DIA, JPM ×2 — went through in skill mode with the guard unregistered. They would pass today's rules, but they passed then because *nothing was checking*, which is a different thing from being allowed.)

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

**Guard verification runs before any of this** — see *Session start — prove the guard is alive* in the Mandate. No order goes out in a session where `session-setup.sh` did not exit 0 printing `order guard verified`.

**Session self-audit (first call of every run).** `get_equity_orders` on the agent-tradable account with `agent_initiated=true` since the previous session (this also catches agent orders the user approved in the app, which `placed_agent="agentic"` alone would miss). An order is a Mandate breach by a prior run if its `type` is anything but `market`; if its `market_hours` is anything but `regular_hours`; if it was created outside Mon–Fri 09:30–15:58 ET; if its `time_in_force` is not `gfd`; if it is a buy without `dollar_based_amount`; or if it is a buy whose notional exceeds $1,200. I check all of these on every order returned, and surface any breach in the push notification rather than letting it pass silently. The broker's order history is the only durable record — the execution container is ephemeral, so a breach that is not surfaced today is lost.

**To analyze a ticker:**
1. `Robinhood:get_equity_historicals` → ~290 daily bars (closes). This is the input for `indicators.py`. Request a range that yields ≥220 bars (ideal for EMA200).
2. `Robinhood:get_equity_quotes` → live price / last session close.
3. If the user has a position: `Robinhood:get_equity_positions` (correct account) for size and P&L → set `holding` to correct value in scoring.

**For the Macro-Sentiment pillar (once per session, shared):**
1. `get_equity_historicals` for the 8 ETFs: SPY, RSP, IWM, HYG, LQD, TLT, XLY, XLP.
2. Attempt to fetch the 10Y-2Y yield spread from Investing.com (web) and inject it as `yield_spread`. If blocked by network egress policy or unavailable, omit it immediately without retrying: the script automatically redistributes its 20% weight among the other components and execution continues seamlessly without blocking.

**For portfolio management:**
- `Robinhood:get_portfolio` → market value and buying power.
- `Robinhood:get_equity_positions` → open positions by account.
- `Robinhood:get_realized_pnl` → realized P&L (useful for the Friday review).

## Computation Flow (Run via Code Execution)

Scripts are pure stdlib; they do not need internet access. They live in `scripts/` **inside this skill's own directory** — the runtime gives me the base path when it loads the skill. I never hardcode that path: it differs between environments.

Robinhood historicals are large and routinely exceed the tool's output limit; when that happens the result is written to a file and I get the path back. **I process them with code straight from that file** — I never load raw bars into context. I save the `get_equity_quotes` responses to a file as well (they are small and come back inline).

**Step 0 — prepare the inputs (every session, before anything else).**
```bash
python3 scripts/prepare_inputs.py --historicals <historicals files…> --quotes <quotes files…> --out <workdir>
```
It does two things I must never do by eye:
1. **Drops bars flagged `interpolated: true`.** `get_equity_historicals` synthesizes bars to fill gaps — including the whole span before a symbol's first trade. Found 2026-10-19: SPCX (first trade 2026-06-12) came back with 318 bars, 230 of them fake; scored over them it showed "EMA50>EMA200, EMA200↑", both invented. With real bars only it has 89 bars and the Trend pillar is marked `partial: 2/4 components`.
2. **Makes the last bar today's, from the broker's own dates** (`previous_close_date`, `venue_last_trade_time`, read in America/New_York — never a hard-coded UTC time, which breaks at every daylight-saving change). Daily bars do not include the current session until well after the close:
   - last real bar is today → nothing appended;
   - last bar is the prior session and the quote traded today ≥ 09:30 ET → `last_trade_price` appended: **final** after 16:00 ET, **provisional** during regular hours — then I label the scorecard `intraday (partial bar)`, because flags that hinge on the last bar (RSI turn, MACD histogram slope, %B, EMA20 reclaim) can flip by the close, and that uncertainty goes in the written reasoning of any trade taken on it;
   - pre-open, weekend or holiday → nothing appended;
   - last bar older than the prior session → **STALE, symbol excluded** (data does not reconcile — circuit breaker).
   `last_non_reg_trade_price` is never used: extended-hours prints are not closes.

Output: `ticker_<SYM>.json` per symbol, `macro_input.json` (the 8 ETFs, nested under `series`), and `closes.json` with the per-symbol report (real bars, dropped bars, appended mode, warnings). Every warning it prints goes into the session report.

**Step 1 — Macro (once per session).**
```bash
python3 scripts/macro_pillar.py <workdir>/macro_input.json --json
```
To add the 10Y-2Y spread, insert `"yield_spread": <float or series>` into `macro_input.json` first. Save the `pillar_score` (-2..+2). That number is the Macro-Sentiment score for ALL tickers today.

**Step 2 — Per ticker.**
```bash
python3 scripts/score.py <workdir>/ticker_<SYM>.json --macro-score <pillar_score> --holding <true|false>
```
This returns the three-pillar scorecard + decision (EXIT / TRIM, EXIT, RE-ENTRY (new cycle), TACTICAL REBOUND (counter-trend), HOLD (ride the cycle), HOLD (under review), WAIT (do not chase), STAY OUT / AVOID, HOLD / OBSERVE, OBSERVE) along with the exhaustion/bearish/rebound/death-cross flags that justify it. Passing the correct `holding` value is key: the decision cascade behaves differently depending on whether there is an open position or we are flat.

If only raw indicators are needed: `python3 scripts/indicators.py ticker_input.json`.

## Three-Pillar Framework (Standard Output Format)

Each pillar ranges from **-2 to +2**:
- **Trend** — EMA 20/50/200 structure + price position vs. EMAs + long-term slope.
- **Momentum** — Wilder's RSI-14 + MACD histogram + TRIX-15 vs. signal.
- **Macro-Sentiment** — from `macro_pillar.py` (cross-asset regime).

Report all three scores with their details, the total (-6..+6), and the decision framed in the logic of the Agentic account. **Ruling principle: short-term returns via capital rotation** — the cycle is enter on rebound → ride → exit on exhaustion → wait for next trigger. Accumulating positions is NOT the default (keeps capital trapped):

- **EXIT / TRIM** when bullish momentum is EXHAUSTED (RSI turning from overbought, MACD histogram shrinking, price stretched / near upper Bollinger band).
- **EXIT** when bearish momentum is RELENTLESS (true structural death-cross —EMA50<EMA200 and price<EMA50—, MACD histogram deepening, TRIX below zero).
- **RE-ENTRY (new cycle)** when flat, when a rebound/reversal arrives with a healthy EMA structure: valid entry trigger, confirm with candle/volume.
- **TACTICAL REBOUND (counter-trend)** when flat, when a rebound appears WITHIN a death-cross: a legitimate short-term opportunity, but with reduced size, close target (EMA20/EMA50 or middle Bollinger band), conditional exit on the next session / daily close if the rebound falters (since server-side stop orders are prohibited), and quick exit. It is not a new cycle and does not become a hold.
- **HOLD (ride the cycle)** when holding a position with positive trend+momentum: maintain while watching for exhaustion; the next expected action is exit with profit, not adding to position.
- **HOLD (under review)** when holding a position with weak structure or momentum, but no full exit trigger yet: maintain while monitoring for further deterioration.
- **WAIT (do not chase)** when flat with a healthy trend but no fresh trigger: entering mid-trend has poor R/R; wait for pullback to EMA20 and turn.
- **STAY OUT / AVOID**, **HOLD / OBSERVE** (when holding) or **OBSERVE** (when flat) as appropriate.

## External Context (News + Analysts — Non-Blocking / Optional)

All external context (news, analyst ratings, yield spread) is strictly optional, best-effort, and non-blocking:

1. **News/macro:** Investing.com (as defined in guardrails).
2. **Analyst ratings:** Google Finance beta —
   `https://www.google.com/finance/beta/quote/<TICKER>:<EXCHANGE>?tab=analysis`
   Returns: consensus (Buy/Hold/Sell), 12m price targets (avg/max/min), analyst table with dates, and last earnings vs. estimates.
3. **Network egress policy & non-blocking execution:** In remote scheduled runs, `WebFetch` to external sites (investing.com, google.com, cnbc.com, fred.stlouisfed.org, home.treasury.gov) is blocked by the network egress policy. **Never let an external fetch error or egress block halt, delay, or fail execution.** If external requests are blocked, time out, or return errors, skip them immediately without retrying and proceed purely with deterministic Robinhood data and local scripts. The macro pillar computes cleanly without the yield spread by redistributing its weight.
4. If successfully fetched, report this as **qualitative context alongside the three-pillar scorecard** — it never modifies numeric scores or trading decisions. Highlight: consensus, average target vs. current price (upside or price already past target), and recent rating changes (<2 weeks).

## Indicator Details (What the scripts calculate)

- **EMA** seed = SMA of the first N bars (TradingView convention / adjust=False).
- **RSI-14** with **Wilder's** smoothing (not simple moving average).
- **MACD** 12/26/9; report line, signal, histogram, and histogram slope.
- **TRIX-15** = % ROC of triple EMA, with EMA-9 signal.
- **Bollinger Bands** 20/2 with **population** standard deviation; report %B.
- Slopes are measured against 5 bars ago (configurable with `--slope-lookback`).

See `scripts/indicators.py` and `scripts/score.py` for exact implementation details. The math is checked by known-answer self-tests (constant-series EMA, monotonic-series RSI, MACD = EMA12 − EMA26, flat-series %B); `score.py` also asserts the decision on synthetic exhaustion and downtrend scenarios. Run any script with no arguments to execute its self-test — a non-zero exit means do not trust the numbers that session.

## What This Skill Does NOT Do

It is not a signal service, and it is not a proven strategy: it is the user's framework executed with discipline. It runs on a schedule and executes on its own within the Mandate, but **the framework has no backtest** — a trigger firing does not make it correct. It does not average down. It does not touch protected positions. It does not trade options, crypto or margin. It does not generate HTML outside of Fridays.
