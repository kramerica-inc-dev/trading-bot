# Regime-multiplier calibration — Fase 4

> Walk-forward calibration of the strategy's per-regime risk multipliers
> (`bull_trend`, `bear_trend`, `range`), optimising Calmar ratio. `chop` and
> `unclear` are held at `0.0` for this phase.
> Script: `backtest/calibrate_regime_multipliers.py`
> Latest run artifact: `backtest/results/regime_multiplier_calibration_20260512_150438.json`

## Setup

- Data: `backtest/data/BTC-USDT_5m.csv`, 106 559 bars (2025-04-12 → 2026-04-17).
- Search vs holdout split: first 80% search (85 247 bars, → 2026-02-02), last 20% holdout (21 312 bars, evaluated **once**).
- Grid: `{0.5, 1.0, 1.5}` on each of `bull_trend / bear_trend / range` → 27 combinations (coarse, no iterative refinement around the winner).
- Walk-forward inside the search window: 3 splits, 70/30 train/test, `min_trades = 5`.
- Objective: maximise Calmar ratio on the train portion. Deflated Sharpe reported alongside.
- Sizing: 1% risk per trade, 1× leverage.

## Result

**The objective is essentially flat across the grid.** Mean train-Calmar per combo ranges from −4.27606 to −4.27534 — a spread of **σ ≈ 0.00034** over all 27 combinations. The `range` multiplier nudges Calmar at the 4th decimal; `bull_trend` and `bear_trend` make no difference at all in this period. The "winner" (`bull_trend=0.5, bear_trend=0.5, range=1.0`, mean train-Calmar −4.275) is statistically indistinguishable from the field (`z_vs_field ≈ 0.71σ`), and deflated Sharpe is 0.0.

Per-split test-Calmar for the winners (−8.97, −9.75, −9.52) is far worse than train-Calmar (≈ −4.2 to −4.5) — the usual train/test gap, but here it just confirms there's no real signal to capture.

Holdout (evaluated once): winner Calmar −4.125 vs current multipliers −4.145 — a hair better, but the winner sits **outside 1σ of its own train distribution**, so the gate fails.

## Verdict — NO-GO

`regime_multipliers.enabled` stays **false**; the strategy keeps its current defaults (`bull_trend=1.0, bear_trend=0.8, range=0.55`). Gate per `IMPROVEMENT_PLAN.md` ("Na Fase 4 holdout: regime-multiplier winner buiten 1σ van train? Niet activeren, multipliers ongewijzigd"): not satisfied → not activated.

Why the calibration is empty here: with the current entry filters the strategy is so deep in the red (search-window Calmar ≈ −1.17, return ≈ −37%, win rate ~12%) that scaling position size up or down per regime can't move a risk-adjusted metric — you're scaling a losing distribution. Regime multipliers will only become a meaningful lever once the underlying edge problem (Fase 5 risk score, Fase 6 bear-check, or upstream entry-logic work) brings the strategy near break-even.

## Config plumbing (added, inert)

A top-level optional `regime_multipliers` section was added to `config.example.json` and wired through `scripts/advanced_strategy.py` (`MultiIndicatorConfluence.__init__`) and `scripts/trading_bot.py` (`_build_strategy_with_live_profiles`). When `enabled: true`, it overrides only the `bull_trend / bear_trend / range` entries of `regime_risk_multipliers`; `chop / unclear` are untouched. Default `enabled: false` → zero behaviour change. Re-run `python3 -m backtest.calibrate_regime_multipliers` after a future edge improvement to re-test.
