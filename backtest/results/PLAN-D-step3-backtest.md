# Plan D — Step 3: 12-Month Backtest

**Generated:** 2026-04-18T06:32:39.059171+00:00
**Test slice:** 2026-01-12 00:00:00+00:00 → 2026-04-17 22:25:00+00:00 (27630 bars)
**Strategy config:** {'min_chop_prob': 0.6, 'z_entry': 2.0, 'z_stop': 3.5, 'sma_length': 20, 'rsi_period': 14, 'rsi_overbought': 70.0, 'rsi_oversold': 30.0, 'max_hold_bars': 48, 'allow_long': True, 'allow_short': True}
**Backtest config:** fee=0.0006 slip=0.05% risk=0.5%

## Headline metrics

| Metric | Plan D (test) | Baseline (12mo, FINDINGS) |
|--------|---------------|---------------------------|
| Trades | 403 | 185 |
| Long / Short | 217 / 186 | 185 / 0 |
| Win rate | 25.8% | 12.4% |
| Avg win / loss ($) | +0.214 / -0.289 | +0.054 / -0.241 |
| Win:loss ratio | 0.74 | 0.22 |
| Expectancy/trade ($) | -0.1589 | -0.205 |
| Net P&L ($) | -64.06 | -37.84 |
| Gross P&L ($) | -28.29 | — |
| Fees total ($) | 35.76 | — |
| Fee share of gross | 126.4% | — |
| Sharpe (annualized) | -20.70 | — |
| Max drawdown | -55.85% | — |

Baseline is the 12-month backtest of the previous advanced strategy, for context only. Plan D runs on out-of-sample slice, baseline ran on full 12 months.

## Gate criteria (Plan D step 3)

- FAIL — WR > 50%
- FAIL — W/L ratio >= 0.8
- FAIL — Expectancy > 0
- FAIL — Sharpe > 1.0
- FAIL — Max DD > -15%

**Overall:** FAIL

## Exit reason distribution

| Reason | Trades | Winners | WR |
|--------|--------|---------|-----|
| stop_loss | 288 | — | — |
| take_profit | 110 | — | — |
| max_hold_bars | 5 | — | — |

## Monthly P&L breakdown

| Month | Trades | Wins | WR | P&L ($) |
|-------|--------|------|-----|---------|
| 2026-01 | 62 | 18 | 29.0% | -11.69 |
| 2026-02 | 116 | 29 | 25.0% | -24.28 |
| 2026-03 | 144 | 37 | 25.7% | -19.81 |
| 2026-04 | 81 | 20 | 24.7% | -8.27 |

## Strategy rejection reasons (chop-gate, z-score, RSI)

| Reason | Count |
|--------|-------|
| z_not_extreme | 14574 (59.3%) |
| below_chop_prob | 7323 (29.8%) |
| rsi_not_oversold | 1351 (5.5%) |
| rsi_not_overbought | 1238 (5.0%) |
| z_past_stop_long | 42 (0.2%) |
| z_past_stop_short | 39 (0.2%) |
| no_precomputed | 13 (0.1%) |

Total rejections: 24580. Trades taken: 403. Signal-to-trade rate: 1.61%.

## Classifier coefficients (held from step 1 training)

| Feature | Coefficient |
|---------|-------------|
| bb_width | -1.0408 |
| atr_pct | +0.7292 |
| autocorr_1 | -0.0969 |
| hurst | +0.0707 |
| adx14 | -0.0502 |
