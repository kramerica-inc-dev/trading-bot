# Look-ahead audit — primary-timeframe feature engineering

**Scope**: Fase 2 of `IMPROVEMENT_PLAN.md`. Audit of `scripts/advanced_strategy.py`
(`MultiIndicatorConfluence`) for look-ahead bias in primary-TF feature computation.
Higher-timeframe look-ahead is handled separately by `HTFCandleSync` /
`set_htf_candles()` and is out of scope here.

**Verdict (summary)**: **No look-ahead found.** All primary-TF indicators and
features are causal — pure functions of the closed-bar window the backtester
feeds them. Comments documenting bar-handling were added at each indicator/feature
site. A shift-by-one backtest and a per-indicator prefix-stability test suite
(`tests/test_lookahead_discipline.py`) confirm this. Two cosmetic boundary notes
below need a decision before Fase 3+, but neither is a backtest-contaminating
look-ahead.

---

## The execution convention (why "current bar" is unambiguous here)

The backtester (`backtest/backtester.py`, `Backtester.run`) loops:

```python
for i in range(lookback, len(candles)):
    window = candles[i - lookback:i]        # closed bars only — bar i is NOT included
    current = candles[i]
    current_price = float(current[4])       # close of bar i
    ...
    signal = self.strategy.analyze(window, current_price)
    # if signal acts: entry executes at current_price (close of bar i), with slippage
```

So inside `analyze(candles, current_price)`:

- `candles` contains **only closed bars**; `candles[-1]` is bar `t-1`.
- `current_price` is the live/last price — in the backtest, the close of bar `t`,
  which is also the price the order executes at.

This matches the live runners: `async_runtime.on_market_tick` and
`trading_bot._evaluate_entry` call `strategy.analyze(candles, current_price)` where
`current_price` is the latest ticker price and `candles` is the cached series.
The standard live convention "compute signals from closed bars, act on the latest
price" is therefore faithfully reproduced by the backtester. No indicator can see
bar `t` or beyond, because it is never passed in.

A `LOOK-AHEAD CONTRACT` comment block was added at the top of `analyze()` stating
this requirement.

> **Live/backtest consistency note (informational, not a backtest look-ahead):**
> in live mode the exchange candle endpoint may return the *in-progress* bar as the
> last element of `candles`. The backtester deliberately does not. This is a
> live-execution concern (the live runner should drop the unconfirmed bar before
> calling `analyze`, or accept that `candles[-1]` live ≈ `current_price` anyway) —
> it does **not** affect backtest validity, which is what Fase 2 gates on. Flagged
> for the live-executor work, not fixed here.

---

## Per-indicator / per-feature findings

All "evidence" entries reference tests in `tests/test_lookahead_discipline.py` and
the shift-by-one run described at the bottom.

| Feature / function | Window used | Status | Evidence |
|---|---|---|---|
| `calculate_rsi` | full supplied closed-bar series `[.. : t-1]` (Wilder smoothing) | clean | `test_rsi_prefix_stable` |
| `calculate_macd` | EMA(fast)−EMA(slow) + signal EMA over closed bars `[.. : t-1]` | clean | `test_macd_prefix_stable` |
| `calculate_bollinger_bands` | SMA ± k·std over last `bb_period` closed bars `[t-bb_period : t-1]` | clean | `test_bollinger_prefix_stable` |
| `calculate_atr` | mean TR over last `atr_period` closed bars `[t-atr_period : t-1]` | clean | `test_atr_prefix_stable` |
| `calculate_volume_signal` | last closed bar's volume vs mean over `[t-volume_period : t-1]` — "current" = closed bar `t-1`, not the live bar | clean | `test_volume_signal_prefix_stable` |
| `_ema`, `_ema_series` | EMA over supplied closed series | clean | covered transitively via MACD / regime tests |
| `_efficiency_ratio` | Kaufman ER over last `period+1` closed bars | clean | `test_efficiency_ratio_prefix_stable` |
| `_slope_pct` | %change `values[t-period] → values[t-1]` (closed-bar values) | clean | `test_slope_pct_prefix_stable` |
| `detect_market_regime` (EMAs, trend_bias, anchor_bias, anchor_slope, ER, atr_pct, persistence, bull/bear/range/chop scores, regime_confidence) | all derived from the closed-bar window `[.. : t-1]`; `candles[-1]` = bar `t-1` | clean | `test_regime_metrics_prefix_stable` |
| `_trend_state_from_candles` (per-TF regime state for MTF) | closed-bar series of that TF (HTF series are pre-filtered to closed bars by `set_htf_candles` / resampling of base closed bars) | clean (HTF discipline owned by `HTFCandleSync`) | exercised via `analyze` in shift-by-one + `test_dynamic_tf_integration.py` |
| `build_multi_timeframe_context` / `_finalize_regime_with_mtf` / `_mtf_entry_confirms` | consume the closed-bar HTF context only | clean (HTF discipline owned by `HTFCandleSync`) | shift-by-one run |
| `_compute_regime_scores` (bull/bear/range/chop scores + regime confidence) | pure function of `regime_metrics` (closed-bar derived) + closed-bar HTF states | clean | shift-by-one run; no raw candle access |
| `_trend_long_signal` / `_trend_short_signal` entry triggers (trend filter, RSI filter, MACD sign, pullback/breakout zone, `recent_high`/`recent_low` over `prices[-breakout_lookback:]`, `close_strength`/`close_weak` on `candles[-1]`) | closed-bar window for all signal inputs; `current_price` for breakout/pullback distance and SL/TP; entry executes at `current_price` | clean — boundary case A below | shift-by-one run; `test_analyze_is_pure_on_window` |
| `_range_signal` entry triggers (BB position, RSI band, MACD-with-band, reversal bar, volume, entry-TF neutrality, midzone block) | BB/RSI/MACD from closed bars; `bb_pos` and `reversal_bar` use `current_price`; `reversal_bar` compares `current_price` to `candles[-2][4]` | clean — boundary cases A and B below | shift-by-one run |
| `_evaluate_trade_quality` (RR ratio, atr_pct, volume score, bb_pos, trend_extension) | RR from stop/take vs `current_price` (= execution price); other components closed-bar derived | clean | shift-by-one run |
| `_resolve_risk_multiplier`, `_enrich_signal`, time-exit `max_hold_bars` | functions of confidence / regime / config only | clean | n/a |
| internal state (`_bar_index`, `_last_signal_bar`, spacing/cooldown, diagnostics buffers) | counters/history only, never future | clean | n/a |

