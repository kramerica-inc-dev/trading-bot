# Plan D-ζ: Higher-timeframe + target-aligned classifier

**Generated:** 2026-04-18T06:58:59.705671+00:00
**Deploy size modeled:** $5,000 (0.5% risk/trade)
**Target redefined:** strategy-outcome-aligned (crosses SMA20 before z-stop, conditional on |z|>=z_entry)
**Timeframes tested:** 15m, 1h (resampled from 5m)

## Classifier performance per timeframe

| TF | n_train | n_test | base_train | base_test | AUC_train | AUC_test |
|----|---------|--------|------------|-----------|-----------|----------|
| 15m | 2744 | 976 | 0.297 | 0.296 | 0.5688 | **0.5430** |
| 1h | 706 | 268 | 0.290 | 0.213 | 0.5174 | **0.6152** |

For comparison: Plan D step 1 unconditional AUC_test = 0.6436; conditional (on |z|>=2) AUC_test = 0.7844. Both used a *misaligned* target — these new numbers use the strategy-aligned target, so higher is not automatic.

## Strategy backtest sweep per timeframe

### 15m

| Gate | Trades | Long/Short | WR | W/L | Expectancy ($) | Net P&L ($) | Sharpe | Max DD | Fee share |
|------|--------|------------|-----|-----|----------------|-------------|--------|--------|-----------|
| p>0.50 | 0 | 0/0 | 0.0% | 0.00 | +0.000 | **+0.00** | 0.00 | 0.0% | 0% |
| p>0.55 | 0 | 0/0 | 0.0% | 0.00 | +0.000 | **+0.00** | 0.00 | 0.0% | 0% |
| p>0.60 | 0 | 0/0 | 0.0% | 0.00 | +0.000 | **+0.00** | 0.00 | 0.0% | 0% |
| p>0.65 | 0 | 0/0 | 0.0% | 0.00 | +0.000 | **+0.00** | 0.00 | 0.0% | 0% |
| p>0.70 | 0 | 0/0 | 0.0% | 0.00 | +0.000 | **+0.00** | 0.00 | 0.0% | 0% |

**Best:** gate>0.50 with net $+0.00

### 1h

| Gate | Trades | Long/Short | WR | W/L | Expectancy ($) | Net P&L ($) | Sharpe | Max DD | Fee share |
|------|--------|------------|-----|-----|----------------|-------------|--------|--------|-----------|
| p>0.50 | 0 | 0/0 | 0.0% | 0.00 | +0.000 | **+0.00** | 0.00 | 0.0% | 0% |
| p>0.55 | 0 | 0/0 | 0.0% | 0.00 | +0.000 | **+0.00** | 0.00 | 0.0% | 0% |
| p>0.60 | 0 | 0/0 | 0.0% | 0.00 | +0.000 | **+0.00** | 0.00 | 0.0% | 0% |
| p>0.65 | 0 | 0/0 | 0.0% | 0.00 | +0.000 | **+0.00** | 0.00 | 0.0% | 0% |
| p>0.70 | 0 | 0/0 | 0.0% | 0.00 | +0.000 | **+0.00** | 0.00 | 0.0% | 0% |

**Best:** gate>0.50 with net $+0.00

## Verdict

**Best overall:** 15m at gate>0.50
- WR: 0.0%
- W/L: 0.00
- Expectancy: +0.0000
- Net P&L: $+0.00
- Sharpe: 0.00
- Max DD: 0.0%
- Fee share: 0%

**Gate:** FAIL

Even with target alignment and higher timeframe, the mean-reversion framework does not clear the step-3 gate. Rely on Plan E (cross-sectional) as the primary track.