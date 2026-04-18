# Plan E — η probe: maker execution sensitivity

**Generated:** 2026-04-18T20:51:05.920489+00:00
**Fixed config:** lb=72h, rb=24h, REV, k_exit=6
**Deploy:** $5,000

## Fill-rate sweep

Assumes fraction F of each rebalance's turnover fills at maker (fee -0.010%, slip 0.000%); remainder at taker (fee +0.060%, slip 0.050%).

| Fill rate F | Eff cost/side (bps) | Net P&L | Return % | Gross | Fees | Sharpe | Max DD |
|-------------|---------------------|---------|----------|-------|------|--------|--------|
| 0.0 | +11.00 | $+403.85 | +8.1% | $+968.35 | $564.50 | +0.81 | -9.4% |
| 0.3 | +7.40 | $+599.61 | +12.0% | $+986.24 | $386.63 | +1.16 | -8.6% |
| 0.5 | +5.00 | $+734.01 | +14.7% | $+998.41 | $264.40 | +1.39 | -8.1% |
| 0.7 | +2.60 | $+871.63 | +17.4% | $+1,010.78 | $139.16 | +1.62 | -7.6% |
| 0.9 | +0.20 | $+1,012.53 | +20.3% | $+1,023.36 | $10.83 | +1.86 | -7.1% |
| 1.0 | -1.00 | $+1,084.23 | +21.7% | $+1,029.73 | $-54.50 | +1.97 | -6.8% |

## Raw cost sensitivity

| Cost/side (bps) | Net P&L | Return % | Sharpe | Max DD |
|-----------------|---------|----------|--------|--------|
| +0.00 | $+1,024.42 | +20.5% | +1.88 | -7.0% |
| +1.00 | $+965.19 | +19.3% | +1.78 | -7.2% |
| +2.00 | $+906.54 | +18.1% | +1.68 | -7.4% |
| +3.00 | $+848.46 | +17.0% | +1.59 | -7.7% |
| +5.00 | $+734.01 | +14.7% | +1.39 | -8.1% |
| +7.00 | $+621.79 | +12.4% | +1.20 | -8.5% |
| +11.00 | $+403.85 | +8.1% | +0.81 | -9.4% |

## Gate-crossing threshold

**Minimum fill rate for gate pass (Sharpe>1, net>0, DD>-15%):** **F = 0.3** (Sharpe +1.16, net $+599.61)

If realistic maker fill rate on a 24h-cadence rebalance is >= 30%, ε passes the gate with maker engineering.
