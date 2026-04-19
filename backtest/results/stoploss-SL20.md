# Plan E — Fixed Symmetric 20% Stop-Loss (SL-20)

**Generated:** 2026-04-19T07:41:32.440374+00:00
**Range:** 2025-04-18 22:00:00+00:00 -> 2026-04-18 21:00:00+00:00  (0.99y)
**Baseline config:** lb=72h, rb=24h (UTC 08:00), sign=-1 (reversal), k_exit=6, 3L/3S, 10% per leg
**Variant:** +20% symmetric stop (long: low < entry*0.80; short: high > entry*1.20). Fill at stop level or bar open on gap. Stay flat until next rebalance.
**Fees:** 0.06% fee + 0.05% slip = 0.11% per side

## 1. Headline delta

### Full period

| Metric | Baseline | SL-20 | Delta |
|--------|----------|-------|-------|
| Return | -5.17% | -14.03% | -8.87% |
| Sharpe | -0.48 | -1.46 | -0.98 |
| Max DD | -10.48% | -18.64% | -8.15% |
| Final equity | $4,741.67 | $4,298.30 | $-443.37 |

### Walk-forward (Train < 2026-01-01 | Test >=)

| Slice | Metric | Baseline | SL-20 | Delta |
|-------|--------|----------|-------|-------|
| TRAIN | Return | -6.89% | -12.16% | -5.27% |
| TRAIN | Sharpe | -0.91 | -1.72 | -0.81 |
| TRAIN | Max DD | -10.48% | -16.03% | -5.55% |
| TEST(OOS) | Return | +1.90% | -2.08% | -3.98% |
| TEST(OOS) | Sharpe | +0.77 | -0.74 | -1.51 |
| TEST(OOS) | Max DD | -3.52% | -6.36% | -2.83% |

## 2. Tail-event protection

- **Worst hourly bar (portfolio-level return):** baseline -1.98% vs SL-20 -1.27%
- **Worst single stopped leg (SL-20, realized USD):** $-102.68
- **Total SL-20 triggers:** 30  (over 0.99y, 10 assets)
- **Fleet trigger rate:** 30.3/yr across universe (~3.03/yr per asset)

At 20% threshold a stopped long that fills at 0.80*entry realizes ~ -2% equity on the 10% leg (before gap and fees). So each trigger caps that leg's draw at roughly -$100 on $5,000 deploy; without SL the same leg could extend to -5% or -10% equity if the continuation is large.

## 3. Per-asset trigger table

| Symbol | n_triggers | trig/yr | avg_loss_usd | worst_leg_usd | gap_rate |
|--------|------------|---------|--------------|----------------|----------|
| BTC-USDT | 0 | 0.0 | +0.00 | +0.00 | 0.00 |
| ETH-USDT | 4 | 4.03 | -96.51 | -102.07 | 0.00 |
| SOL-USDT | 2 | 2.02 | -89.90 | -92.23 | 0.00 |
| XRP-USDT | 4 | 4.03 | -92.81 | -102.52 | 0.00 |
| BNB-USDT | 0 | 0.0 | +0.00 | +0.00 | 0.00 |
| DOGE-USDT | 5 | 5.04 | -98.77 | -100.44 | 0.00 |
| ADA-USDT | 5 | 5.04 | -96.27 | -102.68 | 0.00 |
| AVAX-USDT | 3 | 3.02 | -97.37 | -102.30 | 0.00 |
| DOT-USDT | 5 | 5.04 | -93.16 | -100.21 | 0.00 |
| LINK-USDT | 2 | 2.02 | -92.43 | -95.63 | 0.00 |

## 4. Trigger log (all events)

