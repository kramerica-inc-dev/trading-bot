# Bear-check / devil's-advocate module — Fase 6 (option A)

> Before each entry, evaluate the **opposing** case — the bearish case for a
> long, the bullish case for a short — and produce a single "counter-argument
> strength" score in `[0, 1]`. A high counter-argument maps to a *smaller*
> effective position size; it is **not** a hard block. It plugs into the
> continuous-score architecture from Fase 5: the resulting `size_multiplier` is
> applied on top of the Fase 5 risk-score sizing (or, when
> `risk_scoring.enabled` is false, directly onto `risk_multiplier`).
>
> Module: `scripts/bear_check.py` · wired into `scripts/advanced_strategy.py`
> behind `bear_check.enabled` (default **false**) · A/B test:
> `backtest/bear_check_ab.py` · run artifacts:
> `backtest/results/bear_check_ab_20260512_194802.json` (12k-bar holdout) and
> `backtest/results/bear_check_ab_20260512_194822.json` (24k-bar holdout).
>
> Only **option A** (deterministic checklist) is in scope. Option B (LLM
> periodic auditor) is explicitly *not* part of this plan — it would be a
> separate, event-triggered, dashboard-only audit layer, never in the
> trade pipeline.

## Components and normalizations

`compute_bear_check(signal, indicators, market_context, config) -> {"score",
"components", "size_multiplier", "weights", "raw_inputs"}` — the `score` is the
weighted average of four sub-scores, each normalized to `[0, 1]` where **higher
= stronger counter-argument against the proposed trade**:

