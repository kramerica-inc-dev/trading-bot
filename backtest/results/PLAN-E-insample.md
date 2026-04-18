# Plan E (ε) — in-sample cross-sectional momentum backtest

**Generated:** 2026-04-18T07:47:21.496095+00:00
**Universe:** BTC-USDT, ETH-USDT, SOL-USDT, XRP-USDT, BNB-USDT, DOGE-USDT, ADA-USDT, AVAX-USDT, DOT-USDT, LINK-USDT
**Range:** 2025-04-18 08:00:00+00:00 -> 2026-04-18 06:00:00+00:00
**Bars:** 8759
**Deploy:** $5,000
**Signal:** trailing 24h return (log), rank cross-sectionally
**Entry:** long top 3 / short bottom 3, equal-weight, 10%/leg (gross exposure 60%)
**Rebalance:** every 4h on UTC hours [0, 4, 8, 12, 16, 20]
**Friction:** fee 0.06% + slippage 0.05% per side (= 0.11% per side, 0.22% round-trip)

## Results

| Metric | Value |
|--------|-------|
| Initial equity | $5,000.00 |
| Final equity | $1,475.00 |
| Net P&L | $-3,525.00 |
| Return % | -70.5% |
| Gross P&L | $-888.27 |
| Fees | $2,636.73 |
| Fee share (|fees/gross|) | 296.8% |
| Rebalances | 2184 |
| Sharpe (annualized) | -11.13 |
| Max drawdown | -70.6% |

## Gate

- Sharpe > 1.0: **FAIL** (-11.13)
- Net P&L > 0: **FAIL** ($-3525.00)
- Max DD > -15%: **FAIL** (-70.6%)

**Overall:** FAIL — re-parametrize or escalate to γ

## Turnover / fee distribution

- Turnover per rebalance: mean 0.38, median 0.40, max 1.20
- Fee per rebalance: mean $1.2073, max $4.8666
- Total rebalance cost: $2636.73 over 2184 events
