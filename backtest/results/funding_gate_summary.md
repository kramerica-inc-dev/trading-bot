# Plan A — Step 3 funding-gate backtest summary

**Generated:** 2026-04-17T22:28:03Z
**Data window:** 2026-01-17 17:50:00+00:00 → 2026-04-17 17:45:00+00:00
**Baseline trade count:** 31

## Baseline (no gate)
- Sharpe: -15.579
- Win rate: 9.7%
- Expectancy / trade: $-0.30
- Gross P&L: $-5.46
- Net P&L:   $-9.42
- Fees:      $3.96 (72.7% of gross)
- Max DD:    8.32%

## Best gate (≤50% suppression, ranked by Sharpe)
- max_long: 0.0050%  min_short: -0.0050%
- Passed trades: 19 (suppression 38.7%)
- Passed Sharpe: -11.773 (Δ +3.806)
- Passed WR:     10.5%
- Passed expectancy: $-0.29 (Δ +0.02)
- Passed net P&L: $-5.43 (Δ +3.99)
- Fee share of gross: 79.6%
- Filtered: 12 (1 winners / 11 losers, net $-3.99)

## Regime breakdown (best gate)

| regime   |   baseline_trades |   passed_trades |   skipped_trades |   baseline_wr |   passed_wr |   skipped_wr |   baseline_pnl |   passed_pnl |   skipped_pnl |
|:---------|------------------:|----------------:|-----------------:|--------------:|------------:|-------------:|---------------:|-------------:|--------------:|
| range    |                31 |              19 |               12 |          0.10 |        0.11 |         0.08 |          -9.42 |        -5.43 |         -3.99 |

## Decision point

**PROCEED to Step 4-6.** The best gate improves Sharpe and expectancy without suppressing >50% of trades, and skips more losers than winners.

See `funding_gate_grid.csv` for the full sweep and `funding_gate_regime_grid.csv` for regime-level impact.