| Component | What it captures | Normalization |
|---|---|---|
| `mtf_opposition` | the lower / entry timeframe agrees with the trade direction but the higher timeframes oppose it | `against = max(0, -side_sign · htf_alignment_score) / 2` (`htf_alignment_score` = 1h-state code + 4h-state code, each in `{-1, -0.5, 0, 0.5, 1}` → raw range `[-2, 2]`; `side_sign` = +1 long / −1 short). If `side_sign · entry_tf_state_code ≤ 0` (the entry TF does *not* itself agree with the trade), `against` is **halved** — the plan's premise is "LTF bullish but HTF bearish". Clamped to `[0, 1]`. |
| `recent_lower_highs` (long) / `recent_higher_lows` (short) | within a recent window of *closed* bars, swing structure works against the trade | from `market_context['recent_closes']` (closed-bar closes — no look-ahead; the strategy passes its `close_prices` window): split the last `structure_window` (default 20) closes into an earlier and a recent half. Long: `disp = max(0, max(earlier) − max(recent))` (lower highs). Short: `disp = max(0, min(recent) − min(earlier))` (higher lows). Normalized: `clip(disp / (ATR · structure_atr_full), 0, 1)` with `structure_atr_full` default `1.0` (a displacement of one full ATR = maximal counter-argument); if ATR is unavailable it falls back to `1%` of the price level. Alternatively, a precomputed `recent_structure_against ∈ [0, 1]` in the context/indicators is used as-is. |
| `bb_extreme_against` | price at a Bollinger extreme on the **wrong side** for the trade (a long near the upper band, a short near the lower band) | uses `bb_pos = (price − bb_lower) / (bb_upper − bb_lower) ∈ [0, 1]` when present. Long: `clip((bb_pos − bb_long_extreme) / (1 − bb_long_extreme), 0, 1)`, `bb_long_extreme` default `0.8`. Short: `clip((bb_short_extreme − bb_pos) / bb_short_extreme, 0, 1)`, `bb_short_extreme` default `0.2`. **Fallback** when `bb_pos` is absent (trend signals don't compute it): an RSI proxy from `indicators['rsi_value']` — long: `clip((rsi − 70) / 30, 0, 1)`; short: `clip((30 − rsi) / 30, 0, 1)`. (Thresholds `rsi_long_extreme` / `rsi_short_extreme` config-tunable.) |
| `loser_correlation` | similarity to recently-closed losing trades (same regime + same setup / `active_strategy`) | **STUBBED → `0.0`.** Threading the strategy's realized trade outcomes back into `analyze()` is invasive for the backtester's bar loop and out of scope for a 1-day phase. If `market_context['recent_loser_outcomes']` is supplied (a list of `{"regime", "active_strategy"}` dicts), the component returns the fraction sharing this trade's regime *and* active strategy — a cheap "same setup" proxy — but nothing supplies that today, so in practice it is always `0`. The component (and its weight) is kept so the wiring/logging is ready for a future iteration. |

**Funding-extreme is omitted.** It is in the plan's component list, but the
funding rate was a Fase 3 NO-GO (`docs/funding-analysis.md`): no statistically
significant, materially-sized, regime-robust signal on this data. Funding is not
used anywhere in this module — or anywhere else in the codebase.

Weights default to **equal** (`0.25` each — option A, simpler and lower-overfit
than learned weights). They are config-tunable via `bear_check.weights` and are
normalized to sum to 1; a zero-everything `weights` falls back to equal.

### A `hold`/unknown action, or all-zero components, yields `score = 0.0` →
`size_multiplier = 1.0` (no behaviour change).

## Strength → size curve

```
size_multiplier = clip(1.0 − score · max_penalty, min_floor, 1.0)
```

Defaults: `max_penalty = 1.0`, `min_floor = 0.0` (both config-tunable via the
`bear_check` section). So:

| `score` | `size_multiplier` (defaults) |
|---|---|
| 0.00 | 1.00× (no counter-argument) |
| 0.25 | 0.75× |
| 0.50 | 0.50× |
| 0.75 | 0.25× |
| 1.00 | 0.00× — a maximal counter-argument zeroes the trade |

It is a **soft** scaler, but a maximal counter-argument *can* effectively veto
(preserving the plan's "not a hard block, but can effectively block" spirit).
When the bear-check drives `risk_multiplier` to `0`, the strategy records a
`bear_check_veto` rejection — exactly like the other soft gates
(`risk_score_zero`, `quality_gate`, …).

`max_penalty = 0.0` (or `min_floor = 1.0`) makes the curve a no-op on sizing —
the enabled path then gates identically to the disabled path (covered by a
unit test).

## Wiring (default disabled → zero behaviour change)

In `MultiIndicatorConfluence.analyze()`, **after** the signal + `risk_multiplier`
(+ `risk_score`/`risk_score_components` if `risk_scoring.enabled`) are computed
and **before** the trade-spacing check:

1. `build_bear_check_record(signal, indicators, {"recent_closes": close_prices,
   "atr": atr}, bear_check_config)` — `close_prices` are the closed-bar window
   from `analyze()`'s look-ahead contract, so the structure check sees no future
   bar.
2. `signal.bear_check = {"score": …, "components": {…}}` (new `AdvancedSignal`
   field; also mirrored into `indicators` as `bear_check_score` /
   `bear_check_<component>` for diagnostics).
3. `signal.risk_multiplier *= bear_check.size_multiplier`.
4. If `risk_multiplier ≤ 0` → `_record_rejection('bear_check_veto')` and return
   a hold.

Composable with Fase 5 — both can be on; the bear-check applies *after* the
risk-score sizing. When `bear_check.enabled = false` the strategy is
byte-for-byte unchanged (regression test pins this).

The trade record also gains a `bear_check` field
(`AdvancedSignal.bear_check` → `BacktestTrade.bear_check`,
`{"score": float, "components": {…}}`), so post-hoc one can ask "did
high-bear-check-score trades actually fare worse?".

Config plumbing: an inert `bear_check` section in `config.example.json`,
surfaced to the strategy through `TradingBot._build_strategy_with_live_profiles`
exactly like the Fase 4 `regime_multipliers` / Fase 5 `risk_scoring` sections.

## A/B test (gate 5)

`python3 -m backtest.bear_check_ab --max-bars 12000 --risk-pct 1.0` — strategy
run twice on a bounded holdout window (the full 106k-bar 5m series takes ~1.5h;
this caps to the most-recent N candles, like the Fase 5 calibrator), once with
`bear_check.enabled=false` (A) and once `=true` (B). MTF resampling is disabled
in both arms (no HTF datasets fed here) so the only difference is the bear-check.
`risk_per_trade_pct=1.0` keeps risk-based sizing below the 100% notional cap so
the bear-check multiplier actually bites (at `5.0` the cap masks it — same
caveat as Fase 4/5).

### 12 000-bar holdout (2026-03-07 → 2026-04-17)

| arm | trades | avg size | Sharpe (bar) | Sharpe (trade) | Calmar | max DD | alpha-vs-BH | win rate | return |
|---|---|---|---|---|---|---|---|---|---|
| A — off | 15 | 137.45 | −12.06 | −17.41 | **−6.996** | 4.32% | −18.05% | 6.7% | −4.03% |
| B — on  | 15 | 136.35 | −11.95 | −17.41 | **−7.008** | 4.23% | −17.96% | 6.7% | −3.94% |
| Δ (B−A) | 0 | −1.10 | +0.11 | 0.00 | **−0.012** | −0.09 pp | +0.09 pp | 0.00 pp | +0.09 pp |

### 24 000-bar holdout (sanity, larger window)

| arm | trades | avg size | Sharpe (bar) | Calmar | max DD | alpha-vs-BH | win rate | return |
|---|---|---|---|---|---|---|---|---|
| A — off | 30 | 134.26 | −9.97 | **−3.722** | 7.62% | +6.21% | 10.0% | −7.33% |
| B — on  | 30 | 132.20 | −10.26 | **−3.728** | 7.48% | +6.35% | 10.0% | −7.19% |
| Δ (B−A) | 0 | −2.06 | −0.29 | **−0.006** | −0.14 pp | +0.14 pp | 0.00 pp | +0.14 pp |

The effect is **negligible**: trade count is unchanged (no bear-check ever drove
`risk_multiplier` to 0 — scores are tiny, see below), max DD shrinks by a tenth
of a percentage point only because a couple of positions are sized fractionally
smaller, and **Calmar gets marginally *worse*** in both windows. The plan's
expected pattern (lower trade volume, higher win rate, lower max DD) does **not**
materialise.

## Bear-check-score vs. outcome

On the B arm, 15 trades (12k) / 30 trades (24k) carried a `bear_check` record.
The score distribution is **degenerate-ish**: mean ≈ 0.10, std ≈ 0.10, range
`[0.00, 0.25]` — i.e. the maximal observed counter-argument is only `0.25`
(→ `size_multiplier` 0.75×), and most trades score `0`. With MTF resampling
disabled, `mtf_opposition` rarely fires meaningfully; `recent_lower_highs`
seldom reaches a full ATR of adverse displacement at entry; `bb_extreme_against`
via the RSI fallback is the only consistently non-zero contributor; and
`loser_correlation` is the stub (always 0).

Correlation of `bear_check.score` with realized PnL:

| window | Pearson(score, PnL) | Spearman(score, PnL) | low-tercile win% / mean PnL | high-tercile win% / mean PnL |
|---|---|---|---|---|
| 12k | **+0.19** | **+0.40** | 0.0% / −39.39 | 0.0% / −29.75 |
| 24k | **+0.11** | **+0.22** | 10.0% / −30.01 | 10.0% / −22.06 |

The correlation is **positive, not negative** — high-bear-check trades did, if
anything, *slightly less badly* than low-bear-check trades (every trade is a
loser at this win rate, so "less badly" is the only available verdict). That is
the **opposite** of the intended signal: the bear-check is not identifying the
worse trades. The terciles show no monotonic deterioration with score either.

## Verdict — **DO NOT ACTIVATE**

Per gate 5 of `IMPROVEMENT_PLAN.md` ("bear-check verlaagt Calmar of niet
meetbaar effect? → niet activeren, terug naar tekentafel"):

- **Calmar gets (marginally) worse**, not better, on both holdout windows.
- The bear-check score has **no useful relationship with trade outcomes** — the
  correlation is the wrong sign.
- The scores are tiny (≤ 0.25) so the layer barely moves position sizes anyway.

This is consistent with the broader reality of Fases 3–5: the strategy's
underlying entry edge is deeply negative (Calmar ≈ −1 to −14, win rate 7–12%,
alpha vs BH around −18% on this slice). A deterministic filter/sizing layer on
top of those signals cannot manufacture risk-adjusted improvement — and the
specific opposing-case heuristics here, in particular, don't even rank the
trades correctly.

`bear_check.enabled` stays **`false`**. The value delivered by this phase is the
documented analysis, the (inert) infrastructure, and the per-trade `bear_check`
logging field — so a future iteration (e.g. one that re-enables MTF context, or
implements the `loser_correlation` ring-buffer, or — most importantly — first
fixes the entry edge) can re-test cheaply with `backtest/bear_check_ab.py`.
