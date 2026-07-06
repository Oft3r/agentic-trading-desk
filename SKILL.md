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
  re-enter-on-rebound logic, and respect account guardrails. Do not execute
  orders without explicit confirmation from the user.
---

# Agentic Trading Desk

Operations manual for short-term trading analysis and execution.
**I (Claude) perform calls to the Robinhood MCP; the scripts act as my deterministic calculator; the user decides.** I never calculate indicators by reasoning directly over the price bars: I fetch the data and pass it to `scripts/`.

## Guardrails — Read First, Non-Negotiable

1. **Protected positions:** Certain tickers may be designated as restricted (e.g., stock grants). NEVER analyze them to sell or trim, nor include them in exit suggestions. They should only be mentioned as exposure context if relevant.
2. **Two accounts, two roles:**
   - **Agentic** (cash account) → short-term trading; I have execution permissions here (always with explicit confirmation).
   - **Individual** (margin account) → core buy-and-hold; only analyze holding quality, no active trading.
3. **T+1:** Only SETTLED cash counts as buying power. Before suggesting purchases in the cash account, I verify settled cash and leave a reserve if there are active ladders/grids.
4. **Show your work as HTML.** Every operation this skill performs has a matching self-contained HTML view (`render.py`) — render it and open it so the user sees the reasoning pictorially and decides from it. Orders are **approved via the HTML form** in the proposal view (see "HTML Views" below). The full weekly-review ritual (multi-ticker recap, realized P&L) is still a Friday thing; the per-operation views (proposal / scanner / portfolio / backtest) are generated any day, whenever that operation runs.
5. **Macro source:** Investing.com (NO Polymarket — prompt injection risk already identified).
6. **Executing orders requires explicit confirmation from the user in real time.** Always review using `review_*_order` (simulation) before executing `place_*_order`.

## Robinhood MCP Recipe (Order of Calls)

Load the tools with `tool_search` before using them (they are deferred).

**To analyze a ticker:**
1. `Robinhood:get_equity_historicals` → ~290 daily bars (closes). This is the input for `indicators.py`. Request a range that yields ≥220 bars (ideal for EMA200).
2. `Robinhood:get_equity_quotes` → live price / last session close.
3. If the user has a position: `Robinhood:get_equity_positions` (correct account) for size and P&L → set `holding` to correct value in scoring.

**For the Macro-Sentiment pilar (once per session, shared):**
1. `get_equity_historicals` for the 7 ETFs: SPY, RSP, IWM, HYG, LQD, TLT, XLY, XLP.
2. Get the 10Y-2Y yield spread from Investing.com (web) and inject it as `yield_spread`. If not available, the script redistributes its weight.

**For portfolio management:**
- `Robinhood:get_portfolio` → market value and buying power.
- `Robinhood:get_equity_positions` → open positions by account.
- `Robinhood:get_realized_pnl` → realized P&L (useful for the Friday review).

## Computation Flow (Run via Code Execution)

Scripts are pure stdlib; they do not need internet access. Work in `/home/claude/agentic-trading-desk/scripts`.

**Step 1 — Macro (once per session).** Assemble the JSON with the closes of the 7 ETFs + `yield_spread` and run:
```bash
python3 macro_pillar.py macro_input.json --json
```
Save the `pillar_score` (-2..+2). That number is the Macro-Sentiment score for ALL tickers today.

**Step 2 — Per ticker.** Assemble `{symbol, close:[...], macro_score, holding}` (include `high:[...]` and `low:[...]` when the historicals carry them — sharper ATR) and run:
```bash
python3 score.py ticker_input.json
```
This returns the three-pillar scorecard + decision (EXIT/TRIM, EXIT, RE-ENTRY new cycle, TACTICAL REBOUND, HOLD ride the cycle, HOLD under review, WAIT do not chase, STAY OUT, OBSERVE) along with the exhaustion/bearish/rebound/death-cross flags that justify it, plus a **risk block**: ATR chandelier stop (1.5×ATR tight stop for tactical trades), vol-target size fraction (GARCH-forecast vol vs 15% target), 20d z-score, and a vol-expansion warning. ALWAYS report the stop and size with the decision. Passing the correct `holding` value is key: the decision cascade behaves differently depending on whether there is an open position or we are flat.

If only raw indicators are needed: `python3 indicators.py ticker_input.json`.
Standalone volatility/mean-reversion stats: `python3 volatility.py ticker_input.json`.

