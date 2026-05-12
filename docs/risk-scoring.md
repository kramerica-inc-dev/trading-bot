# Continuous weighted risk score — Fase 5

> Replaces the strategy's **binary** entry gates (`min_confidence` /
> `min_quality_score` / `min_regime_confidence` — sub-threshold signals were
> dropped entirely) with a **continuous** weighted risk score in `[0, 1]` that
> maps to a position-size multiplier, so a low-confidence signal becomes a
> *smaller* trade instead of no trade — but a score mapping to `0×` is still
> effectively "no trade", preserving the spirit of the old hard floors.
>
> Module: `scripts/risk_scoring.py` · wired into `scripts/advanced_strategy.py`
> behind `risk_scoring.enabled` (default **false**) · calibration:
> `backtest/calibrate_risk_score_weights.py` · run artifact:
> `backtest/results/risk_score_weights_calibration_20260512_183355.json`.
>
> **Funding rate is NOT an input** — it was a NO-GO in Fase 3
> (`docs/funding-analysis.md`).

## Components and normalizations

`compute_risk_score(signal, market_context, config) -> float` is the weighted
average of four components, each normalized to `[0, 1]`:

| Component | Source | Normalization |
|---|---|---|
| `confidence` | the signal's `confidence` | already in `[0, 1]` — clamped, no rescale |
| `quality_score` | the strategy's `_evaluate_trade_quality` score | already `[0, 1]` (clamped there); **re-normalized** against `[quality_min, 1.0]` (default `quality_min = 0.55`, the old gate threshold) so a quality score sitting on the old gate maps to ≈0 |
| `regime_confidence` | `_compute_regime_scores` confidence | already `[0, 1]`; **re-normalized** against `[regime_confidence_min, 1.0]` (default `0.40`, the old gate) |
| `mtf_alignment` | `htf_alignment_score` = 1h-state code + 4h-state code (each in `{-1, -0.5, 0, 0.5, 1}` → raw range `[-2, 2]`) | signed by trade side: `clip(0.5 + 0.25 · side_sign · htf_score, 0, 1)` — both HTFs agreeing with the trade → `1.0`; neutral HTFs → `0.5`; both HTFs against → `0.0` |

Weights default to **equal** (`0.25` each — option A in the plan, simpler and
lower overfit risk). They are config-tunable via `risk_scoring.weights` and are
normalized to sum to 1. Re-normalization floors are tunable via
`risk_scoring.quality_min` / `risk_scoring.regime_confidence_min`.

## Score → size curve

`score_to_size_multiplier(score, config) = clip(size_slope · score + size_intercept, 0.0, 1.0)`

