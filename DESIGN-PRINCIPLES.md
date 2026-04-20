# Design principles, architecture, and abandoned avenues

**Status as of:** 2026-04-20
**Current active work:** Plan E multi-instance paper fleet (8 variants)
**Production status:** Legacy single-pair live bot has been stopped since 2026-04-18 ("Plan D pivot"). No live capital at risk.

This document is a durable record of **how** and **why** the bot is built the
way it is, and which alternative approaches were tried and rejected. The
`README.md` describes the legacy single-pair bot (still the largest part of
the code surface). This file is the up-to-date map to everything else.

---

## 1. Core development principles

These principles were not all present from day one. Several were added
*because* earlier work violated them. Each principle is followed by the
concrete incident that taught us it matters.

### P1. Paper-first, always

**Rule:** New strategies run paper-only for at least 2-4 weeks before any
live capital is committed. Paper instances must share the exact market data
the live bot sees (same cache, same fetch path) so the comparison is clean.

**Why:** The legacy "advanced" regime bot looked fine on its own grid
search but returned a 12% win rate over 185 trades across 12 months live.
See `FINDINGS-2026-04-18-baseline-wr.md`. A paper window would have surfaced
the three structural bugs (see §4) before any money was exposed.

### P2. An out-of-sample gate is mandatory; in-sample is not a result

**Rule:** Every new signal is evaluated with a walk-forward split. The train
slice tunes parameters; the test slice decides go/no-go. OOS Sharpe is the
primary metric; in-sample Sharpe alone is not a gate.

**Why:** Ten rounds of threshold/weight tuning on the legacy feature set
produced in-sample improvements that evaporated out-of-sample. The final
ML diagnostic (`ANALYSIS-2026-04-18-edge-options.md`) showed AUC 0.503 on
forward returns — indistinguishable from random. The in-sample lift was
noise fitting.

### P3. Classifier / gate must clear an explicit predictive-power bar *before* it gates live trades

**Rule:** A classifier or filter is allowed to control live execution only
after it demonstrates **AUC > 0.55** on a walk-forward holdout using the
strategy-aligned target. Otherwise it can be on the monitoring surface but
cannot make decisions.

**Why:** The advanced strategy's regime classifier emitted `range` 100% of
the time. The live bot's trade logic was built for trend. This mismatch
hid the fact that the strategy had no edge, for months. Requiring an AUC
gate forces the predictor to prove its usefulness before it is trusted.

### P4. Edge must come from new information, not from re-tuning old features

**Rule:** When a feature set has been shown to lack measurable edge (AUC
~ 0.50), further tuning of it is frozen for at least 3 months. Work
continues on **new information sources** (funding rate, open interest,
cross-sectional, on-chain) or new **strategy surfaces**.

**Why:** Continuing to tune a dead feature set is a gambler's fallacy —
"one more calibration pass." It has near-zero expected value and very
real alternative-cost. Recorded as a binding decision in
`DECISIONS.md` on 2026-04-18.

### P5. Execution-gating > signal-adaptation

**Rule:** When considering a new rule/flag, prefer ones that **gate
execution** (skip a trade, pause on volatility, reduce size) over ones
that **modify the signal** (change lookback, re-weight factors). When in
doubt, run both as separate paper instances.

**Why:** Signal adaptations tend to overfit; execution gates usually don't,
because they react to directly observable state (vol, breadth, account
balance). The stop-loss competition on 2026-04-19 confirmed this: the 5
variants that *cut reverting legs* (fixed 5/10/15/20% and vol-scaled 2σ)
all destroyed OOS Sharpe, while the 1 variant that only **protects winners
from give-back** (SL-TRAIL, armed after +5% favorable) was the sole
survivor. Cutting losers fights the mean-reversion thesis; protecting
winners does not.

### P6. Parallel variants, shared data

**Rule:** Run competing strategy variants as independent paper instances
that share the market-data cache. State is per-instance; prices are
identical. This isolates the contribution of each feature flag without
requiring a rebuild of the backtest framework.

