# Plan E — Regime classifier (Phase 1) walk-forward report

**Generated:** 2026-04-26T16:38:29.411497+00:00
**Data range:** 2025-04-18 22:00:00+00:00 → 2026-04-18 21:00:00+00:00
**Universe:** BTC-USDT, ETH-USDT, SOL-USDT, XRP-USDT, BNB-USDT, DOGE-USDT, ADA-USDT, AVAX-USDT, DOT-USDT, LINK-USDT
**Walk-forward:** train=6mo / test=3mo / step=3mo, 2 folds, hold_h=24 (right-edge leakage trim)
**Target:** binarized 24h basket forward return at q0.25 (long_n=3, short_n=3, signal_sign=-1)
**Features:** breadth_pos_72h, breadth_above_sma200, xs_dispersion_72h, btc_vol_ratio_24_720, btc_trend_strength, xs_rank_autocorr_72h

## Per-fold results

| Fold | Train range | Test range | Train n | Test n | Train q25 | Test pos rate | Test AUC |
|------|-------------|------------|--------:|-------:|-----------|--------------:|---------:|
| 1 | 2025-04-18 → 2025-10-18 | 2025-10-18 → 2026-01-18 | 3648 | 2208 | -0.0087 | 0.25 | **0.493** |
| 2 | 2025-07-18 → 2026-01-18 | 2026-01-18 → 2026-04-18 | 4392 | 2136 | -0.0093 | 0.18 | **0.460** |

## Feature importances (final-fold standardized coefficients)

| Feature | Coefficient |
|---------|------------:|
| breadth_above_sma200 | +0.461 |
| breadth_pos_72h | -0.408 |
| xs_rank_autocorr_72h | +0.193 |
| btc_vol_ratio_24_720 | -0.192 |
| btc_trend_strength | -0.067 |
| xs_dispersion_72h | -0.019 |

Coefficients are on standardized features; magnitude is comparable across rows. Positive = feature increases P(loss-tail).

## Verdict

**HARD FAIL** — at least one fold AUC < 0.52.

Per P3, this terminates Phase 1 outright. Document in DECISIONS.md and either revise the target definition or close the PRD.

**Final-fold artifact:** `scripts/models/regime_classifier_e_2026-01-18.joblib`
