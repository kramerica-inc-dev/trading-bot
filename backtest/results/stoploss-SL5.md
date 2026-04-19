# Plan E SL-5 (symmetric 5% stop-loss) — backtest report

**Generated:** 2026-04-19T07:40:57.581395+00:00
**Universe:** BTC-USDT, ETH-USDT, SOL-USDT, XRP-USDT, BNB-USDT, DOGE-USDT, ADA-USDT, AVAX-USDT, DOT-USDT, LINK-USDT
**Range:** 2025-04-18 22:00:00+00:00 -> 2026-04-18 21:00:00+00:00
**Bars:** 8760 (1h)
**Config:** lb=72h, rb=24h @ UTC 08:00, sign=-1, k_exit=6, 10%/leg
**Friction:** fee 0.06% + slip 0.05% = 0.11% per side
**Stop:** symmetric 5% intrabar (long low, short high); fill at stop or worse-case open gap; flat until next rebalance.
**Train/Test split:** 2026-01-01

## Full-period comparison (12 months)

| Metric | Baseline (no stop) | SL-5 | Delta |
|--------|-------------------:|-----:|------:|
| Final equity | $4,681.74 | $4,309.80 | -371.94 |
| Return % | -6.37% | -13.80% | -7.44pp |
| CAGR % | -6.42% | -13.91% | -7.49pp |
| Sharpe | -0.61 | -1.22 | -0.60 |
| Max DD | -12.25% | -17.14% | -4.89pp |
| Trades/month | 21.9 | 25.7 | +3.78 |
| Total fees | $504.14 | $765.55 | +261.41 USD |

## Walk-forward split

| Slice | Metric | Baseline | SL-5 | Delta |
|-------|--------|---------:|-----:|------:|
| IS (train) | Sharpe | -0.93 | -1.22 | -0.29 |
| IS (train) | Max DD  | -11.63% | -12.64% | -1.01pp |
| IS (train) | Return  | -6.86% | -10.29% | -3.44pp |
| OOS (test) | Sharpe | +0.29 | -1.20 | -1.49 |
| OOS (test) | Max DD  | -4.53% | -8.88% | -4.36pp |
| OOS (test) | Return  | +0.64% | -3.80% | -4.44pp |

## Per-asset stop statistics (SL-5)

| Asset | n_legs | n_triggers | trigger_rate | total_loss_avoided ($) | avg_loss_avoided ($/trig) |
|-------|-------:|-----------:|-------------:|-----------------------:|--------------------------:|
| BTC-USDT | 60 | 14 | 23.3% | $+27.41 | $+1.96 |
| ETH-USDT | 75 | 31 | 41.3% | $+77.16 | $+2.49 |
| SOL-USDT | 75 | 36 | 48.0% | $-84.29 | $-2.34 |
| XRP-USDT | 74 | 27 | 36.5% | $+79.46 | $+2.94 |
| BNB-USDT | 71 | 26 | 36.6% | $+23.09 | $+0.89 |
| DOGE-USDT | 75 | 42 | 56.0% | $-7.27 | $-0.17 |
| ADA-USDT | 78 | 39 | 50.0% | $+0.97 | $+0.02 |
| AVAX-USDT | 88 | 46 | 52.3% | $-79.28 | $-1.72 |
| DOT-USDT | 84 | 44 | 52.4% | $+72.70 | $+1.65 |
| LINK-USDT | 67 | 34 | 50.7% | $-126.23 | $-3.71 |

## Top-5 asset-level insights

**Biggest beneficiaries (total_loss_avoided > 0 means stop helped):**
- XRP-USDT: 27 triggers / 74 legs (36.5%); total avoided $+79.46 (avg $+2.94/trigger)
- ETH-USDT: 31 triggers / 75 legs (41.3%); total avoided $+77.16 (avg $+2.49/trigger)
- DOT-USDT: 44 triggers / 84 legs (52.4%); total avoided $+72.70 (avg $+1.65/trigger)
- BTC-USDT: 14 triggers / 60 legs (23.3%); total avoided $+27.41 (avg $+1.96/trigger)
- BNB-USDT: 26 triggers / 71 legs (36.6%); total avoided $+23.09 (avg $+0.89/trigger)

**Worst sufferers (negative = stop fired too early, hurt PnL):**
- LINK-USDT: 34 triggers / 67 legs (50.7%); total avoided $-126.23 (avg $-3.71/trigger)
- SOL-USDT: 36 triggers / 75 legs (48.0%); total avoided $-84.29 (avg $-2.34/trigger)
- AVAX-USDT: 46 triggers / 88 legs (52.3%); total avoided $-79.28 (avg $-1.72/trigger)
- DOGE-USDT: 42 triggers / 75 legs (56.0%); total avoided $-7.27 (avg $-0.17/trigger)
- ADA-USDT: 39 triggers / 78 legs (50.0%); total avoided $+0.97 (avg $+0.02/trigger)

## Verdict

- Full-period Sharpe delta: **-0.60**
- OOS Sharpe delta: **-1.49**
- Full-period DD delta: **-4.89pp** (positive = smaller DD)
- OOS DD delta: **-4.36pp**
- Total stop triggers: 339 across 747 legs (45.4%)
- Sum loss_avoided (stop - hold) across all triggers: $-16.30

**Risk-profile label: REJECTED**
