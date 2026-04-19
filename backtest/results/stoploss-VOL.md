# Plan E variant — VOLATILITY-SCALED stop-loss (2× 30d hourly σ, per asset)

**Generated:** 2026-04-19T07:41:37.101398+00:00
**Base config:** lb=72h REV, rb=24h @ UTC 08:00, k_exit=6, 3L/3S, 10%/leg, cost/side=11.0bps
**Stop rule:** σ_24h = √24 · stdev(log r_1h over prior 720h); LONG=entry·exp(−2σ), SHORT=entry·exp(+2σ). Fallback 5% if σ unavailable.
**Range:** 2025-04-18 22:00:00+00:00 → 2026-04-18 21:00:00+00:00   (Train < 2026-01-01 | Test ≥ 2026-01-01)

## Delta vs baseline (no stop)

| Slice | Metric | Baseline | SL-VOL | Δ |
|-------|--------|----------|--------|---|
| Full | Sharpe | -0.61 | -1.26 | -0.64 |
| Full | Return % | -6.4% | -13.3% | -7.0pp |
| Full | CAGR % | -6.4% | -13.4% | -7.0pp |
| Full | Max DD % | -12.3% | -16.4% | -4.2pp |
| Train | Sharpe | -0.93 | -1.60 | -0.67 |
| Train | Return % | -6.9% | -12.3% | -5.4pp |
| Train | CAGR % | -9.7% | -17.1% | -7.4pp |
| Train | Max DD % | -11.6% | -14.6% | -2.9pp |
| Test (OOS) | Sharpe | +0.29 | -0.33 | -0.62 |
| Test (OOS) | Return % | +0.6% | -1.1% | -1.8pp |
| Test (OOS) | CAGR % | +2.2% | -3.7% | -5.9pp |
| Test (OOS) | Max DD % | -4.5% | -6.4% | -1.9pp |

**Turnover (SL-VOL):** 105.80 total across 297 rebalances | **Fees:** $535.16 | Baseline fees: $504.14

## Per-asset stop diagnostics

| Asset | Entries | Triggers | Trigger rate | Avg σ_24h | Avg stop dist | Fallback |
|-------|---------|----------|--------------|-----------|---------------|----------|
| BTC-USDT | 60 | 17 | 28.3% | 2.14% | 4.34% | 4 |
| ETH-USDT | 67 | 23 | 34.3% | 3.46% | 6.85% | 5 |
| SOL-USDT | 61 | 19 | 31.1% | 3.84% | 7.53% | 3 |
| XRP-USDT | 66 | 20 | 30.3% | 3.59% | 6.98% | 7 |
| BNB-USDT | 69 | 20 | 29.0% | 2.61% | 5.22% | 4 |
| DOGE-USDT | 61 | 22 | 36.1% | 4.43% | 8.34% | 8 |
| ADA-USDT | 58 | 19 | 32.8% | 4.12% | 8.06% | 3 |
| AVAX-USDT | 70 | 24 | 34.3% | 4.25% | 8.13% | 7 |
| DOT-USDT | 68 | 26 | 38.2% | 4.41% | 8.36% | 8 |
| LINK-USDT | 57 | 20 | 35.1% | 4.33% | 8.33% | 5 |

## Insights

- Full-period Sharpe Δ: -0.64; return Δ: -7.0pp; max-DD Δ: -4.2pp.
- OOS Sharpe Δ: -0.62 — stops help/hurt OOS specifically.
- Stop width ranges from BTC-USDT (4.3%) to DOT-USDT (8.4%) — vol-scaling gives high-vol names wider buffers as intended.
- Highest trigger rate: DOT-USDT (38.2%); lowest: BTC-USDT (28.3%).

## Verdict

**REJECT** — vol-scaled stops do not clear the bar (no OOS Sharpe gain and/or worse drawdown). Mean-reversion signal benefits from letting trades breathe; premature stops truncate the reversion leg.
