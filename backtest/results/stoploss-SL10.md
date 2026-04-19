# Plan E — SL-10 fixed symmetric 10% stop-loss evaluation

**Generated:** 2026-04-19T07:40:34.769288+00:00
**Config:** lb=72h, rb=24h, SIGN=-1 (REV), k_exit=6, 10%/leg, fee=0.0006, slip=0.0005, init=$5000
**Stop:** symmetric 10%, 1h OHLC granularity, gap-through handled at bar open, no re-entry until next rebalance

## Full-period delta

| Metric | Baseline | SL-10 | Delta |
|--------|----------|-------|-------|
| Final equity | $5,423.19 | $5,145.28 | $-277.92 |
| Return %     | +8.5% | +2.9% | -5.6pp |
| CAGR         | +8.5% | +2.9% | -5.6pp |
| Sharpe       | +0.85 | +0.33 | -0.52 |
| Max DD       | -9.4% | -8.8% | +0.6pp |
| Turnover (cum) | 98.80 | 106.10 | +7.30 |
| Fees (cum)   | $564.50 | $594.33 | $+29.83 |
| Rebalances   | 268 | 293 | — |

## Walk-forward (split 2026-01-01)

| Slice | Baseline Sharpe | SL-10 Sharpe | dSharpe | Baseline DD | SL-10 DD | dDD |
|-------|-----------------|---------------|---------|-------------|----------|-----|
| IS    | +0.57 | +0.28 | -0.30 | -9.4% | -8.8% | +0.6pp |
| OOS   | +1.68 | +0.50 | -1.18 | -3.9% | -6.5% | -2.7pp |

## Per-asset SL-10 activity

| Asset | Triggers | Trig rate (/rb) | Cum avoided (%eq) | Avg loss avoided/trig |
|-------|----------|-----------------|-------------------|------------------------|
| BTC-USDT | 2 | 0.007 | -0.26% | -0.13% |
| ETH-USDT | 15 | 0.051 | -0.08% | -0.01% |
| SOL-USDT | 13 | 0.044 | -0.42% | -0.03% |
| XRP-USDT | 14 | 0.048 | +1.05% | +0.08% |
| BNB-USDT | 4 | 0.014 | -0.55% | -0.14% |
| DOGE-USDT | 21 | 0.072 | -1.48% | -0.07% |
| ADA-USDT | 14 | 0.048 | -1.91% | -0.14% |
| AVAX-USDT | 14 | 0.048 | +0.20% | +0.01% |
| DOT-USDT | 16 | 0.055 | +1.02% | +0.06% |
| LINK-USDT | 10 | 0.034 | -0.29% | -0.03% |

## Top-5 asset insights (by |cum avoided|)

- **ADA-USDT**: 14 triggers, cum avoided -1.91% of equity (hurtful); avg per-trigger -0.14%
- **DOGE-USDT**: 21 triggers, cum avoided -1.48% of equity (hurtful); avg per-trigger -0.07%
- **XRP-USDT**: 14 triggers, cum avoided +1.05% of equity (helpful); avg per-trigger +0.08%
- **DOT-USDT**: 16 triggers, cum avoided +1.02% of equity (helpful); avg per-trigger +0.06%
- **BNB-USDT**: 4 triggers, cum avoided -0.55% of equity (hurtful); avg per-trigger -0.14%

## Verdict

**REJECTED**

SL-10 degrades the core signal. The 10% stop cuts reversals too early — leg often continues to mean-revert after the stop trigger, so we realize loss then miss recovery. Keep baseline (no stops).
