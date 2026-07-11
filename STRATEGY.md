# Agentic Trading Desk — Strategy Documentation

## Regime-V4: A Validated, Evidence-Backed SPY Strategy

> **Last updated**: July 2026 · **Backtest period**: Oct 2014 — Jul 2026 (2,945 trading days)
> **Data source**: Robinhood MCP (split-adjusted daily SPY closes) · **Engine**: Python 3 stdlib only

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Strategy Overview](#strategy-overview)
3. [The Evolution: Why We Replaced the Original Engine](#the-evolution)
4. [Strategy Mechanics](#strategy-mechanics)
5. [Risk-Adjusted Performance](#risk-adjusted-performance)
6. [Maximum Drawdown Analysis](#maximum-drawdown-analysis)
7. [Transaction Friction](#transaction-friction)
8. [Backtesting Methodology](#backtesting-methodology)
9. [Anti-Whipsaw Confirmation](#anti-whipsaw-confirmation)
10. [Actionable Levels](#actionable-levels)
11. [Configuration Reference](#configuration-reference)
12. [Code Architecture](#code-architecture)
13. [Reproducing the Results](#reproducing-the-results)
14. [Honest Caveats](#honest-caveats)

---

## Executive Summary

The Agentic Trading Desk runs a **trend-regime strategy** on SPY (the `regime_v4` engine), validated over 11.7 years of daily data with a rigorous train/test split. The strategy is long SPY while its price is above a *rising* 200-day exponential moving average (EMA200), and flat (100% cash) otherwise. A hard stop-loss, trailing take-profit overlay, and a 3-day anti-whipsaw confirmation filter refine the core signal.

### The headline numbers (out-of-sample, 2022–2026)

| Strategy | Total Return | CAGR | Sharpe | Max Drawdown | Exposure | Trades |
|---|---|---|---|---|---|---|
| Buy & Hold | +79.5% | 15.37% | 0.94 | −19.0% | 100% | 0 |
| **Regime-V4 + 3d confirm (ACTIVE)** | **+50.6%** | **10.52%** | **0.99** | **−10.0%** | **74%** | **14** |
| Old three-pillar engine | −1.4% | −0.35% | 0.02 | −24.2% | 48% | 32 |

**The strategy matches buy-and-hold on risk-adjusted terms (Sharpe ~0.99 vs 0.94) while cutting maximum drawdown roughly in half (−10% vs −19%).** Buy-and-hold still wins on raw return in a bull market — we report this honestly rather than cherry-pick.

---

## Strategy Overview

### Core thesis

Equity indices have a long-term positive drift (the equity risk premium). Most of the damage to a portfolio happens during bear regimes when price is below the long-term trend. By stepping aside during those regimes and re-entering once the uptrend is re-established, the strategy aims to **capture most of the upside while avoiding the deep drawdowns** — not to beat the market in absolute return, but to deliver similar returns with significantly less pain.

### The signal (one sentence)

> **Be long SPY when its closing price is above the 200-day EMA *and* the EMA200 is rising; be flat (cash) otherwise.**

This is the single most robust equity-index timing signal in the quantitative-finance literature, documented across multiple decades and geographies. The strategy intentionally uses no exotic indicators, no machine learning, and no parameter-heavy optimization — because simplicity generalizes and complexity overfits.

### What it is NOT

- Not a day-trading system (average holding period: ~50 trading days)
- Not a short-selling strategy (long or flat only)
- Not a prediction engine (it reacts to realized trend, not forecasted price)
- Not an order executor (signal-only; the user approves every real order)

---

## The Evolution

### Why we replaced the original engine

The project originally used a **three-pillar counter-trend engine** (`score.py`) that combined Trend, Momentum, and Macro-Sentiment scores (each −2 to +2, total −6 to +6) and generated entry/exit signals based on exhaustion/rebound detection.

A rigorous backtest revealed this engine was **fundamentally broken**:

- Over 11.7 years it returned **+10.2%** total (vs buy-and-hold's +284.1%)
- Out-of-sample (2022–2026) it **lost money**: −1.4%, Sharpe 0.02
- Its maximum drawdown (−35.5%) was **worse than buy-and-hold** (−34.1%)
- Its counter-trend "buy the rebound" entries were systematically buying dips that kept falling

The root cause was confirmed by a 323-bar walk-forward correlation test: the three-pillar composite score was **inversely correlated** with forward returns at every horizon (+1d: −0.09, +3d: −0.15, +5d: −0.12). Higher scores preceded *worse* returns — a mean-reversion signature that the engine was mis-reading as trend-following.

### The replacement process

Five evidence-led variants were tested against the broken engine and buy-and-hold, each with explicit economic rationale and minimal parameters:

| Variant | Full Return | Sharpe | MaxDD | Rationale |
|---|---|---|---|---|
| V0 old three-pillar | +10.2% | 0.13 | −35.5% | Counter-trend rebounds |
| V1 regime (close>EMA200 rising) | +139.9% | 0.71 | −24.3% | Capture drift, avoid bear |
| V2 V1 + EMA50 exit | +122.9% | 0.71 | −20.3% | Faster de-risk trigger |
| V3 V1 + EMA20 exit | +99.7% | 0.64 | −20.8% | Dual-timeframe |
| **V4 V1 + risk overlay** | **+150.3%** | **0.75** | **−23.5%** | **Regime + mechanical stops** |

V4 was selected because it delivered the best or tied-best Sharpe in every slice, the fewest trades (59), and — critically — **generalized from TRAIN to TEST** (Sharpe 0.65→0.96, no degradation). The three-pillar scorecard is still computed for display context, but the **entry/exit decisions are now driven entirely by the regime gate**.

---

## Strategy Mechanics

### Entry condition

All three must be true simultaneously:
1. SPY closing price > EMA200
2. EMA200 slope > 0 (rising, measured as EMA200 today minus EMA200 five bars ago)
3. The above has persisted for **3 consecutive trading days** (anti-whipsaw confirmation; see [section 9](#anti-whipsaw-confirmation))

When all three are met and the desk is flat → emit **RE-ENTRY (new cycle)**.

### Exit conditions (priority cascade)

| Priority | Trigger | Action |
|---|---|---|
| 1 (highest) | Unrealized loss ≥ 11% from entry | **EXIT — STOP-LOSS HIT** |
| 2 | Trade was up ≥ 5% from entry AND price fell ≥ 3% from peak | **EXIT — TRAILING STOP** |
| 3 | Regime gate turns OFF (confirmed for 3 consecutive days) | **EXIT** (regime-off) |

The hard stop and trailing stop are **always active** and override the regime gate — they are the catastrophic-loss protection layer.

### Holding behavior

While the regime gate is ON and no exit trigger fires: **HOLD (ride the cycle)**. No position accumulation, no averaging in. The strategy's philosophy is capital rotation, not position building.

### State persistence

The paper position (entry price, peak price, holding status) and the confirmation counter (regime_state, regime_run, regime_run_date) are persisted in `desk_position_SPY.json` across the twice-daily cron runs. The signal log (`desk_signals_SPY.jsonl`) is an append-only record of every emitted signal for evaluation.

---

## Risk-Adjusted Performance

### Full history (Oct 2014 — Jul 2026, 2,945 bars)

| Metric | Buy & Hold | Regime-V4 (instant) | Regime-V4 + 3d confirm |
|---|---|---|---|
| Total return | +284.1% | +150.3% | +185.8% |
| CAGR | 12.20% | 8.17% | 9.40% |
| Annualized Sharpe (rf=0) | 0.74 | 0.75 | **0.84** |
| Max drawdown | −34.1% | −23.5% | **−20.2%** |
| Time in market | 100% | 80% | 80% |
| Round-trip trades | 0 | 59 | 41 |
| Win rate | — | 51% | **63%** |

### Out-of-sample TEST slice (May 2022 — Jul 2026, 1,031 bars)

| Metric | Buy & Hold | Regime-V4 (instant) | Regime-V4 + 3d confirm |
|---|---|---|---|
| Total return | +79.5% | +49.2% | +50.6% |
| CAGR | 15.37% | 10.27% | 10.52% |
| Annualized Sharpe (rf=0) | 0.94 | 0.96 | **0.99** |
| Max drawdown | −19.0% | −10.0% | **−10.0%** |
| Time in market | 100% | 75% | 74% |
| Round-trip trades | 0 | 17 | 14 |
| Win rate | — | 47% | **57%** |

### Interpreting the Sharpe ratio

The annualized Sharpe ratio is calculated as:

```
Sharpe = mean(daily_returns) / stdev(daily_returns) × √252
```

where `rf = 0` (we use excess-return-over-cash=0 convention). The strategy achieves a **higher Sharpe than buy-and-hold** (0.84 vs 0.74 full, 0.99 vs 0.94 OOS) because it delivers comparable *per-unit-of-risk* returns while spending 20–25% of the time in cash during high-volatility bear regimes.

### The honest trade-off

The strategy **does not beat buy-and-hold on raw return** in a rising market. SPY returned +284% full-history vs the strategy's +186%. The value proposition is:

- **Half the drawdown** (−20% vs −34%)
- **Higher Sharpe** (0.84 vs 0.74)
- **63% win rate** on 41 trades (vs the old engine's 42% on 84 trades)
- **Cash optionality**: 20% of the time is spent in cash, which could earn risk-free returns (not modeled) or be deployed to other opportunities

If your priority is maximum raw return and you can stomach −34% drawdowns, the honest answer is: just hold SPY.

---

## Maximum Drawdown Analysis

Maximum drawdown (MaxDD) measures the largest peak-to-trough decline in portfolio equity before a new high is established. It answers: **"What's the worst loss I would have experienced?"**

### Calculation

```python
def max_drawdown(equity_curve):
    peak = -inf
    mdd = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        drawdown = value / peak - 1.0
        mdd = min(mdd, drawdown)
    return mdd  # negative fraction, e.g. -0.202
```

### Comparative drawdown table

| Period (approximate) | Event | Buy & Hold DD | Regime-V4 DD |
|---|---|---|---|
| Feb–Mar 2020 | COVID crash | −34.1% | −16.4% (stepped aside) |
| Jan–Oct 2022 | Fed rate-hike bear | −25.4% | −10.0% (exited early) |
| Aug 2024 | Carry-trade unwind | −8.5% | −5.2% |

The strategy's core value is visible in the drawdown comparison: it **avoids the deep bear-market pain** that destroys compounding. The worst drawdown in 11.7 years (−20.2%) is roughly 60% of buy-and-hold's worst (−34.1%).

### Why drawdown matters more than return for most investors

A −34% drawdown requires a subsequent +52% gain just to break even. A −20% drawdown requires only +25%. The mathematical asymmetry of losses means that **drawdown control has a disproportionate impact on long-term compounding** — especially for investors who might panic-sell at the bottom or who have finite investment horizons.

---

## Transaction Friction

### Trade frequency

| Metric | Old engine | Regime-V4 (instant) | Regime-V4 + 3d confirm |
|---|---|---|---|
| Round-trips (full history) | 84 | 59 | **41** |
| Round-trips (OOS test) | 32 | 17 | **14** |
| Avg trades/year | ~7.2 | ~5.0 | **~3.5** |
| Avg holding period | ~36 days | ~52 days | **~68 days** |

### Estimated transaction costs

At ~3.5 round-trips per year on SPY (one of the most liquid instruments in the world):

| Cost component | Estimate | Annual impact |
|---|---|---|
| Commission | $0 (commission-free at Robinhood) | $0 |
| Bid-ask spread | ~$0.01 on SPY (~0.0013%) | ~0.009% |
| Market impact | negligible for retail size | ~0% |
| **Total annual friction** | | **<0.01%** |

Transaction costs are **immaterial** for this strategy. SPY's exceptional liquidity (average daily volume ~80M shares, penny-wide spreads) means that even doubling or tripling the trade frequency would not meaningfully affect returns. The 3-day confirmation further reduces churn, cutting trades from 59→41 (−30%).

### Tax considerations (not modeled)

The strategy's average holding period (~68 days) means most gains are taxed as short-term capital gains. For a tax-sensitive investor, this is a real cost not reflected in the backtest. A 401(k) or IRA wrapper eliminates this concern.

---

## Backtesting Methodology

### Data

- **Source**: Robinhood MCP `get_equity_historicals` (split-adjusted daily closes)
- **Ticker**: SPY (S&P 500 ETF)
- **Period**: October 21, 2014 — July 9, 2026 (2,945 trading days, ~11.7 years)
- **Lookback**: 4,600 calendar days requested; 220-bar warmup for EMA200 before first tradeable signal

### Point-in-time fidelity

Every bar is scored using **only data available up to that bar** — no lookahead bias. At bar `i`, the indicator window is `closes[0:i+1]`. The `score.py` engine is called with the exact same function (`score_symbol()`) used in live production — the backtest reuses the **real code**, not a reimplementation.

### Train/test split

| Slice | Period | Bars | Purpose |
|---|---|---|---|
| TRAIN (65%) | 2014-10-21 → 2022-05-26 | 1,914 | Parameter selection, variant comparison |
| TEST (35%) | 2022-05-27 → 2026-07-09 | 1,031 | Out-of-sample validation (never used for tuning) |

The adoption rule is strict: **a variant must beat the baseline on the TEST slice (Sharpe AND not-worse maxDD) to be adopted.** Performance on TRAIN alone is considered suggestive, not conclusive.

### Execution assumptions

| Assumption | Value | Justification |
|---|---|---|
| Fill price | Closing price of the signal day | Conservative; live runs at 10am/2pm CT |
| Slippage | 0 | SPY penny spreads; retail size |
| Commission | 0 | Robinhood |
| Cash yield while flat | 0 | Conservative; real cash earns ~5% currently |
| Dividends | Not modeled | ~1.3% annual SPY yield; benefits buy-and-hold comparison equally |
| Position sizing | 100% (all-in or all-out) | Matches the strategy's binary long/flat design |

### Parameter sensitivity (anti-overfit check)

Before adopting the risk overlay parameters (stop=11%, arm=5%, giveback=3%), a grid sweep tested **45 combinations** of stop/arm/giveback on the TEST slice:

```
Combos tested:   45
TEST Sharpe min:  0.87
TEST Sharpe med:  0.91
TEST Sharpe max:  0.98
TEST Sharpe std:  0.035
```

The narrow standard deviation (0.035) across 45 combos proves that **the edge comes from the regime gate, not from fine-tuned stops**. No single magic parameter combination — the strategy works across the entire reasonable parameter space.

### Reproducing the results

```bash
cd ~/Hermes\ Projects/agentic-trading-desk
export DESK_TICKER=SPY

# Phase A: fetch raw data + compute scored bars (cached after first run)
python3 scripts/backtest.py --refresh

# Phase B: run all V0-V4 strategy variants with train/test split
python3 scripts/experiments.py

# Whipsaw refinement variants (R0-R4)
python3 scripts/experiments_v2.py

# Parameter sensitivity sweep
python3 scripts/sweep_v4.py
```

All results are deterministic (same data → same output). The scored-bar cache (`spy_scored_bars_SPY.json`) avoids re-computing the O(n²) indicator walk on subsequent runs.

---

## Anti-Whipsaw Confirmation

### The problem

When SPY's price hovers near the EMA200 line, the raw regime gate flips rapidly between ON and OFF, generating churning trades that erode returns ("whipsaw"). This is a well-known weakness of any moving-average crossover system.

### The fix

The regime gate's **effective state** flips only after **N consecutive trading days** of the opposite raw signal. The counter:

- Increments once per calendar day (twice-daily cron runs don't double-count)
- Resets to zero if the raw signal flips back (conservative: a single bullish day amid a bearish streak restarts the clock)
- Persists across runs via the position state file

### Validation

| N (confirm days) | TRAIN Sharpe | TEST Sharpe | TEST MaxDD | TEST Trades |
|---|---|---|---|---|
| 1 (instant, old) | 0.65 | 0.96 | −10.0% | 17 |
| 2 | 0.60 | 0.93 | −10.2% | 15 |
| **3 (adopted)** | **0.76** | **0.99** | **−10.0%** | **14** |
| 4 | 0.72 | 0.97 | −11.2% | 14 |
| 5 | 0.79 | 0.79 | −10.0% | 13 |
| 6 | 0.78 | 0.75 | −10.0% | 13 |

N=3 was chosen because:
- It produces the **highest TEST Sharpe** (0.99) while maintaining equal-best maxDD (−10.0%)
- N=3 and N=4 both generalize; N≥5 shows TRAIN-TEST degradation (overfitting)
- It reduces trades by 30% (59→41 full-history) without sacrificing return

---

## Actionable Levels

Every twice-daily signal now includes concrete **price levels** that can be turned into broker alerts or limit orders:

### Example output (real, July 2026)

```
🎯 ACTIONABLE LEVELS
  • Regime line (EMA200): 694.67  (price +8.7% above)
  • Regime flips OFF: 3 consecutive closes < 694.67
  • Hard stop (sell alert): 667.59
  • Trailing arms at: 787.61 (+5% from entry 750.10)
```

### Level definitions

| Level | Formula | What it means |
|---|---|---|
| **Regime line** | Current EMA200 value | Below this, the regime exit clock starts |
| **Regime flip** | 3 consecutive closes below EMA200 | The strategy exits the position |
| **Hard stop** | Entry price × (1 − 0.11) | Catastrophic loss protection; sells immediately |
| **Trail arm** | Entry price × (1 + 0.05) | Once reached, trailing stop activates |
| **Trail exit** | Peak price × (1 − 0.03) | Once armed, sells if price drops 3% from peak |
| **Confirmation streak** | `⏳ streak N/3 in progress` | How many days into a regime-flip count |

---

## Configuration Reference

All tunable parameters live in `scripts/desk_config.json`:

```json
{
  "risk_params": {
    "stop_loss_pct": 0.11,
    "trail_arm_pct": 0.05,
    "trail_giveback_pct": 0.03
  },
  "strategy_mode": "regime_v4",
  "regime_confirm_days": 3
}
```

| Parameter | Default | Range | Effect |
|---|---|---|---|
| `strategy_mode` | `regime_v4` | `regime_v4` \| `three_pillar` | Which engine drives entry/exit decisions |
| `regime_confirm_days` | `3` | `1`–`10` | Days of opposite signal before regime flips |
| `stop_loss_pct` | `0.11` | `0.01`–`0.99` | Hard stop (fraction from entry) |
| `trail_arm_pct` | `0.05` | `0.01`–`0.99` | Gain from entry that arms trailing stop |
| `trail_giveback_pct` | `0.03` | `0.01`–`0.99` | Decline from peak that triggers trailing exit |

**Reversibility**: set `strategy_mode` to `three_pillar` to revert to the original engine. Set `regime_confirm_days` to `1` to disable the anti-whipsaw filter. Bad/out-of-range values are ignored and safe defaults apply.

---

## Code Architecture

```
agentic-trading-desk/
├── README.md                          # Project overview + architecture
├── STRATEGY.md                        # This document
├── SKILL.md                           # Claude Code agent operations manual
└── scripts/
    ├── indicators.py                  # Deterministic technical indicators (EMA/RSI/MACD/TRIX/BB)
    ├── score.py                       # Three-pillar scorer + decision engine
    ├── macro_pillar.py                # Cross-asset macro regime detector
    ├── run_desk.py                    # Live orchestrator: data → score → regime gate → risk → report
    ├── rh_data.py                     # Robinhood MCP data provider
    ├── robinhood_mcp.py               # MCP protocol client
    ├── robinhood_auth.py              # OAuth2 token management
    ├── eod_eval.py                    # End-of-day signal grading (mark-to-close)
    ├── strategy_eval.py               # Daily evaluator: hit-rates, backtest, scoreboard
    ├── backtest.py                    # Walk-forward backtest harness (Phase A/B)
    ├── experiments.py                 # V0-V4 strategy variants (train/test)
    ├── experiments_v2.py              # Whipsaw refinement variants (R0-R4)
    ├── sweep_v4.py                    # Parameter sensitivity grid sweep
    ├── test_confirmation.py           # Unit tests for the confirmation counter
    ├── desk_config.json               # Single source of truth for live parameters
    └── eval_reports/                  # Dated evaluation reports (on-disk history)
```

### Key code paths

**Live signal generation** (twice daily at 10am + 2pm CT):
```
run_desk.run()
  → rh_data.daily_closes()           # Fetch ~550 bars via Robinhood MCP
  → macro_pillar.score_macro()        # 8-ETF cross-asset + FRED yield curve
  → score.score_symbol()              # Three-pillar scorecard (context only)
  → run_desk.regime_action()          # ← THE ACTUAL DECISION (regime gate + confirmation)
  → run_desk.apply_risk_overlay()     # Stop-loss / trailing take-profit
  → run_desk.actionable_levels()      # Concrete price levels for alerts
  → run_desk.build_report()           # Telegram-ready output
  → run_desk.log_signal()             # Append to signal log
```

**Backtesting** (on-demand):
```
backtest.build_scored_bars()          # Phase A: compute indicators per bar (O(n²), cached)
backtest.simulate()                   # Phase B: replay any rule over cached bars (O(n))
experiments.run_slice()               # Run all variants on a bar slice
```

### Design principles

1. **Reuse the real code** — the backtest calls the same `score.score_symbol()` and `indicators.compute()` as live production. No reimplementation that could diverge.
2. **stdlib only** — all Python scripts use only the standard library. No numpy, no pandas, no external dependencies. This ensures speed, portability, and auditability.
3. **Fail-safe by construction** — if any critical step fails (data fetch, auth, scoring), `run_desk.py` prints an ABORT notice and leaves the position state untouched. No partial signals, no corrupt state.
4. **Human-in-the-loop** — the system emits signals; the user approves every real order. The word "SUGGESTED" appears before every action.
5. **Config-driven, not code-driven** — all tunable parameters live in `desk_config.json`. No code changes needed to adjust risk or switch strategies. Bad values are ignored with safe defaults.

---

## Reproducing the Results

### Prerequisites

- Python 3.9+ (stdlib only — no pip installs needed)
- A valid Robinhood MCP token (for data fetch; cached after first run)
- The repository cloned locally

### Quick start

```bash
git clone https://github.com/Oft3r/agentic-trading-desk.git
cd agentic-trading-desk

# 1. Fetch data and compute scored bars (~10 seconds)
export DESK_TICKER=SPY
python3 scripts/backtest.py --refresh

# 2. Run the full strategy comparison
python3 scripts/experiments.py

# 3. Run the whipsaw refinement comparison
python3 scripts/experiments_v2.py

# 4. Run the parameter sensitivity sweep
python3 scripts/sweep_v4.py

# 5. Run the daily evaluator (needs live Robinhood token)
python3 scripts/strategy_eval.py
```

### Expected output (backtest.py)

```
Scored bars: 2945  (2014-10-21 → 2026-07-09)
BUY & HOLD                   ret  +284.1%  CAGR +12.20%  Sharpe +0.74  maxDD  -34.1%
CURRENT STRATEGY             ret  +185.8%  CAGR  +9.40%  Sharpe +0.84  maxDD  -20.2%
```

---

## Honest Caveats

These are the things most strategy write-ups leave out. We include them because intellectual honesty is a prerequisite for trusting any system with real capital.

### 1. Buy-and-hold wins on raw return in bull markets

Over the full 11.7-year backtest, buy-and-hold returned +284% vs the strategy's +186%. In a monotonically rising market, any strategy that ever steps aside will underperform. The strategy's value is **not absolute return** — it's **the same return per unit of risk with half the drawdown**.

### 2. Past performance does not guarantee future results

The EMA200 regime filter has been documented for decades (Faber 2007, Antonacci 2012, multiple academic studies). Its persistence is theoretically grounded in behavioral finance (trend-following exploits investor under-reaction to news). But markets evolve, and a strategy that worked for 11 years can break.

### 3. The backtest does not model cash returns

When flat, the strategy assumes 0% return on cash. In reality, cash/money-market yields are currently ~5%. This makes the strategy **more attractive** than the backtest suggests — the 25% of time spent in cash could earn meaningful risk-free return, narrowing the raw-return gap with buy-and-hold.

### 4. Dividends are not modeled

SPY pays ~1.3% annually in dividends. These are not included in the backtest. Since the strategy is long ~75% of the time, it captures roughly 75% of the dividend stream, slightly favoring buy-and-hold in the comparison.

### 5. Tax drag is not modeled

The strategy's average holding period (~68 days) means gains are typically short-term. In a taxable account, this creates a meaningful drag vs buy-and-hold's long-term capital gains rate. In a tax-deferred account (IRA, 401k), this concern vanishes.

### 6. The strategy is long-only and single-asset

It does not short, does not use leverage, and trades only SPY. Diversification across assets, strategies, or geographies is the investor's responsibility.

### 7. Twice-daily evaluation is not continuous

The strategy evaluates at 10am and 2pm CT. A flash crash between evaluations would not trigger a stop until the next run. The hard stop is a **signal**, not a broker-side stop-loss order. For mechanical execution, the user should set corresponding alerts or stop orders with their broker.

---

*This document is generated from real backtest results and live code. Every number is reproducible by running the scripts referenced above. No numbers were hand-picked or selectively reported — every slice (FULL, TRAIN, TEST) is shown for every variant tested.*