---

## Boundary cases (need a decision before Fase 3+, not blocking Fase 2)

These are **not** backtest look-ahead** — the strategy never sees a future bar — but
they are stylistic choices about how the *live/last price* is used relative to the
last closed bar. The main session should confirm they are intended.

### Boundary case A — entries decided with `current_price` and executed at `current_price`

`_trend_long_signal`, `_trend_short_signal` and `_range_signal` use `current_price`
(in the backtest: the close of bar `t`) to evaluate breakout/pullback proximity,
band position and to compute the stop/take, and the order then executes at that same
`current_price`. Live, `current_price` is the latest ticker — genuinely available
when the decision is made, and the order does fill near it. So this is the standard
"act on the latest price using closed-bar indicators" pattern, **not** look-ahead.
The only subtlety is that in the backtest `current_price` equals the *close* of bar
`t` exactly (no intrabar fill modelling), which is mildly optimistic on fill timing —
a fill-modelling refinement, orthogonal to look-ahead. Recommendation: leave as is;
note it when Fase 1's slippage/fill model is revisited.

### Boundary case B — `_range_signal` "reversal bar" compares `current_price` to `candles[-2][4]`

In `_range_signal`:

```python
prev_close = float(candles[-2][4]) if len(candles) >= 2 else current_price
...
'reversal_bar': ... (bb_pos <= entry_band and current_price > prev_close) ...
```

Since `candles[-1]` is the last *closed* bar `t-1`, `candles[-2]` is bar `t-2` — so
this compares the live price (bar `t` close) against the close *two* bars back,
skipping bar `t-1`. It is not look-ahead (no future data), but `candles[-1][4]`
(close of `t-1`) would arguably be the more natural reference for "is the latest
price above the previous close". This is a pre-existing quirk; I left it unchanged
(out of scope to retune entry logic) and flag it for the main session.

---

## Methodology / evidence detail

### 1. Code review + inline annotations

Every indicator helper and feature site in `scripts/advanced_strategy.py` now carries
a one-line comment stating which bars enter the computation (e.g.
`# Wilder RSI over the full supplied closed-bar series [.. : t-1]; no future data.`),
plus a `LOOK-AHEAD CONTRACT` block at the top of `analyze()` and a block comment
above the `indicators` section. These make the discipline self-documenting and give
reviewers a fast cross-check.

### 2. Per-indicator prefix-stability tests

`tests/test_lookahead_discipline.py :: IndicatorPrefixStabilityTests` — for each
indicator (`calculate_rsi`, `calculate_macd`, `calculate_bollinger_bands`,
`calculate_atr`, `calculate_volume_signal`, `_efficiency_ratio`, `_slope_pct`,
`detect_market_regime`, and `analyze` itself) it asserts that the value computed for
a prefix of the series is identical regardless of what (future) bars are appended —
i.e. the functions are causal and pure on their argument. All pass.

### 3. Shift-by-one backtest

`tests/test_lookahead_discipline.py :: ShiftByOneBacktestTests` runs the existing
backtester twice over a fixed 5 000-bar slice of `backtest/data/BTC-USDT_5m.csv`
(rows 20 000–25 000): once with the normal strategy, once with a `_LaggedStrategy`
wrapper that drops the most recent closed bar before calling `analyze()` (forcing the
strategy to act one bar later on staler data). Result:

| run | trades | final equity | return |
|---|---|---|---|
| unshifted | 10 | 9 725.33 | −2.75% |
| lagged by one bar | 10 | 9 789.85 | −2.10% |

Same trade count; the lagged run is marginally *less bad* (well within one bar of 5m
noise) — definitely **not** the large *improvement* you would see if the unshifted run
were exploiting the to-be-removed bar. Conclusion: **no material look-ahead.** The test
asserts the lagged return does not improve beyond a generous noise band and that the
trade count doesn't collapse.

### 4. Fixes applied

None required — nothing was found that needed a correctness fix. Only comments and
tests/docs were added.

---

## Files changed / added in Fase 2

- `scripts/advanced_strategy.py` — added bar-handling comments at every indicator /
  feature site and a `LOOK-AHEAD CONTRACT` block on `analyze()` (no logic change).
- `tests/test_lookahead_discipline.py` — new: prefix-stability tests per indicator +
  shift-by-one backtest sanity test.
- `docs/lookahead-audit.md` — this file.

`pytest` (from project root): **130 passed, 2 skipped** (the 2 skips are pre-existing,
network-/model-gated). The `tests/test_lookahead_discipline.py` suite is 10 tests, all
passing.
