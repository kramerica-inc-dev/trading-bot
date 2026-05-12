# Axis C — Friction / Cost Drag

Scope: the 185 trades in `backtest/results/diag_trades.csv` (BTC-USDT 5m, 'advanced'
strategy, ~365-day baseline, $115 initial balance). Costs re-derived analytically
from `entry_price`/`exit_price`/`size` (fill prices in the CSV are already
slippage-adjusted) using the backtester cost model: `fee_rate=0.0006` per side on
notional, `slippage_pct=0.05` per fill, `contract_value=0.001` BTC/contract.
**Funding is not modelled** in `backtest/backtester.py` (no funding term anywhere in
`_calculate_pnl`); funding modelling lives only in the plan-e runner, so this
backtest understates real-world cost slightly.

## 1. Cost breakdown

| Item | $ | % of \|gross PnL\| ($1.25) | % of \|net PnL\| ($37.84) |
|---|---|---|---|
| Fees (entry+exit) | 19.96 | 1600% | 53% |
| Slippage (entry+exit) | 16.63 | 1334% | 44% |
| **Total friction** | **36.59** | **2932%** | **97%** |
| Funding | not modelled | — | — |

- Net PnL recorded: **−$37.84 (ROI −32.9%)**.
- Gross PnL on the *same trades* with zero costs: **−$1.25 (ROI −1.1%)**.
- So friction accounts for **$36.6 of the $37.8 loss (≈97%)**.
- Average trade notional: **$89.96** (≈0.8× the $115 balance — 5% SL-risk sizing).
- Cost per trade: **$0.198 = 0.220% of notional** ($0.108 fees + $0.090 slippage).
  Matches the ~0.22% round-trip estimate exactly (2×0.06% + 2×0.05%).

## 2. Per-trade gross move vs the cost floor

|gross move| = |exit_mid / entry_mid − 1| per trade:

| pct | p10 | p25 | p50 | p75 | p90 | p95 | mean |
|---|---|---|---|---|---|---|---|
| % | 0.042 | 0.086 | **0.132** | 0.195 | 0.258 | 0.293 | 0.146 |

- Round-trip cost floor: **0.220%**.
- **Median gross move 0.132% — only 60% of the cost floor.**
- **Dead-on-arrival: 151/185 trades (82%) have a gross move smaller than the 0.22%
  round-trip cost** — i.e. even a perfect-direction call loses money. 73/185 (39%)
  don't even cover *one* side's cost (0.11%).
- The strategy is structurally fighting a cost floor it almost never clears: holds
  are ~3.7 bars (≈18 min) and SL/TP brackets are tighter than the cost.

## 3. Counterfactuals (same 185 trades, same sizes)

| Scenario | PnL | ROI | Win rate |
|---|---|---|---|
| (a) zero cost | −$1.25 | −1.1% | 45.9% |
| (b) half cost | −$19.5 | −17.0% | 25.4% |
| (c) realistic (actual) | −$37.84 | −32.9% | 12.4% |

With zero costs the strategy is **break-even-to-slightly-negative**, not profitable.
Win rate jumps 12.4% → 45.9% purely because so many trades flip from "tiny loss after
costs" to "tiny win" — confirming most outcomes hug zero. Halving costs only halves
the bleed; it does not create an edge.

## 4. Turnover vs no-edge

- Turnover: 185 trades / 365 d ≈ **0.51/day**, mean hold ≈ 3.7 bars.
- **Gross expectancy per trade (mean signed mid-to-mid move): −0.0082%** (median
  −0.042%). Gross $ per trade: −$0.007.
- Gross expectancy is **≈ 0, marginally negative** — the entry signal has *no
  per-trade edge even before costs*. It is not a positive-but-sub-0.22% edge that a
  turnover/hold-time fix could rescue; there is essentially nothing to scale up.

## Implications for go/no-go

- **Costs are not the root cause — they are the accelerant.** Friction turns a ~0%
  gross strategy into a −33% net one, but the underlying edge is ≈0 (gross expectancy
  −0.008%/trade, 82% of trades dead-on-arrival).
- **Fix (1) "fix exit structure" — partial value at best.** Wider TP / longer holds
  would let *some* trades clear the 0.22% floor, but with zero gross edge a better
  exit just spreads ~0 over fewer, bigger trades — break-even, not profit. Worth
  doing only as a component of a redesign, not on its own.
- **Fix (2) "redesign entry signal" — necessary condition.** Any viable version of
  this family needs an entry whose gross expectancy is comfortably above ~0.22–0.3%
  (so it survives costs *and* funding, which this backtest ignores). The current
  MultiIndicatorConfluence entry produces moves centered on 0.13% — a factor ~2 short
  even gross. That's a from-scratch signal problem, not a tuning problem.
- **Fix (3) "abandon strategy family" — strongly supported by this axis.** Combined
  with Fases 3/4/5/6 all NO-GO, the friction picture says the 5m high-turnover,
  tight-bracket confluence approach is structurally mismatched to its cost floor. If
  the entry redesign in (2) cannot demonstrably produce >0.3% gross expectancy
  per trade out-of-sample, abandon the family.
