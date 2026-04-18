# Plan D — Dedicated Mean-Reversion Strategy for BTC-USDT Chop

**Status:** In progress
**Created:** 2026-04-18
**Supersedes for live deploy:** The current "advanced" strategy
**Gates on:** FINDINGS-2026-04-18 (12-month baseline unfixable), and user
authorization 2026-04-18 to pursue option β (Plan D) after Plan A/fix
verification dead-ended.

---

## Goal

Build a single-asset mean-reversion strategy for BTC-USDT on 5m bars, gated
by a **separately validated chop classifier**. The old strategy's core
failure mode was that its regime classifier emitted `range` 100% of the
time while its trade logic was built for trend — a false classifier hid
the fact that the strategy had no edge.

Plan D inverts that: the classifier must clear an explicit predictive-power
bar **before** it's allowed to gate live trades. If the chop classifier
cannot beat AUC 0.55, Plan D is abandoned in favor of Plan E or shutdown.

Plan D reuses the existing Blofin connector, 5m candle dataset, and
backtester harness. It does not reuse the "advanced" strategy's regime
classifier, signal logic, or TP/SL geometry.

---

## Success criteria (backtest)

For step 3 to pass and progress to walk-forward validation:

- **Win rate > 50%** — mean reversion typically has high WR because exits
  target a small reversion move
- **Win:loss ratio ≥ 0.8** — keeps breakeven WR achievable
- **Net expectancy positive after fees** — `avg_win × WR − avg_loss × (1−WR) > 0`
- **Sharpe > 1.0** on daily P&L series over 12 months
- **Max drawdown < 15%** of starting balance

Failure criteria (stop, escalate to Plan E or γ):

- WR < 40% on 12-month backtest
- Net expectancy ≤ 0 after fees
- Sharpe ≤ 0

## Success criteria (walk-forward)

- All 3 walk-forward test windows show positive expectancy individually
  (not just in aggregate)
- No single test window has WR below 40%

---

## Six-step execution plan

### Step 1 — Build & validate chop classifier

**Scope:** new module `scripts/chop_classifier.py`. Features per 5m bar:

- `atr_pct` — ATR14 as a percentage of close
- `bb_width` — Bollinger band width (upper − lower) / middle, 20-bar
- `adx14` — directional movement (low = ranging)
- `hurst` — 100-bar rolling Hurst exponent estimate (< 0.5 = mean-reverting)
- `autocorr_1` — lag-1 autocorrelation of 1-bar returns over last 100 bars

**Target label** (binary, per bar): "price returns to SMA20 within 48 bars
(4 hours) without breaking ±3×ATR14 first." Forward-looking label; computed
once over the dataset then held out of features.

**Model:** logistic regression with L2 regularization. Simple and
interpretable by design — mirrors the discipline lesson from the old
classifier's feature-freeze incident. Any win from fancier models can come
later once the target definition itself is validated.

**Split:** 9 months train (2025-04-12 → 2026-01-12), 3 months test
(2026-01-12 → 2026-04-17).

**Deliverable:** `backtest/results/PLAN-D-step1-classifier.md` reporting:
- Test AUC (must be > 0.55 to proceed; 0.52-0.55 triggers variations; <0.52
  stops Plan D)
- Feature importances (coefficient magnitudes)
- Calibration curve (predicted probability vs observed frequency)
- Base rate (how often the target fires) — if <10% the problem is imbalanced
  and we'll rebalance

If AUC fails the bar, try up to 3 variations (different N, different feature
sets) then stop.

---

### Step 2 — Mean-reversion strategy module

**Scope:** new module `scripts/mean_reversion_strategy.py`. Subclass of
`TradingStrategy` returning a `Signal`. Logic:

1. Compute z-score of close vs 20-bar SMA: `z = (close - sma20) / std20`
2. Compute chop classifier probability `p_chop` from step 1
3. **Entry conditions:**
   - `p_chop > 0.60` (model confident we're in chop)
   - `|z| > 2.0` (price is stretched)
   - RSI14 in the confirming extreme (>70 for z>0 short, <30 for z<0 long)
4. **Exits:**
   - Target: z-score returns to 0 (mean reverted) — computed each bar, not
     fixed ATR TP
   - Stop: `|z| > 3.5` (regime break — classifier was wrong)
   - Max hold: 48 bars (4 hours) — if reversion hasn't happened, exit flat
5. **Sizing:** 0.5% risk per trade based on distance to stop
6. **No ATR-multiplier TP/SL.** The old strategy's 3×ATR TP often landed
   inside the 0.22% friction floor — a known-lose geometry. Mean-reversion
   exits by target instead.

**Deliverable:** strategy module, registered in `create_strategy("meanrev", ...)`.

---

### Step 3 — 12-month backtest

**Scope:** new script `backtest/plan_d_backtest.py`. Runs the new strategy
on the same 106k 5m candles used for the baseline. Same friction model:
fee_rate=0.0006, slippage_pct=0.05 (0.22% round-trip floor).

**Reports:**

- Headline: trades, WR, avg win/loss, expectancy, net P&L, Sharpe, max DD
- Exit reason distribution (target hit / stop hit / max hold)
- Monthly P&L breakdown
- Comparison to baseline (same period): 185 trades / 12.4% WR / -$37.84 P&L
- Chop-gate fire rate: how often did the classifier suppress an otherwise-valid z-score signal

**Deliverable:** `backtest/results/PLAN-D-step3-backtest.md`.

**Decision gate:** against the success/failure criteria above.

---

### Step 4 — Walk-forward validation

**Scope:** new script `backtest/plan_d_walk_forward.py`. Rolling windows:

- Window 1: train 2025-04-12 → 2025-10-12 (6mo), test 2025-10-12 → 2026-01-12 (3mo)
- Window 2: train 2025-07-12 → 2026-01-12 (6mo), test 2026-01-12 → 2026-04-12 (3mo)
- (only 2 test windows fit in 12 months of data; if more is needed, extend
  the historical pull)

The classifier is **retrained per window** on the 6-month train block,
then the strategy is run over the 3-month test block with that frozen
classifier. No parameter reselection between windows.

**Deliverable:** `backtest/results/PLAN-D-step4-walkforward.md`. Per-window
metrics plus aggregate.

**Decision gate:** all windows must pass the walk-forward criteria above.

---

### Step 5 — Paper-trade on live data [not runnable while user AFK]

**Scope:** deploy the strategy to a new live pipeline that writes intended
trades to `memory/paper-trades.jsonl` but places no real orders. Run for
minimum 2 weeks, target 4 weeks.

**Deliverable:** paper-trade log + weekly report comparing live signal
distribution against backtest distribution. Key check: execution slippage
(mark price when signal fires vs simulated fill) within assumption.

---

### Step 6 — Live deploy at reduced risk [not runnable while user AFK]

**Scope:** flip the prod bot from "stopped" back on with the new strategy
config. **0.5% risk per trade** (down from 1.5% on old strategy), daily
monitoring for first 30 days.

**Kill switch:** systemctl stop if 7-day cumulative net P&L drops below
-2% of account balance.

**Deliverable:** `backtest/results/PLAN-D-live-month1.md` after 30 days.

---

## Out of scope

- Multi-asset (Plan E) — stays deferred
- Funding gate overlay (Plan A) — can be bolted on later if Plan D live
  shows positive expectancy and the gate still makes sense
- OI divergence (Plan C) — gated on A
- Re-tuning the old "advanced" strategy features — banned by DECISIONS.md
  feature freeze

---

## Rollback plan

- Steps 1-4 are offline — no rollback needed
- Step 5 paper mode — no trade impact, just stop the process
- Step 6 live — `systemctl stop trading-bot` on prod host, same as prior stop
  at 2026-04-17 22:42 UTC

---

## Notes during execution

Progress per step will be appended to `backtest/results/PLAN-D-step{N}-*.md`.
If a step fails the decision gate, a follow-up entry goes into DECISIONS.md
and Plan D is closed out with a write-up of what was learned.