**Why:** A/B testing in finance is expensive if each arm costs separate
data access, separate runtime, and separate reconciliation. A single
runner templated by systemd (`plan-e@.service` + per-instance config)
gives the same statistical cleanness as a cross-sectional sweep at
~1/8 the engineering cost. See `scripts/plan_e_runner.py` and
`deploy/deploy_multi.sh`.

### P7. Fail closed on reconciliation mismatches

**Rule:** On startup the bot compares exchange positions with local state.
In live mode, any mismatch (orphan position, missing TP/SL, stale state)
aborts the bot. Use `--force-reconcile` only in emergencies.

**Why:** A silent reconciliation skip on the legacy bot once caused it to
open a new position on top of an already-open one it had forgotten about,
because a previous crash had not persisted the position map. Fail-closed
converts silent corruption into a loud startup abort.

### P8. Atomic state writes

**Rule:** All persisted state (`portfolio.json`, `positions.json`,
`runtime-state.json`) is written via tmp-file + atomic rename, never
in-place.

**Why:** A crash during an in-place write produces a truncated JSON file
that then breaks the next startup reconciliation. The cost of the rename
dance is negligible; the cost of a corrupted state file during a live
position is not.

---

## 2. Architecture at a glance

### 2.1 Legacy stack (stopped since 2026-04-18)

```
┌─────────────────────────────────────────────────┐
│ scripts/trading_bot.py            (entrypoint)  │
│   ├─ advanced_strategy.py         (confluence)  │
│   ├─ regime_timeframe.py          (TF resolver) │
│   ├─ risk_utils.py                (SL sizing)   │
│   ├─ blofin_adapter.py            (exchange)    │
│   └─ market_data_stream.py        (WebSocket)   │
│ Persistence: memory/positions.json + runtime-state.json
└─────────────────────────────────────────────────┘
```

Single pair (BTC-USDT), 5m bar, multi-indicator confluence, regime-aware
timeframe switching. Still in the repo because the execution plumbing
(reconciliation, TP/SL, circuit breaker, WebSocket) is reused or ported.
Strategy layer is frozen (see P4).

### 2.2 Current stack (Plan E paper fleet)

```
┌───────────────────────────────────────────────────────────┐
│ scripts/plan_e_runner.py           (multi-instance runner)│
│   ├─ PlanEConfig + feature-flag dataclasses:              │
│   │   VolHaltConfig, BreadthSkipConfig,                   │
│   │   OutlierExcludeConfig, StopLossConfig                │
│   ├─ compute_signal()  → log(close[-1]/close[-73])        │
│   ├─ rank_signals()                                       │
│   ├─ select_positions() (with k_exit=6 hysteresis)        │
│   ├─ paper_execute_rebalance()                            │
│   └─ check_stops_cycle() (for plan-e-trail only)          │
│ Per-instance state: state/plan-e-<variant>/portfolio.json │
│ Shared market data: state/shared_cache/                   │
└───────────────────────────────────────────────────────────┘
               │
               ▼
┌───────────────────────────────────────────────────────────┐
│ systemd templated unit: plan-e@<variant>.service          │
│ 8 instances on the Proxmox LXC:                           │
│   plan-e@base   control                                   │
│   plan-e@c      + BTC vol-halt                            │
│   plan-e@g      + breadth tail-skip                       │
│   plan-e@cg     + C + G stacked                           │
│   plan-e@i      + outlier exclusion                       │
│   plan-e@12h    12h rebalance cadence                     │
│   plan-e@48h    48h rebalance cadence                     │
│   plan-e@trail  + trailing 10% stop-loss (arm at +5%)     │
└───────────────────────────────────────────────────────────┘
               │
               ▼
┌───────────────────────────────────────────────────────────┐
│ scripts/dashboard_api.py   (Flask, multi-instance aware)  │
│ scripts/dashboard.html     (instance dropdown + Compare)  │
└───────────────────────────────────────────────────────────┘
```

