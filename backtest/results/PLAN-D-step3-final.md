# Plan D — Step 3 Final Verdict: FAIL, escalate

**Date:** 2026-04-18
**Test slice:** 2026-01-12 → 2026-04-17 (3 months, out-of-sample to classifier training)
**Backtest config:** fee=0.0006, slippage=0.05%, risk=0.5% per trade
**Status:** Plan D step 3 has failed. Walk-forward (step 4) **skipped** per
plan's own failure policy. Prod bot remains stopped from 2026-04-17 22:42 UTC.

## What happened

Step 1 passed cleanly — chop classifier achieved test AUC 0.6436 on
out-of-sample data, well above the 0.55 gate. This was a genuine positive
signal and notably different from the old regime classifier's AUC 0.503.

Step 3 failed across every configuration tested:

| Variant | p_chop | z_entry | z_stop | Trades | WR | Expectancy | Net P&L | Sharpe |
|---------|--------|---------|--------|--------|-----|------------|---------|--------|
| V0 default | 0.60 | 2.0 | 3.5 | 403 | 25.8% | -0.159 | -$64.06 | -20.70 |
| V1 strict gate | 0.75 | 2.0 | 3.5 | 45 | 24.4% | -0.228 | -$10.28 | -6.13 |
| V2 wide geom | 0.60 | 2.5 | 4.0 | 238 | 27.3% | -0.167 | -$39.86 | -12.97 |
| V3 strict+wide | 0.75 | 2.5 | 4.0 | 31 | 22.6% | -0.262 | -$8.13 | -6.04 |
| V4 very strict | 0.85 | 2.5 | 4.5 | 2 | 0.0% | -0.717 | -$1.43 | -2.89 |

Conditional-classifier probe (retrained only on bars with |z|>=2):

| Variant | Gate | Trades | WR | Expectancy | Net P&L |
|---------|------|--------|-----|------------|---------|
| Conditional (test AUC 0.7844) | p>0.50 | 54 | 20.4% | -0.240 | -$12.97 |
| Conditional | p>0.60 | 26 | 19.2% | -0.231 | -$6.00 |
| Conditional | p>0.70 | 12 | 8.3% | -0.313 | -$3.76 |
| Conditional | p>0.80 | 2 | 0.0% | -0.387 | -$0.77 |

All variants fail the step-3 gate (WR > 50%, expectancy > 0, Sharpe > 1).
The best variant by net P&L is the smallest-trade-count one — you can
only minimize loss by not trading.

## Why it failed

Three mutually reinforcing issues, from shallow to deep:

### 1. Fee share is catastrophic at retail account size

Default config: fees $35.76 on gross -$28.29 → fee share 126%. Even the
strictest gates produce 60%+ fee share. At 5m bar cadence with 0.22%
round-trip friction on a $115 account, the strategy trades itself into
oblivion regardless of signal quality. Mean-reversion edge per trade on a
single asset is structurally too small relative to fees.

### 2. Conditional base rate is unfavorable for mean reversion

Unconditional base rate (any bar): **61%** of bars mean-revert within 48
bars. That's the classifier's training population.

Conditional base rate (|z|>=2 bars): **27%.** Overextended bars are
systematically LESS likely to revert than random bars. Momentum eats them.

So the strategy's entry condition (|z|>=2) selects *exactly* the population
where mean reversion is least likely. The unconditional classifier (AUC
0.64) is applied to a subpopulation where base rate has collapsed.

Retraining conditional on |z|>=2 lifts AUC to 0.78 — the classifier is
more discriminating than the unconditional version on this harder task.
But even a 0.78 AUC classifier ranking a 27%-base-rate subpopulation
can't push WR above 50% — and the strategy's observed conditional WR
(20%) is actually *lower than base rate*, which points to the next issue.

### 3. Classifier target is misaligned with strategy exit geometry

The classifier's target is:
> `target=1 if price crosses SMA20 within 48 bars AND does not break ±3×ATR from entry close`

The strategy's stop is:
> `stop at z = z_stop (3.5–4.5), mean-reverting target at z = 0`

These are different. A bar can be labeled `target=1` (reverts to SMA20
without breaking ±3×ATR bands) while the strategy still stops out — the
z-stop fires based on z-score from a moving SMA, while the target-break
check uses ATR-width bands anchored at entry price. The z=3.5 stop is
roughly ±1.5×std20 from entry, which is tighter than the ±3×ATR band
for most bars.

The classifier learned "will this bar eventually revert?" The strategy
needed to know "will this bar revert *before stopping out at z=z_stop*?"
Those are not the same question. The bias has only one direction:
strategy stops out on bars the classifier correctly labeled as reverting.

## What I did not do

**Did not proceed to step 4 (walk-forward).** Per PLAN-D-mean-reversion.md
step 3 failure policy, a step-3 failure stops the plan. Walk-forward tests
the SAME strategy on MORE data — when the in-sample backtest already loses
money, walk-forward can only confirm the loss, not invalidate it.

**Did not deploy anything.** Prod bot has been stopped since 2026-04-17
22:42 UTC and remains stopped. All Plan D work is local + uncommitted-
until-reviewed per user's Q4=a default.

## Candidate next moves (for user decision)

Three ways forward, listed by scope:

### Option δ — Plan D-v3: target-aligned classifier

Redesign the classifier's target to match the strategy's actual success
condition:
> `target=1 iff price crosses SMA20 BEFORE |z| exceeds z_stop within
>  max_hold_bars, given entry at |z|>=z_entry`

This is a conditional *and* strategy-aware target. AUC on this harder
target is unknown but bounded by the 27% conditional base rate — it's
a harder problem. Worth ~2 days of work.

**Upside:** if AUC on this target is ≥0.60, the strategy has a real chance
because it's being optimized against its actual PnL-determining event.

**Downside:** the fee floor doesn't change. Even perfect signal won't fix
friction at 5m BTC-USDT retail scale. And the target is forward-looking
through strategy exit logic — feature engineering / validation gets messy.

### Option ε — Plan E: cross-sectional multi-asset

Originally reserved as the fallback. Alpha from pair / basket
relationships is less friction-constrained because positions partially
hedge, and the signal space is larger. Implementation cost: real — needs
multi-asset data pipeline and portfolio sizing logic. Account size ($115)
makes this marginal even if it works — per-leg notionals would be tiny.

### Option γ — Shutdown (reaffirm)

Accept that BTC-USDT 5m retail trading at the current account size is
friction-bound below viability. Keep prod bot stopped indefinitely.
Record the findings and close out the trading project.

Given the data:
- 12 months of baseline: -$37.84
- 3 months of Plan D best variant: -$1.43 (barely trading)
- Projected annual run-rate at best variant: ~-$5 (essentially flat but
  with opportunity cost of the account)

The defensible case for γ is strong. The case for δ or ε rests on
believing that this particular combination of market, timeframe, account
size, and fee schedule is the constraint — and that changing one or more
of those parameters (maker fees via Plan B, larger timeframe via 15m/1H,
different asset, different strategy class) could change the outcome. That
is not a small bet and deserves an explicit decision rather than drift.

## Recommendation

**γ + research track.** Stop live trading. Spend the time that would have
gone into δ or ε on understanding *why* the current market/timeframe/size
combination doesn't support a positive-expectancy strategy. That knowledge
is valuable either way — if it turns out there IS a viable strategy for
this combination, you'll know why. If not, you'll have saved yourself
from burning money trying.

My read — but this is your call. I'll wait.
