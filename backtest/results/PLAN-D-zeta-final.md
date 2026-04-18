# Plan D-ζ — Final verdict: FAIL, mean reversion is structurally dead

**Date:** 2026-04-18
**Scope:** 15m + 1h BTC-USDT, target-aligned classifier, $5k deploy size
**Status:** ζ closes out. Mean reversion not viable at retail timeframes. ε (cross-sectional) is now the sole active track.

## What ζ tested

Two probes in one harness:
1. **Higher timeframe:** resample 5m BTC-USDT to 15m and 1h (friction amortizes over larger bar moves).
2. **Target-aligned classifier:** redefine the chop classifier's target to be the strategy's actual success event — `target=1 iff price crosses SMA20 BEFORE |z| exceeds z_stop within max_hold_bars, given entry at |z|>=z_entry`. Train conditionally only on bars where entry is possible.

This probe was designed to rescue Plan D if either of the two identified failure modes (TF-bound friction, classifier target misalignment) was the binding constraint.

## Result

### Classifier performance (strategy-aligned target, conditional training)

| TF  | n_train | n_test | base_test | AUC_test |
|-----|---------|--------|-----------|----------|
| 15m | 2744    | 976    | 0.296     | 0.5430   |
| 1h  | 706     | 268    | 0.213     | 0.6152   |

AUC at 1h (0.62) is comparable to Plan D's original unconditional AUC (0.64). The target alignment did not produce a meaningfully better classifier.

### Conditional hit rate — the binding constraint

Predicted p_chop distribution on the TEST set (where the strategy would actually trade):

| TF  | min p | max p | mean | Bars p>0.30 | Bars p>0.35 | Bars p>0.40 | Bars p>0.45 |
|-----|-------|-------|------|-------------|-------------|-------------|-------------|
| 15m | 0.036 | 0.441 | 0.281 | 428 (44%)   | 89 (9%)     | 6 (0.6%)    | 0           |
| 1h  | 0.250 | 0.465 | 0.311 | 165 (62%)   | 21 (8%)     | 9 (3.4%)    | 2 (0.7%)    |

Hit rate (fraction where strategy-aligned target = 1) among bars passing each gate:

| Gate | 15m n / hit | 1h n / hit |
|------|-------------|------------|
| p>0.30 | 428 / **33.0%** | 165 / 26.1% |
| p>0.35 | 89 / 30.3% | 21 / 23.8% |
| p>0.40 | 6 / 0.0% | 9 / 11.1% |
| p>0.45 | — | 2 / 50.0% (n too small) |

**The hit rate does not exceed ~33% even at the classifier's most confident bars.** This is the structural ceiling.

## Why mean reversion is dead at these timeframes

Expected value of a mean-reversion trade with z_entry=2.0, z_stop=3.5, TP=mean:

- Win size (in std-units): z_entry − 0 = **2.0**
- Loss size (in std-units): z_stop − z_entry = **1.5**
- W/L geometry: 1.33

At hit rate 33% (best case from data):
```
EV = 0.33 × 2.0 − 0.67 × 1.5 = 0.66 − 1.00 = −0.34 std-units
```

**Negative expectancy before fees at the CEILING of what the classifier can achieve.**
Fees (~0.22% round-trip) make it worse but are not the primary issue.

For positive EV with this geometry, we'd need hit rate ≥ 1.5/(2.0+1.5) = **42.9%**. The data shows the classifier cannot get there — the 95th-percentile-confidence bars on the test set have hit rate in the 20-30% range. Overextended bars (|z|>=2) are systematically less likely to revert than random bars, and no amount of feature engineering on the classifier side can flip that base-rate disadvantage.

## What this rules out

- Any variant of Plan D-v3 (δ) that relies on a better classifier over the same entry-condition subpopulation.
- Mean reversion on BTC-USDT at 5m, 15m, or 1h with any reasonable z_entry/z_stop geometry at retail account size.
- Not tested but implied: even larger account size doesn't fix this — the ceiling is structural, not frictional.

## What this does NOT rule out

- Cross-sectional momentum on a multi-asset universe (ε) — different alpha class, different base rate, relative-value rather than level-reversion.
- Higher-timeframe single-asset strategies on daily/weekly bars (different market regime, not tested).
- Maker-only execution (Plan B) on a different signal — still applies to whatever ε produces.

## Decision

**ζ closes: FAIL.** Plan E (ε) is the sole active track. No further time on mean reversion without new evidence.

Plan D-v3 / option δ is off the table.
Shutdown (γ) remains the fallback if ε also fails.
