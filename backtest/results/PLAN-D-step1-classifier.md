# Plan D — Step 1: Chop Classifier Validation

**Gate result:** **PASS**  (best test AUC: 0.6436)

**Generated:** 2026-04-18T06:28:32.628226+00:00
**Split:** train before 2026-01-12, test after
**Model:** L2 logistic regression (C=1.0)

## Gate criteria

| AUC | Decision |
|-----|----------|
| > 0.55 | PASS — proceed to Plan D step 2 |
| 0.52 – 0.55 | MARGINAL — try variants, record finding |
| ≤ 0.52 | FAIL — stop Plan D, escalate to Plan E or γ |

## Variant comparison

| Variant | Features | n_train | n_test | base_rate_test | AUC_train | AUC_test | Brier |
|---------|----------|---------|--------|----------------|-----------|----------|-------|
| A_n48_full | atr_pct,bb_width,adx14,hurst,autocorr_1 | 78820 | 27582 | 0.610 | 0.6592 | **0.6436** | 0.2236 |

## Best variant: A_n48_full

### Feature coefficients (standardized)

| Feature | Coefficient |
|---------|-------------|
| bb_width | -1.0408 |
| atr_pct | +0.7292 |
| autocorr_1 | -0.0969 |
| hurst | +0.0707 |
| adx14 | -0.0502 |

Positive coefficient: feature increases P(chop). Negative: decreases it.

### Test-set probability distribution

- mean: 0.627
- std:  0.165
- p10:  0.411
- p50:  0.667
- p90:  0.785

### Calibration (test set)

| bin_center | count | mean_pred | observed_freq |
|------------|-------|-----------|---------------|
| 0.05 | 532 | 0.043 | 0.209 |
| 0.15 | 409 | 0.152 | 0.279 |
| 0.25 | 635 | 0.255 | 0.324 |
| 0.35 | 1047 | 0.353 | 0.387 |
| 0.45 | 1880 | 0.456 | 0.459 |
| 0.55 | 3727 | 0.556 | 0.504 |
| 0.65 | 9435 | 0.656 | 0.650 |
| 0.75 | 7836 | 0.741 | 0.717 |
| 0.85 | 1859 | 0.836 | 0.725 |
| 0.95 | 222 | 0.930 | 0.721 |

A well-calibrated classifier has mean_pred ≈ observed_freq per bin.

### Confusion matrix at P(chop) > 0.5 threshold

- TN=2804  FP=7942
- FN=1699  TP=15137

### Confusion matrix at P(chop) > 0.6 threshold (strategy gate)

- TN=4653  FP=6093
- FN=3577  TP=13259
- precision @ 0.6: 0.685
- recall @ 0.6:    0.788

## Next step

Proceed to Plan D step 2: build mean-reversion strategy using this classifier as the regime gate at P(chop) > 0.6.