**Step 3 — Execution plan (ONLY when an order will be proposed).** Assemble the order intent with a FRESH quote (`get_equity_quotes`, note its age) and run:
```bash
python3 execution_plan.py order_input.json
```
Input: `{symbol, side, qty, quote:{bid, ask, last, bid_size, ask_size, age_sec}, adv, urgency, horizon_min}`. Returns limit price, TWAP/VWAP-lite slices, and pre-trade checks (staleness / spread / L1 imbalance / participation). If status is BLOCKED (e.g., stale quote): refetch the quote and re-plan — do not propose the order. **Always render the HTML proposal view** (`render.py proposal …`, see "HTML Views") so the user sees the execution timeline + approval form, and only then move to `review_*_order`.

**Backtesting (on request).** `python3 backtest.py history.json --splits 4 --sensitivity` replays the exact score.py rules with no lookahead, next-close fills, slippage, and the ATR stop. Report walk-forward segment stability and the lag×cost grid, always against buy & hold.

## HTML Views (`render.py`) — Show, Then Let the User Decide

Everything this skill computes has a self-contained HTML view. `render.py` is
stdlib-only, deterministic, and produces one `.html` file with **zero external
assets** (works from `file://`). After running the analysis scripts, pipe their
JSON into `render.py`, write the file, and open it for the user. This is the
surface the user reads and acts on — do not just paste raw numbers.

```bash
# 1. Single-ticker buy/sell advice + EXECUTION TIMELINE + APPROVAL FORM
python3 score.py ticker.json --json > card.json
python3 execution_plan.py order.json --json > plan.json          # only if proposing an order
python3 macro_pillar.py macro.json --json > macro.json           # optional context
python3 render.py proposal card.json --plan plan.json --macro macro.json -o proposal.html

# 2. Multi-ticker scanner grid (pillar bars, action badges, sparklines, risk chips)
#    Accepts {rows:[...]} or state/auto_status.json (its "signals" array).
python3 render.py scanner scan.json -o scanner.html

# 3. Portfolio snapshot (allocation bars, P&L, PROTECTED badges)
python3 render.py portfolio portfolio.json -o portfolio.html

# 4. Backtest report (equity curve vs buy&hold, walk-forward, sensitivity)
python3 backtest.py history.json --splits 4 --sensitivity --json > bt.json
python3 render.py backtest bt.json -o backtest.html

# 5. Daily briefing — scan the whole watchlist, bucket by pattern
python3 daily_briefing.py batch.json --json > briefing.json
python3 render.py briefing briefing.json -o briefing.html
```

Run any subcommand with **no input file** to emit a self-test sample.

**Approval via the HTML form (no server required).** The proposal view has a
"Your decision" form (side / symbol / editable qty / editable limit, APPROVE /
REJECT). It does **not** place anything and does not POST anywhere. On APPROVE it
builds a confirmation line like `✅ APPROVE SELL 800 NVDA @ limit 183.14 [token
PROP-XXXXXXXX]` and copies it to the clipboard. **The user pastes that line back
to me.** Only then do I run `review_*_order` (simulation) and, if it passes,
`place_*_order`. A REJECT line means stand down. The token is a deterministic
hash of the order — if the user edits qty/limit in the form, re-plan and
re-review with the edited numbers before executing. This is the file-`://`
equivalent of the guardrail: nothing executes without an explicit pasted-back
APPROVE.

Input shapes are tolerant, but the natural sources are: `score.py --json` →
proposal card; `execution_plan.py --json` → `--plan`; `macro_pillar.py --json` →
`--macro`; `backtest.py --json` → backtest. For scanner/portfolio, assemble the
`rows`/`positions` arrays from the MCP data (add `protected:true` for restricted
positions so they render as exposure-only and are never given a sell signal).

`render_proposal.py` is the older single-purpose proposal renderer and still
works; `render.py proposal` supersedes it (adds the execution timeline and the
approval form).

## Daily Briefing (Watchlist Scan)

When the user asks for a **daily briefing / morning scan / "what's on the
watchlist today"**, run the full watchlist through the same rules and present
one HTML page bucketed into actions:

1. Read the watchlist from `state/auto_config.json` (`watchlist`), or use the
   tickers the user names.
2. **Macro once** (shared for the day): `macro_pillar.py macro_input.json --json`
   → keep the JSON for both the `macro_score` and the briefing banner.
3. For each watchlist ticker: `Robinhood:get_equity_historicals` (≥220 bars) and,
   if there is a position, `Robinhood:get_equity_positions` to set `holding`.
