# Plan D — Step 3 (final probe): Conditional Chop Classifier

**Generated:** 2026-04-18T06:35:59.222114+00:00
**Hypothesis:** retraining the classifier on bars where |z|>=2.0 corrects a selection-bias flaw that caused the unconditional classifier's signal not to transfer to actual trade bars.

## Conditional training data

- Training bars (pre-split, |z|>=2.0): 7780
- Test bars (post-split, |z|>=2.0): 2839
- Conditional base rate (training): 0.273
- Unconditional base rate (training): 0.609

Conditional base rate is *lower* than unconditional — overextended bars are indeed less likely to mean-revert than random bars. This confirms the selection-bias hypothesis qualitatively.

## Classifier AUC

- Train AUC (conditional): 0.8041
- Test AUC (conditional):  0.7844
- (Unconditional test AUC from step 1: 0.6436 — for reference but not directly comparable)

## Strategy backtest with conditional classifier

| Gate | Trades | WR | W/L | Expectancy ($) | Net P&L ($) | Sharpe | Max DD |
|------|--------|-----|-----|----------------|-------------|--------|--------|
| p>0.50 | 54 | 20.4% | 0.70 | -0.240 | **-12.97** | -9.90 | -11.5% |
| p>0.60 | 26 | 19.2% | 0.89 | -0.231 | **-6.00** | -6.59 | -6.1% |
| p>0.70 | 12 | 8.3% | 0.55 | -0.313 | **-3.76** | -7.25 | -3.6% |
| p>0.80 | 2 | 0.0% | 0.00 | -0.387 | **-0.77** | -4.09 | -0.7% |

## Verdict

**NO MATERIAL IMPROVEMENT.** Conditional classifier does not rescue the strategy. Even with a selection-bias-corrected gate, the underlying reversion signal does not beat friction on 5m BTC-USDT at retail account size.

**Final Plan D verdict: FAIL.** Do not proceed to walk-forward validation. Document in DECISIONS.md and escalate to Plan E or γ.