Defaults `size_slope = 2.0`, `size_intercept = -0.5` (the plan's example):

| risk score | size multiplier |
|---|---|
| ≤ 0.25 | **0.0×** — no trade (the new low-end hard floor) |
| 0.50 | 0.5× |
| ≥ 0.75 | 1.0× |

When `risk_scoring.enabled = true`, the strategy's `risk_multiplier` is
multiplied by this size factor; the legacy gates (`low_regime_confidence`,
`quality_gate`, `risk_allocation_zero`) become **non-fatal** — instead of
rejecting, the signal is scaled. If the resulting `risk_multiplier ≤ 0` (e.g.
score ≤ 0.25, or a `chop`/`unclear` regime which already forces `0`), it is
recorded as a `risk_score_zero` rejection. The legacy gate code is **kept**
behind the flag for at least one release (fallback discipline).

When `risk_scoring.enabled = false` (the default) the strategy is byte-for-byte
unchanged — there is a regression test asserting this.

## Logging

When enabled, each entry signal carries `risk_score` (the scalar) and
`risk_score_components` (the `{component: normalized_value}` dict). The
backtester serializes these onto each `BacktestTrade` (`risk_score`,
`risk_score_components`) so post-hoc analysis can ask whether high-score trades
fared better. Components are also mirrored into `signal.indicators` as
`risk_score` / `risk_score_<component>`.

## Calibration result

`backtest/calibrate_risk_score_weights.py`: coarse 80-combo grid of
per-component weights `{0.0, 1.0, 2.0}⁴` (the all-zero combo dropped),
walk-forward (3 splits, 70/30 train/test), 80/20 search/holdout, Calmar
objective, deflated Sharpe, field-spread tabulated.

Run on the most-recent 30 000 5m candles (2026-01-03 → 2026-04-17; search
24 000 bars, holdout 6 000 bars, evaluated once), 1% risk per trade, 1×
leverage. *(The full 106 559-bar 5m series makes the 80-combo grid take ~90 min
because the deflated-Sharpe variance loop re-runs a full search-data backtest
per combo; `--max-bars 30000` keeps the coarse grid honest while finishing in
~30 min. Re-run uncapped if you want the whole series.)*

**The objective is essentially flat across the grid** — exactly the Fase-4
experience. Mean train-Calmar per combo ranges from −14.72 to −13.99: field
**μ = −14.18, σ = 0.136** over 80 combos. The "winner" is a `confidence`-only
weighting (`{confidence: 1, quality_score: 0, regime_confidence: 0,
mtf_alignment: 0}`), only `+1.36σ` above the field, and the **deflated Sharpe is
0.000** (P[SR > 0] after 80 trials). That is not a real edge — it is the best
draw from a tight noise field, and a single-component weighting is implausible
on its face. **Option B (Ridge/Lasso) was deliberately NOT done** — per the plan
it is only warranted if A doesn't converge; with a near-flat objective,
escalating to B would just overfit. Equal weights are retained as the
documented default.

*(Note: the calibrator's auto-recommendation heuristic flagged
`use_calibrated_weights=true` because the σ<0.05 "flat" threshold is too tight
for Calmar values around −14 — `σ/|μ| ≈ 1%` is flat in relative terms. The
honest verdict, applying judgement, is: keep equal weights. The script's
threshold is left as-is for transparency.)*

## A/B test — binary gates vs continuous score (holdout, evaluated once)

Holdout = 2026-03-28 → 2026-04-17, 6 000 bars. Continuous arm uses equal
weights and the default `2·score − 0.5` size curve.

| metric | binary gates | continuous score (equal weights) |
|---|---|---|
| trades | 8 | 8 |
| avg trade size (contracts) | 143.7 | 141.8 |
| Sharpe (per-bar) | −9.74 | −9.97 |
| Sharpe (per-trade) | −15.19 | −15.19 |
| Calmar | −12.92 | −13.09 |
| max DD | 2.07% | 2.05% |
| alpha vs buy-and-hold | −19.0% | −19.0% |
| win rate | 12.5% | 12.5% |
| total return | −1.76% | −1.77% |

On the full 24 000-bar search window the two arms are likewise near-identical
(40 trades each; Calmar −3.71 vs −3.72; alpha vs BH +17.1% both — the BH
benchmark itself crashed harder in this window). **The continuous score does not
produce "more trades with smaller average size" here** — with the strategy's
current signal distribution, the score → size curve almost never opens a trade
the old gates would have killed, so it is approximately a re-encoding of the old
gates. Calmar is not improved (a hair *worse* with equal weights, a hair
*better* with the noise-winner weights — both within rounding of the binary
baseline).

## Verdict — DO NOT ACTIVATE

`risk_scoring.enabled` stays **false**; the strategy keeps its binary gates.
Gate per `IMPROVEMENT_PLAN.md` ("Na Fase 5 A/B: continue score slechtere Calmar
dan binaire gates? Niet activeren, fallback behouden"): the continuous score
delivers **comparable, not better** Calmar — and brings no extra trades, no edge,
no DD reduction. Activating it would add complexity for zero measured benefit.

Why it's empty here, same as Fase 4: with the current entry filters the
strategy is deep in the red (search-window Calmar ≈ −3.7, return ≈ −10%, win
rate ~7–8%). A weighting/sizing transform can't manufacture a risk-adjusted
improvement on top of a losing signal distribution — you're re-scaling losses.
The continuous-risk-score machinery will only become a meaningful lever once the
underlying edge problem is fixed (Fase 6 bear-check, or upstream entry-logic
work). The infrastructure is in place and inert: re-run
`python3 -m backtest.calibrate_risk_score_weights` after a future edge
improvement to re-test, and flip `risk_scoring.enabled` only if the A/B then
shows a clear Calmar gain.

## Config plumbing (added, inert)

A top-level optional `risk_scoring` section was added to `config.example.json`
and wired through `scripts/advanced_strategy.py`
(`MultiIndicatorConfluence.__init__`, the `analyze()` entry block) and
`scripts/trading_bot.py` (`_build_strategy_with_live_profiles`). Default
`enabled: false` → zero behaviour change. The live `plan-e@*` runners on prod
don't use `advanced_strategy.py` at all, so they are unaffected regardless.