| timestamp | symbol | side | entry | stop | fill | leg_ret% | leg_pnl_usd | gap |
|-----------|--------|------|-------|------|------|----------|-------------|-----|
| 2025-05-08 20:00:00+00:00 | ETH-USDT | short | 1807.8800 | 2169.4560 | 2169.4560 | -20.00% | -100.55 | false |
| 2025-05-10 05:00:00+00:00 | DOT-USDT | short | 4.2340 | 5.0808 | 5.0808 | -20.00% | -100.21 | false |
| 2025-05-10 23:00:00+00:00 | DOGE-USDT | short | 0.2053 | 0.2464 | 0.2464 | -20.00% | -100.44 | false |
| 2025-06-05 16:00:00+00:00 | DOGE-USDT | long | 0.2269 | 0.1815 | 0.1815 | -20.00% | -100.17 | false |
| 2025-06-22 20:00:00+00:00 | ADA-USDT | long | 0.6460 | 0.5168 | 0.5168 | -20.00% | -102.31 | false |
| 2025-07-10 21:00:00+00:00 | ETH-USDT | short | 2450.9800 | 2941.1760 | 2941.1760 | -20.00% | -102.07 | false |
| 2025-07-17 18:00:00+00:00 | XRP-USDT | short | 2.7914 | 3.3497 | 3.3497 | -20.00% | -102.52 | false |
| 2025-07-18 00:00:00+00:00 | ADA-USDT | short | 0.7120 | 0.8544 | 0.8544 | -20.00% | -102.68 | false |
| 2025-07-19 19:00:00+00:00 | AVAX-USDT | short | 20.9490 | 25.1388 | 25.1388 | -20.00% | -102.30 | false |
| 2025-07-21 15:00:00+00:00 | DOGE-USDT | short | 0.2380 | 0.2856 | 0.2856 | -20.00% | -99.56 | false |
| 2025-07-30 19:00:00+00:00 | DOT-USDT | long | 4.5730 | 3.6584 | 3.6584 | -20.00% | -97.98 | false |
| 2025-08-03 00:00:00+00:00 | DOGE-USDT | long | 0.2357 | 0.1886 | 0.1886 | -20.00% | -97.88 | false |
| 2025-08-11 16:00:00+00:00 | ETH-USDT | short | 3632.0000 | 4358.4000 | 4358.4000 | -20.00% | -95.81 | false |
| 2025-08-12 14:00:00+00:00 | LINK-USDT | short | 19.1800 | 23.0160 | 23.0160 | -20.00% | -95.63 | false |
| 2025-09-13 01:00:00+00:00 | DOGE-USDT | short | 0.2329 | 0.2795 | 0.2795 | -20.00% | -95.80 | false |
| 2025-09-18 17:00:00+00:00 | AVAX-USDT | short | 28.5750 | 34.2900 | 34.2900 | -20.00% | -95.81 | false |
| 2025-10-10 21:00:00+00:00 | XRP-USDT | long | 2.9627 | 2.3702 | 2.3702 | -20.00% | -93.51 | false |
| 2025-10-10 21:00:00+00:00 | ADA-USDT | long | 0.8459 | 0.6767 | 0.6767 | -20.00% | -94.92 | false |
| 2025-10-10 21:00:00+00:00 | AVAX-USDT | long | 30.2810 | 24.2248 | 24.2248 | -20.00% | -94.00 | false |
| 2025-11-04 18:00:00+00:00 | ADA-USDT | long | 0.6393 | 0.5114 | 0.5114 | -20.00% | -91.94 | false |
| 2025-11-04 20:00:00+00:00 | SOL-USDT | long | 187.0400 | 149.6320 | 149.6320 | -20.00% | -92.23 | false |
| 2025-11-08 01:00:00+00:00 | DOT-USDT | short | 2.8290 | 3.3948 | 3.3948 | -20.00% | -90.91 | false |
| 2025-11-21 07:00:00+00:00 | ADA-USDT | long | 0.4932 | 0.3946 | 0.3946 | -20.00% | -89.51 | false |
| 2026-01-31 17:00:00+00:00 | DOT-USDT | long | 1.8870 | 1.5096 | 1.5096 | -20.00% | -89.27 | false |
| 2026-01-31 17:00:00+00:00 | LINK-USDT | long | 12.1820 | 9.7456 | 9.7456 | -20.00% | -89.23 | false |
| 2026-01-31 18:00:00+00:00 | XRP-USDT | long | 1.8780 | 1.5024 | 1.5024 | -20.00% | -88.19 | false |
| 2026-02-05 15:00:00+00:00 | SOL-USDT | long | 105.4700 | 84.3760 | 84.3760 | -20.00% | -87.56 | false |
| 2026-02-05 20:00:00+00:00 | ETH-USDT | long | 2314.7400 | 1851.7920 | 1851.7920 | -20.00% | -87.62 | false |
| 2026-02-05 20:00:00+00:00 | XRP-USDT | long | 1.4270 | 1.1416 | 1.1416 | -20.00% | -87.01 | false |
| 2026-02-25 20:00:00+00:00 | DOT-USDT | short | 1.3710 | 1.6452 | 1.6452 | -20.00% | -87.42 | false |

## 5. Insights

- Full-period Sharpe delta: -0.98. OOS Sharpe delta: -1.51. Max-DD delta: -8.15 pp.
- Trigger rate per asset-year is 3.03 — consistent with the 'tail only' hypothesis (target was 1-2/yr/asset).
- Non-trivial edge erosion: SL-20 cuts winners on volatility-reversal trades whose MFE briefly touches -20% before mean-reverting. Compare to looser (25%/30%) or time-based exits.

## 6. Verdict

**REJECT — SL-20 erodes edge without sufficient tail protection**

This is a paper-layer analysis only; live runner untouched per scope.
