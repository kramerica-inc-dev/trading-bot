# Plan E — stoploss variant: TRAILING 10% (activate after +5%)

**Generated:** 2026-04-19T07:41:34.983969+00:00
**Config:** lookback=72h, sign=-1 (reversal), k_exit=6, leg=10%, rebalance=24h @ UTC 08:00, start=$5,000
**Costs:** fee=0.0006 + slip=0.0005 = 0.0011 per side
**Stop rule:** trail=10%, activation gain=+5% (long) / -5% (short); fills at stop or at open on gap; flat until next rebalance.

## Full-period: baseline (no stop) vs TRAIL-SL

| Metric | Baseline | TRAIL-SL | Δ |
|--------|---------:|---------:|---:|
| Final equity | $4,681.74 | $4,804.01 | $122.27 |
| Return % | -6.37% | -3.92% | +2.45% |
| CAGR % | -6.42% | -3.95% | +2.46% |
| Sharpe | -0.615 | -0.333 | +0.282 |
| Max DD % | -12.25% | -11.67% | +0.58% |
| Rebalances | 261 | 267 | 6 |
| Fees paid | $504.14 | $539.01 | $+34.87 |

## Walk-forward (train < 2026-01-01, test >= 2026-01-01)

### Train (IS)

| Metric | Baseline | TRAIL-SL | Δ |
|--------|---------:|---------:|---:|
| Return % | -6.86% | -6.26% | +0.59% |
| Sharpe | -0.927 | -0.799 | +0.128 |
| Max DD % | -11.63% | -11.67% | -0.04% |

### Test (OOS)

| Metric | Baseline | TRAIL-SL | Δ |
|--------|---------:|---------:|---:|
| Return % | +0.64% | +2.61% | +1.97% |
| Sharpe | +0.287 | +0.973 | +0.687 |
| Max DD % | -4.53% | -3.08% | +1.45% |

## Per-asset trigger stats

`avg_lock_in_gain` = mean PnL at stop − PnL had we held to next rebalance close (positive = stop saved us). Units: USDT per trigger.

| Symbol | Triggers | Rate /1k bars | Avg stop PnL ($) | Avg lock-in ($) | Total lock-in ($) |
|--------|---------:|--------------:|-----------------:|----------------:|------------------:|
| BTC-USDT | 3 | 0.35 | -7.51 | +0.15 | +0.44 |
| ETH-USDT | 2 | 0.23 | -10.70 | +1.46 | +2.91 |
| SOL-USDT | 5 | 0.58 | -6.84 | +2.25 | +11.26 |
| XRP-USDT | 7 | 0.81 | -10.37 | -2.96 | -20.72 |
| BNB-USDT | 2 | 0.23 | -1.00 | +0.37 | +0.75 |
| DOGE-USDT | 8 | 0.92 | -7.10 | +2.20 | +17.63 |
| ADA-USDT | 3 | 0.35 | -4.11 | +4.61 | +13.84 |
| AVAX-USDT | 5 | 0.58 | -9.71 | +0.19 | +0.95 |
| DOT-USDT | 7 | 0.81 | -9.41 | +0.07 | +0.49 |
| LINK-USDT | 4 | 0.46 | -17.28 | -2.45 | -9.81 |
| **TOTAL** | **46** | — | — | — | **+17.72** |

## Insights

- Full period: Δ Sharpe +0.282, Δ MaxDD +0.58%, Δ Return +2.45%.
- OOS: Δ Sharpe +0.687, Δ MaxDD +1.45%.
- Triggers: 46 total across 10 assets. Aggregate lock-in gain vs held-to-rebalance: $+17.72.
- Extra fees paid (vs baseline): $+34.87.

## Verdict

LEAN PROMOTE — Sharpe up meaningfully, DD roughly flat.

### Thesis check

Thesis: 'trailing locks in winners that would have reverted, without cutting losers prematurely (stop only arms after +5% favorable move).'

- Aggregate lock-in (stop-PnL − held-PnL across triggers) = $+17.72 → positive.
- If lock-in ≫ extra fees, thesis supported; if ~equal or negative, trail just churns.

_No existing fixed-SL report found in `backtest/results/` at time of writing; direct comparison pending. Baseline here is the no-stop variant._