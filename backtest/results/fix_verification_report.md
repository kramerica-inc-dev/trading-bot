# Fix Verification — config-level fixes insufficient

**Date:** 2026-04-18
**Dataset:** 106,559 5m BTC-USDT candles, 2025-04-12 → 2026-04-17 (12 months)
**Script:** `backtest/fix_verification.py`

## Question asked

After the FINDINGS-2026-04-18 writeup, the user authorized three quick fixes
to verify whether the baseline WR could be brought above 30% without a
deeper intervention:

1. `mean_reversion.allow_shorts: true`
2. Wider take-profit multiplier
3. Stop the live bot while testing

This report answers: do those config changes reach the >30% WR bar?

## Answer

**No.** The best configuration tested (shorts on, TP=3.5×ATR, SL=2.0×ATR)
reaches 18.5% WR. Still far below the 30% target. Net P&L over 12 months
improves by only $1.70.

## Data

| Config                     | Trades | Long/Short | WR    | Avg win/loss ($) | Expectancy | Net P&L  | Regimes    |
|----------------------------|--------|------------|-------|------------------|------------|----------|------------|
| A: current                 | 185    | 185/0      | 12.4% | +0.054/-0.241    | -0.205     | -$37.84  | range:185  |
| B: allow_shorts only       | 185    | 185/0      | 12.4% | +0.054/-0.241    | -0.205     | -$37.84  | range:185  |
| C: B + TP=2.5×ATR          | 185    | 185/0      | 14.1% | +0.073/-0.247    | -0.202     | -$37.35  | range:185  |
| D: B + TP=3.0×ATR          | 185    | 185/0      | 15.1% | +0.072/-0.250    | -0.201     | -$37.16  | range:185  |
| E: B + TP=3.5×ATR, SL=2.0  | 184    | 184/0      | 18.5% | +0.076/-0.258    | -0.196     | -$36.14  | range:184  |
| F: trend TP=5.0 only       | 185    | 185/0      | 12.4% | +0.054/-0.241    | -0.205     | -$37.84  | range:185  |

## Two new discoveries

### 1. `allow_shorts: true` is not enough to enable shorts

Setting `mean_reversion.allow_shorts: true` produces zero short trades over
12 months. The range-regime short-signal path has four additional
conjunctive gates:

```python
# advanced_strategy.py line 920
if self.range_allow_shorts \
   and not in_midzone \
   and bearish_votes >= max(self.min_votes + 1, 4) \
   and total_score < -1.7 \
   and bb_pos >= self.range_exit_band:   # default 0.88
```

All four must fire simultaneously. Over 12 months of 5m BTC data that
combination is apparently vanishingly rare. Fixing this is a strategy
code change, not a config change — the gates are baked into the logic.

### 2. TP widening helps marginally but is not the dominant issue

WR goes 12.4% → 18.5% as TP widens 1.6 → 3.5 × ATR. That is a real
improvement but nowhere near the 30% target. Net P&L moves only $1.70.

Why so little effect? Because the strategy still takes only longs in
range regime, and the range-regime signal set appears near-random on
forward returns (consistent with AUC 0.503 finding). Wider TPs capture
more of the rare winners but don't change the underlying win/loss ratio
enough to matter.

### 3. Regime classifier unchanged by any of these fixes

All six configs emit regime=`range` for every trade. No config change
touches the regime classifier scale. A deeper intervention there is a
separate piece of work.

## Recommendation

Path 1 (quick config fixes) does not restore baseline sanity. The three
structural issues from FINDINGS-2026-04-18 are all deeper than I originally
judged:

- Shorts need strategy code changes, not just a config flag
- Widening TP helps but only in the second decimal place
- Regime classifier needs a completely separate investigation

**Prod bot remains stopped.** This is correct — re-enabling would resume
the slow bleed with no expectation of improvement.

**Three real options now**, ordered by scope:

**Option α — Deeper strategy work (days–weeks).** Loosen the short-entry
gates (drop bb_pos and bearish_votes requirements to test), investigate
why regime classifier only emits range, re-verify. Effective cost: at
least several days of strategy code work. Prize: possibly a usable
baseline before Plan A matters.

**Option β — Skip to Plan D (mean-reversion dedicated strategy) or E
(cross-sectional).** Build a new strategy from a known-edge template
rather than continue patching this one. Prod stays off until the new
strategy is ready. More work upfront, but higher probability of a
positive-expectancy outcome.

**Option γ — Accept the result, shut it all down.** Keep prod stopped
indefinitely. Document the lesson. Move on to other work. The ~$2
recovered over 12 months from any config fix is not worth the time to
deploy and monitor.

## What was already changed in this investigation

Nothing deployed. Prod config is untouched. Prod bot was stopped at
2026-04-17 22:42 UTC. All investigation work was local-only.