Signal: cross-sectional 72h log-return reversal (`sign=-1`) over a 10-asset
USDT-perp universe, 24h rebalance, 10% notional per leg → 60% gross exposure,
rank hysteresis with `k_exit=6`, no portfolio-level stops (except the one
paper variant). See `backtest/results/PLAN-E-final.md` for the validated
parameters and `PLAN-E-DEPLOY.md` for the runbook.

---

## 3. Work streams in order (planned vs. actual)

The original roadmap from `DECISIONS.md` (2026-04-18) was strict
**A → C → D → E**, one at a time, each live for 2+ weeks before the next.
Reality departed from that sequence:

| Plan | What | Planned | Actual outcome |
|------|------|---------|---------------|
| A | Funding-rate gate | First to ship | **Paused** before live after investigation surfaced 3 structural base-strategy bugs |
| B | Maker-only execution | Deferred up-front | Still deferred (adverse selection risk > 4bps fee savings) |
| C | OI divergence filter | Second, gates on A | Not started — A never went live |
| D | Mean-reversion for chop | Third, after A+C live | **Shipped backtest, failed step 3.** Abandoned 2026-04-18 |
| D-ζ | Plan D on higher TFs | Rescue attempt | **Closed** — classifier gates produced 0 trades on both 15m and 1h |
| E | Cross-sectional reversal | Fourth, largest project | **Promoted to current active work** after A/D dead-ends |

The order A→E flipped to A→D→E→(others deferred) because A's investigation
surfaced that the base strategy itself could not be rescued, so funding was
the wrong next bet. D was then tried as a dedicated chop strategy; when its
gate failed the OOS test, E became the only remaining path with a validated
edge.

---

## 4. Abandoned avenues and why

### 4.1 Legacy "advanced" regime-aware confluence strategy — **Abandoned 2026-04-18**

Multi-indicator confluence (RSI + MACD + Bollinger + volume) with 4-regime
classifier, dynamic timeframes, and per-regime risk multipliers.

**Why it failed:**
1. Over 185 live trades across 12 months, **0 shorts** (`advanced_strategy.py:110`
   defaulted `range_allow_shorts=False`; baseline config never set it true).
2. The regime classifier emitted `range` 100% of the time. Trade logic was
   built for trend. Mismatch hid the strategy's lack of edge.
3. Logistic regression of the seven regime-condition features on forward
   12-bar returns: **AUC 0.5030** — statistically random.

**What we kept from it:**
- The execution plumbing: TP/SL reconciliation, atomic state writes,
  circuit breaker, WebSocket resilience. These are strategy-agnostic and
  have been battle-tested on real orders.
- The backtester harness (`backtest/backtester.py`), adapted for SL-based
  sizing.
- The Blofin API client and adapter layer.

**Lesson → principle P3 + P4.**

### 4.2 Plan A — Funding-rate gate — **Paused before live**

Skip entries when funding is extreme in the direction we'd enter; later,
consider a funding-fade active strategy.

**Why paused:** Plan A step 3 produced a 9.7% WR. Investigation
(`FINDINGS-2026-04-18-baseline-wr.md`) showed the low WR was not a funding
question at all — it was three independent base-strategy bugs (see §4.1).
Adding a gate on top of a broken base is mechanically helpful but cannot
rescue any of them. Decision: fix the base first. Fix turned out to be
impossible (4.1), so A never shipped.

**Status:** Work preserved in `backtest/funding_backfill.py`,
`backtest/funding_gate_backtest.py`,
`backtest/results/funding_gate_summary.md`. If a new base strategy is
validated, Plan A is the first candidate gate to add.

### 4.3 Plan B — Maker-only execution — **Deferred indefinitely**

Switch to post-only limit orders to capture the maker rebate.

