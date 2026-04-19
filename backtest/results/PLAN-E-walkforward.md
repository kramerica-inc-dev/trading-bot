# Plan E — walk-forward validation

**Generated:** 2026-04-19T07:41:38.369094+00:00
**Signal config:** lb=72h, rb=24h, REV
**Split:** Train < 2026-01-01 | Test >= 2026-01-01
**Test period:** ~3.5 months (out-of-sample)

## Taker execution (cost/side = 11bps)

| k_exit | Train Sharpe | Train Return | Train DD | Test Sharpe | Test Return | Test DD |
|--------|--------------|--------------|----------|-------------|-------------|---------|
| 4 | -0.23 | -2.4% | -11.6% | **+2.52** | +7.1% | -2.7% |
| 5 | -0.49 | -4.4% | -12.3% | **+3.15** | +9.2% | -2.9% |
| 6 ← | +0.57 | +4.0% | -9.4% | **+1.68** | +4.3% | -3.9% |
| 7 | -0.07 | -0.9% | -12.9% | **+3.51** | +9.4% | -2.5% |
| 8 | -0.89 | -6.9% | -11.2% | **+3.79** | +9.6% | -2.8% |

Arrow marks the k selected by train Sharpe (k_exit=6).

## Maker-blend execution (F=0.5, cost/side ~5bps)

| k_exit | Train Sharpe | Train Return | Train DD | Test Sharpe | Test Return | Test DD |
|--------|--------------|--------------|----------|-------------|-------------|---------|
| 4 | +0.51 | +3.8% | -9.6% | **+3.57** | +10.2% | -2.0% |
| 5 | +0.13 | +0.6% | -10.8% | **+3.98** | +11.7% | -2.3% |
| 6 ← | +1.11 | +8.3% | -8.1% | **+2.41** | +6.3% | -3.1% |
| 7 | +0.35 | +2.3% | -12.0% | **+4.04** | +10.9% | -2.1% |
| 8 | -0.60 | -4.9% | -10.5% | **+4.19** | +10.6% | -2.6% |

## Verdict

Selected config: k_exit=6
- Taker OOS Sharpe: **+1.68** (gate: >0.5 for PASS)
- Maker(F=0.5) OOS Sharpe: **+2.41**
- Taker OOS Max DD: -3.9%

**Walk-forward: PASS**

Signal retains meaningful edge out-of-sample. Next: paper-trade per P1 policy (2-4 weeks) using taker execution as conservative baseline, with an option to switch to maker rebalancer for Sharpe improvement.
