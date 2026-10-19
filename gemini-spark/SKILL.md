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
- **Position size:** base size = **3% of account value**, and never more than **$1,200** per buy order. TACTICAL REBOUND = half of base size. Sells are not notional-capped: Robinhood offers no short selling, so a sell is bounded by the shares held.
- Max **3 new positions** per session. Exits are uncapped.
- Minimum **15% of account value held in cash**. I never touch that reserve.
- **Orders only during regular hours — Mon–Fri 09:30–15:58 ET.** Outside that window, place no orders at all; carry any action to the next regular session and re-run the analysis then. Robinhood accepts fractional and dollar-based orders only in `regular_hours` (a fractional limit with `market_hours=extended_hours` is rejected; without `market_hours` it queues for the next open), and base size is below one share of most names.
- **Order shape:** `type: "market"` only (no `limit`, `stop_market` or `stop_limit`; stops are manual levels re-checked each session) · `market_hours` omitted or `regular_hours` · buys sized in `dollar_amount`, sells may use `quantity` · `time_in_force` explicitly `gfd`.
  - In every run summary, state the order placed and the explicit rationale.
- **Quote staleness:** Before any order, check `venue_bid_time` / `venue_ask_time` and `venue_last_trade_time` against the current time. If top of book or the last trade is **more than 2 minutes** old, the quote is stale: do not trade the name this session.
- **Order preflight:** Before every `place_equity_order`, check each of these fields:
  - `ET clock`: inside Mon–Fri 09:30–15:58 (otherwise: no order).
  - `side`: `buy` or `sell`.
  - `type`: `market`.
  - `market_hours`: omitted or `regular_hours`.
  - `quote age`: top of book updated < 2 min ago; recent trade exists.
  - `size`: exactly one · buys `dollar_amount` · sells `dollar_amount` or `quantity`.
  - `time_in_force`: explicitly `gfd`.
  - `notional` (buys): `dollar_amount` ≤ 3% of account value and ≤ $1,200.
- No new position in a single name within 2 sessions of confirmed earnings (`get_earnings_calendar`). ETFs exempt.
- Run `review_equity_order` with the exact parameters before every `place_equity_order`. **Abort and report** if `order_checks` is non-empty (surface the alert verbatim), or if `quote_data.last_trade_price` is more than 1% away from the price the analysis used. A dollar-sized market order's review returns no share quantity or fill price — the quote is what to check. If the review returns `customer_approval_required: true`, the order goes to the user for approval in the Robinhood app: report it as *pending approval*, not as executed.

**Circuit breakers**
- If account value at session open is down **more than 4%** against the prior close: no new buys that day. Exits remain allowed.
- If data does not reconcile — missing bars, an obviously stale quote, positions that do not match `get_equity_positions` — **do not trade**.

**Still prohibited**
- Protected positions, always.
- Any account other than the broker-designated agent-tradable one.
- Averaging down.
- Options, crypto, margin, and short selling.

**No external guard runs in this version.** The preflight list above is the entire safety margin: check it field by field before every order and abort on any failing field. The only broker-side control is Robinhood's trade-approval setting (`get_trade_approval_setting` → `human_must_approve_trades`), which only the user can change in the app.

## Robinhood MCP Recipe (Order of Calls)

Discover and load the Robinhood MCP tools before using them.

**Session self-audit (first call of every run):**
`get_equity_orders` on the agent-tradable account with `agent_initiated=true` since the previous session (this also catches agent orders the user approved in the app). Check `side`, `type`, `market_hours`, creation time, `time_in_force`, and notional on every order returned — any non-`market` order, any order outside `regular_hours` or created outside Mon–Fri 09:30–15:58 ET, a non-`gfd` order, a buy without `dollar_based_amount`, or a buy over $1,200 is a discrepancy — and surface any discrepancy in the summary report.

**To analyze a ticker:**
1. `Robinhood:get_equity_historicals` → ~290 daily bars (closes). Request a range yielding ≥220 bars (ideal for EMA200).
2. `Robinhood:get_equity_quotes` → live price / last session close.
3. `Robinhood:get_equity_positions` (agent-tradable account) for size and P&L → `--holding true` if there is a position, `--holding false` if flat. The decision cascade differs between the two.

**For the Macro-Sentiment pillar (once per session, shared):**
1. `get_equity_historicals` for the 8 ETFs: SPY, RSP, IWM, HYG, LQD, TLT, XLY, XLP.
2. Attempt to fetch the 10Y-2Y yield spread from Investing.com (web) and inject it as `yield_spread`. If blocked by network egress policy or unavailable, omit it: `macro_pillar.py` automatically redistributes its 20% weight among the other components.

**For portfolio management:**
- `Robinhood:get_portfolio` → market value and buying power.
- `Robinhood:get_equity_positions` → open positions by account.
- `Robinhood:get_realized_pnl` → realized P&L.

## Computation Flow (Run via Code Execution)

Scripts are pure Python stdlib (3.9+); they do not require external network access or tzdata. They live in `scripts/` inside this skill's directory. Run any script with no arguments to execute its self-test; a non-zero exit means do not trust its numbers this session.

**Step 0 — prepare inputs (every session):**
Save the historicals and quotes responses to files, then:
```bash
python3 scripts/prepare_inputs.py --historicals <files…> --quotes <files…> --out <workdir>
```
It drops bars flagged `interpolated: true` (synthetic fill, e.g. the span before a recent IPO), appends today's `last_trade_price` from the broker's own dates (provisional during regular hours — label the scorecard `intraday (partial bar)` — final after 16:00 ET), excludes stale symbols, and writes `ticker_<SYM>.json`, `macro_input.json` and a report in `closes.json`. Report its warnings.

**Step 1 — Macro (once per session):**
```bash
python3 scripts/macro_pillar.py <workdir>/macro_input.json --json
```
To add the yield spread, insert `"yield_spread"` into `macro_input.json` first. Save `pillar_score` (-2..+2). That number is the Macro-Sentiment score for all tickers today.

**Step 2 — Per ticker:**
```bash
python3 scripts/score.py <workdir>/ticker_<SYM>.json --macro-score <pillar_score> --holding <true|false>
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