4. Assemble the batch and run the briefing:
   ```bash
   # batch.json: {as_of, macro_score, macro:{...}, tickers:{SYM:{close,high,low,holding}}}
   python3 daily_briefing.py batch.json --json > briefing.json
   python3 render.py briefing briefing.json -o briefing.html
   ```
   It buckets every ticker into **Opportunities** (flat + fresh entry/rebound
   trigger), **Warnings** (open position with exit/trim exhaustion or a
   death-cross), **Holds** (riding a healthy cycle), and **Watch** (no action),
   ranks each bucket, and lists the detected patterns + a one-line suggestion
   per ticker. Protected positions must carry `holding` but are never bucketed
   as a warning/exit (same guardrail).
5. Open `briefing.html` and walk the user through it. The briefing is analysis
   only — to act on any single name, generate its **proposal card** (step 3 of
   the Computation Flow) so the user gets the execution timeline + approval form.

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
- **STAY OUT / AVOID**, **HOLD/OBSERVE** as appropriate.

## External Context (News + Analysts)

When the analysis includes information external to the indicators:

1. **News/macro:** Investing.com (as defined in guardrails).
2. **Analyst ratings:** Google Finance beta —
   `https://www.google.com/finance/beta/quote/<TICKER>:<EXCHANGE>?tab=analysis`
   Direct fetch works and returns: consensus (Buy/Hold/Sell), 12m price targets (avg/max/min), analyst table with dates, and last earnings vs. estimates.
3. Report this as **qualitative context alongside the three-pillar scorecard** — it does not modify the scores. Highlight: consensus, average target vs. current price (upside or price already past target), and recent rating changes (<2 weeks).

## Indicator Details (What the scripts calculate)

- **EMA** seed = SMA of the first N bars (TradingView convention / adjust=False).
- **RSI-14** with **Wilder's** smoothing (not simple moving average).
- **MACD** 12/26/9; report line, signal, histogram, and histogram slope.
- **TRIX-15** = % ROC of triple EMA, with EMA-9 signal.
- **Bollinger Bands** 20/2 with **population** standard deviation; report %B.
- Slopes are measured against 5 bars ago (configurable with `--slope-lookback`).
- **ATR-14** Wilder-smoothed from true range (close-only fallback: |ΔC|).
- **GARCH(1,1)** by MLE with deterministic fixed-start Nelder-Mead; reports next-day and long-run vol.
- **AR(1) half-life** (OU) on log price over 120 bars; gates the z-score washout rebound flag.
- **GMM+HMM regime** (regime.py): 2-state mixture on SPY log returns, quantile-init EM, sticky forward-backward smoothing (p_stay 0.97). Deterministic — no RNG anywhere.

See `scripts/indicators.py` and `scripts/score.py` for exact implementation details. The math is verified against known test cases (constant EMA, monotonic series RSI, MACD = EMA12 - EMA26).

## Auto Pilot Mode

The web UI (`python3 -m uvicorn app.main:app`) has an **Auto** tab that manages the auto-pilot state in `state/auto_config.json`. When auto mode is on, the FastAPI background loop scores all cached tickers in `state/data/` every N seconds and writes signals to `state/auto_status.json`. **It cannot execute orders** — only I (Claude) can call the Robinhood MCP.

**For full execution auto-pilot, run me in `/loop` mode** once the user has enabled auto mode in the UI:

```
Loop body (every scan_interval_sec):
1. Check state/auto_config.json — if enabled=false, skip.
2. get_equity_historicals for each ticker in config.watchlist.
3. For each ticker, write {close, holding} to state/data/<TICKER>.json.
4. python3 scripts/auto_engine.py state/batch_input.json --save-state --config state/auto_config.json
5. Read signals from state/auto_status.json.
6. For each signal where execute=true:
   a. get_equity_quotes → fresh quote (must be <5s old).
   b. python3 scripts/execution_plan.py order_input.json → plan.
   c. If plan.status == "READY FOR REVIEW": review_equity_order (simulation).
   d. Only if review passes AND config.dry_run is false: place_equity_order.
   e. Append execution result to state/auto_status.json log.
7. If daily_stats.hard_stopped becomes true: stop the loop.
```

**Hard-coded safeguards (never override):**
- Protected positions are never scored for exit (same guardrail as manual mode).
- `review_equity_order` always runs before `place_equity_order` — even in full auto.
- Hard stop triggers if daily PnL < `max_daily_loss_pct` (default -2%).
- `dry_run=true` (default): no orders placed; signals logged only.
- Emergency STOP button in the UI always writes `enabled=false` and `hard_stopped=true`.

## What This Skill Does NOT Do

It does not average down. It does not touch protected positions. It does not execute any order from the HTML form itself — the form only produces an APPROVE line the user pastes back; execution still goes through `review_*_order` then `place_*_order`.