**Why deferred:** Realistic round-trip fee savings were estimated at ~4bps
(≈0.04%) vs. ~11bps for taker. Adverse-selection risk (the order only fills
when someone on the other side has reason to hit us) was judged to eat the
savings. Not worth the engineering cost unless fees become a first-order
cost driver, which they aren't at current volume.

**Status:** No code written. `backtest/plan_e_eta_maker.py` was a brief
exploration for Plan E specifically — the half-maker assumption lifts
Plan E's Sharpe from ~1.4 in-sample / ~2.3 OOS (taker) but the lift is a
sensitivity bound, not a commitment to maker-only execution.

### 4.4 Plan C — Open-interest divergence filter — **Never started**

Price rising + OI rising → real buying (stay); price rising + OI falling →
short squeeze (skip).

**Why never started:** Gated on Plan A being live and measured. A never
went live. If any new base strategy (Plan E) validates live, C would be
re-evaluated as a cross-sectional filter alongside Agent C/G/I.

### 4.5 Plan D — Mean-reversion strategy for chop — **Abandoned 2026-04-18**

Single-asset 5m mean-reversion on BTC-USDT, gated by a separately validated
chop classifier (AUC > 0.55 requirement per P3).

**Why failed:**
- Step 1 (classifier unconditional): **PASS** (AUC_test 0.6436)
- Step 1 (classifier conditional on |z|>=2): **PASS** (AUC_test 0.7844)
- Step 3 (backtest with the classifier as gate): **FAIL** — the classifier's
  edge did not translate into trade-level P&L, because the target used for
  training (5m z-score reversion) was not aligned with the strategy's exit
  mechanics.

See `backtest/results/PLAN-D-step3-final.md`,
`backtest/results/PLAN-D-step3-sweep.md`.

### 4.6 Plan D-ζ — Higher-timeframe target-aligned classifier — **Closed**

Rescue attempt: train the classifier on a strategy-outcome-aligned target
(crosses SMA20 before z-stop) on 15m and 1h bars.

**Why closed:** AUC dropped to 0.5430 (15m) / 0.6152 (1h) under the aligned
target. When fed into the backtest, every probability gate from 0.50 to
0.70 produced **zero trades** — the classifier was confident on almost no
bars. See `backtest/results/PLAN-D-zeta-summary.md`. With no trades there
is no strategy to measure. Plan D closed.

### 4.7 Plan E refinements — Kept baseline, rejected variants

Several sub-explorations were run on top of the validated Plan E signal:

| Variant | File | Outcome |
|---------|------|---------|
| Plan E sweep (lookback / cadence / long_n / short_n) | `backtest/plan_e_sweep.py`, results in `PLAN-E-sweep.md` | Baseline parameters (72h lb, 24h cadence, 3L/3S) were robust; neighbors within ±1 slot hold up |
| Plan E hysteresis sweep (k_exit) | `PLAN-E-theta-hysteresis.md` | k_exit=6 is the plateau midpoint; k_exit ∈ {5,6,7,8} all acceptable. k_exit=4 overtrades |
| Plan E θ refine | `PLAN-E-theta-refine.md` | Finer grid within the plateau — no further lift |
| Plan E η maker-assumption | `PLAN-E-eta-maker.md` | 50% maker-fill assumption lifts Sharpe ~0.5-0.7; kept as sensitivity, not a commitment |

The Plan E final config uses the **middle of the robust plateau** rather
than the single best grid point — follows P2 (don't bet OOS on a single
lucky tile).

### 4.8 Stop-loss competition — 5 of 6 variants rejected

Six stop-loss variants evaluated in parallel on 2026-04-19 with OHLC-level
intrabar simulation and walk-forward split (train < 2026-01-01, test ≥).

