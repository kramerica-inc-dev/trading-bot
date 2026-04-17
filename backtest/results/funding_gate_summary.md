# Plan A — Step 3 funding-gate backtest summary

**Generated:** 2026-04-17T22:34:32Z
**Data window:** 2025-04-12 22:35:00+00:00 → 2026-04-17 22:25:00+00:00
**Baseline trade count:** 185

## Baseline (no gate)
- Sharpe: -18.210
- Win rate: 12.4%
- Expectancy / trade: $-0.20
- Gross P&L: $-17.88
- Net P&L:   $-37.84
- Fees:      $19.96 (111.6% of gross)
- Max DD:    32.99%

## Best gate (≤50% suppression, ranked by Sharpe)
- max_long: 0.0150%  min_short: -0.0050%
- Passed trades: 146 (suppression 21.1%)
- Passed Sharpe: -16.194 (Δ +2.015)
- Passed WR:     11.6%
- Passed expectancy: $-0.20 (Δ +0.00)
- Passed net P&L: $-29.54 (Δ +8.30)
- Fee share of gross: 110.1%
- Filtered: 39 (6 winners / 33 losers, net $-8.30)

## Regime breakdown (best gate)

| regime   |   baseline_trades |   passed_trades |   skipped_trades |   baseline_wr |   passed_wr |   skipped_wr |   baseline_pnl |   passed_pnl |   skipped_pnl |
|:---------|------------------:|----------------:|-----------------:|--------------:|------------:|-------------:|---------------:|-------------:|--------------:|
| range    |               185 |             146 |               39 |          0.12 |        0.12 |         0.15 |         -37.84 |       -29.54 |         -8.30 |

## Decision point

**PROCEED to Step 4-6.** The best gate improves Sharpe and expectancy without suppressing >50% of trades, and skips more losers than winners.

See `funding_gate_grid.csv` for the full sweep and `funding_gate_regime_grid.csv` for regime-level impact.