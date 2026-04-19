# Plan E SL-15 — fixed 15% stop-loss overlay

**Generated:** 2026-04-19T07:41:32.332216+00:00
**Universe:** BTC-USDT, ETH-USDT, SOL-USDT, XRP-USDT, BNB-USDT, DOGE-USDT, ADA-USDT, AVAX-USDT, DOT-USDT, LINK-USDT
**Range:** 2025-04-18 22:00:00+00:00 -> 2026-04-18 21:00:00+00:00 (8760 bars)
**Config:** lb=72h, rb=24h @ UTC08, k_exit=6, sign=REV, fee+slip=11bps/side
**SL-15 rule:** long stop at entry*0.85, short stop at entry*1.15; gap-through filled at bar.open; flat until next rebalance.

## Full-period comparison

| Metric | Baseline | SL-15 | Delta |
|--------|----------|-------|-------|
| Sharpe | -0.61 | -0.52 | +0.09 |
| Return % | -6.4% | -5.7% | +0.6pp |
| CAGR % | -6.4% | -5.8% | +0.6pp |
| Max DD % | -12.3% | -12.8% | -0.5pp |
| Final equity | $4,682 | $4,714 | $+32 |
| Rebalances | 261 | 278 | +17 |
| Sum turnover | 95.0 | 99.0 | +4.0 |
| Total fees | $504.14 | $536.36 | $+32.22 |
| Stop events | - | 46 | - |

## Walk-forward (train < 2026-01-01 | test >= 2026-01-01)

| Slice | Metric | Baseline | SL-15 | Delta |
|-------|--------|----------|-------|-------|
| TRAIN | Sharpe | -0.93 | -0.51 | +0.41 |
| TRAIN | Return % | -6.9% | -4.1% | +2.7pp |
| TRAIN | Max DD % | -11.6% | -9.8% | +1.9pp |
| TEST | Sharpe | +0.29 | -0.52 | -0.81 |
| TEST | Return % | +0.6% | -1.5% | -2.2pp |
| TEST | Max DD % | -4.5% | -6.6% | -2.1pp |

## Per-asset trigger stats (SL-15)

| Asset | Positions taken | Triggers | Trigger rate | Avg loss avoided (pct pts) |
|-------|-----------------|----------|--------------|-----------------------------|
| BTC-USDT | 56 | 1 | 1.8% | -2.46pp |
| ETH-USDT | 51 | 6 | 11.8% | +3.52pp |
| SOL-USDT | 48 | 6 | 12.5% | -1.71pp |
| XRP-USDT | 60 | 4 | 6.7% | -0.33pp |
| BNB-USDT | 58 | 2 | 3.4% | +0.16pp |
| DOGE-USDT | 50 | 6 | 12.0% | +0.61pp |
| ADA-USDT | 43 | 7 | 16.3% | -1.31pp |
| AVAX-USDT | 52 | 5 | 9.6% | +1.74pp |
| DOT-USDT | 53 | 6 | 11.3% | +1.46pp |
| LINK-USDT | 50 | 3 | 6.0% | +0.29pp |

Total stop events: 46

## Insights

- Total stop events across 12mo: 46 (8.8% of positions taken).
- Sharpe delta (full): +0.09. Return delta: +0.6pp. Max DD delta: -0.5pp (positive = shallower DD).
- OOS Sharpe delta: -0.81.
- Sum of per-event 'loss avoided' (stop pct return minus hold-to-next-rb pct return): +20.2pp across all triggers. Positive => stops were, on net, protective at leg level; negative => stops booked losses that would have reverted.

## Verdict

**INCONCLUSIVE — mixed signals across full-period and OOS; rerun with wider/volatility-scaled stop before deciding.**

- Full Sharpe improved by >=0.05: YES (+0.09)
- OOS Sharpe not worse: NO (-0.81)
- Max DD improved by >=1pp: NO (-0.5pp)