| Variant | OOS ΔSharpe | Verdict |
|---------|------------:|---------|
| SL-5 fixed (symmetric 5%) | **−1.49** | REJECTED — cuts 45% of legs, fights reversion |
| SL-10 fixed (symmetric 10%) | **−1.18** | REJECTED — cuts reversion leg before it pays |
| SL-15 fixed (symmetric 15%) | **−0.81** | INCONCLUSIVE/lean-reject — train helps, test hurts |
| SL-20 tail-only (symmetric 20%) | **−1.51** | REJECTED — tail protection insufficient; edge erosion larger |
| SL-VOL (2× 30d σ per asset) | **−0.62** | REJECTED — vol-scaling still cuts reverters |
| SL-TRAIL (10% trail armed after +5%) | **+0.69** | **PROMOTED to paper** as `plan-e-trail` |

**Root cause of failures:** Plan E enters precisely *because* an asset has
moved 72h in one direction; the entry hypothesis is that the move reverses.
Any symmetric stop is sampling from the very population most likely to
continue briefly before reverting — stopping out locks in the loss and
misses the revert. This is a direct instance of P5: execution gates that
fight the signal destroy edge. The trail-stop survives because it only
arms *after* favorable movement; it never cuts a reverter, only protects
winners from give-back. See `backtest/results/stoploss-*.md`.

**Live caveat on the survivor:** SL-TRAIL had 46 triggers over 12 months
with aggregate lock-in +$17.72 vs. +$34.87 extra fees. Sharpe lift comes
from variance reduction, not trigger-level profit. Statistically fragile;
deployed as paper-only (`plan-e-trail`) for live validation alongside the
7 non-SL variants.

---

## 5. Current open questions (tracked in docs, not here)

- Does Plan E survive 4 weeks of paper-live with real exchange-side
  prices? (gate criterion: OOS Sharpe similar to backtest within ±0.4)
- Does plan-e-c (vol-halt) or plan-e-g (breadth-skip) add measurable
  improvement, or does the baseline dominate? (answer determines which
  gate, if any, lands in the live config)
- Does plan-e-trail beat plan-e-base OOS after 4+ weeks live, and is
  the max-DD lower? (go/no-go on promoting trailing stop to base)
- If Plan E validates live, does Plan A (funding gate) re-enter the
  roadmap as a cross-sectional filter?

---

## 6. What explicitly NOT to do (binding, per `DECISIONS.md` 2026-04-18)

Unless a concrete bug is found, the following are banned for 3 months:

- Re-running `backtest/calibrate_per_timeframe.py` with new parameter grids
- Re-balancing the weights / score contributions of the seven trend conditions
- Adjusting `trend_min_score`, `min_confidence`, ATR multipliers, or anchor
  thresholds on the legacy strategy
- "One more calibration pass" on `efficiency_ratio`, `trend_strength`, or
  `anchor_slope`

Allowed in that period:
- Bug fixes where the scoring math is provably wrong
- Execution improvements (order sizing, slippage, TP/SL reliability,
  circuit breakers, reconciliation)
- Monitoring, observability, logging improvements
- Adding genuinely new signals or strategy surfaces

---

## 7. Where to read what

| Question | Source of truth |
|----------|-----------------|
| How does the legacy bot work? | `README.md` |
| Current Plan E runbook | `PLAN-E-DEPLOY.md` |
| Plan E final parameters + rationale | `backtest/results/PLAN-E-final.md` |
| Why the feature set was frozen | `ANALYSIS-2026-04-18-edge-options.md` + `DECISIONS.md` |
| Why the base strategy had 12% WR | `FINDINGS-2026-04-18-baseline-wr.md` |
| Plan D mean-reversion attempt | `PLAN-D-mean-reversion.md` + `backtest/results/PLAN-D-*.md` |
| Plan A funding-gate work | `PLAN-A-funding-signal.md` + `backtest/results/funding_gate_summary.md` |
| Stop-loss competition | `backtest/results/stoploss-*.md` |
| Binding decisions log | `DECISIONS.md` |
| What was stripped from this copy | `SANITIZATION-NOTES.md` (sanitized copy only) |
