# Axis D — Regime Fit

185 trades, all `side=buy`, all strategy-label `range`. Entry times joined (merge_asof, backward, ≤3h) to `diag_bars.regime` (objective trailing-30d BTC regime: >+5%=bull, <−5%=bear, else sideways — lookahead-free). 0 nulls.

## 1. Strategy-label × objective bar-regime

Strategy labels **100%** of its entries `range`. The objective regime at those entries:

| obj regime at entry | n | share | win rate | mean pnl_pct | total PnL (USD) |
|---|---|---|---|---|---|
| sideways | 90 | 48.6% | 6.7% | −0.246% | −19.86 |
| bull | 60 | 32.4% | 16.7% | −0.197% | −11.29 |
| bear | 35 | 18.9% | 20.0% | −0.237% | −6.69 |
| **all** | 185 | | 12.4% | −0.228% | **−37.84** |

**51.4% of its "range" entries actually occur during objectively trending (bull/bear) stretches.** That roughly matches the unconditional bar mix (44% sideways / 28% bull / 28% bear), i.e. the classifier's "range" label carries essentially **no information** about the objective regime — it fires the same fade-long setup regardless.

But note the twist: it loses in *every* cell. It actually loses **worst in objective sideways** (6.7% win rate, −0.246%/trade) — the exact regime its mean-reversion thesis is supposed to own. Trending cells are slightly less bad (the few wins are dip-buys that get carried by the uptrend). So "fade setups getting run over by trends" is only half the story; the bigger problem is the fade edge isn't there even in real ranges.

## 2. Are these genuine mean-reversion setups?

Yes — they're real band/RSI extremes, not mid-band noise:
- `ind_in_midzone` = 0 for **all 185** trades.
- `ind_bb_pos`: mean −0.002, median +0.04, **max 0.12, min −0.69** (this field is signed distance from the mid-band normalized; ≤~0.12 ⇒ at or below the lower band). Every entry is a buy at/below the lower Bollinger band.
- `ind_rsi_value`: range 31–59, mean 44, 75th pct 48 — modestly oversold-to-neutral.

Does buying lower bb_pos / lower RSI work? **No.**

| bb_pos bucket | n | win rate | mean pnl_pct |
|---|---|---|---|
| ≤0.1 (deepest below band) | 85 | 15.3% | −0.219% |
| 0.1–0.3 | 38 | 2.6% | −0.249% |

| RSI bucket | n | win rate | mean pnl_pct |
|---|---|---|---|
| 30–40 | 38 | 18.4% | −0.219% |
| 40–45 | 63 | 14.3% | −0.197% |
| 45–50 | 55 | 9.1% | −0.249% |
| 50–55 | 24 | 4.2% | −0.267% |

Lower RSI is marginally less terrible (corr RSI↔pnl_pct = −0.13, the only non-zero signal in the whole set), but even the "best" oversold bucket is a deeply negative-expectancy −0.22%/trade. Deeper bb_pos doesn't help at all (corr ≈ 0).

## 3. Are regime_confidence / efficiency_ratio informative?

No.
- `ind_regime_confidence` quintiles: win rate 16/16/5/11/14% — non-monotone; corr with pnl_pct = **−0.008**. The classifier's confidence is uninformative.
- `ind_efficiency_ratio` quintiles (low = choppy/rangey, design-intended sweet spot): win rate 8/11/14/22/8% — the *highest* (most trending) bucket is no better than the lowest; corr = **+0.026**. The strategy does **not** do better in genuinely low-efficiency conditions as designed.
- Score mix: `chop` score dominates (mean 0.44) yet the bot still trades — and `range`/`chop`/`bull`/`bear` score composition shows no relationship to outcome.

## 4. Classifier bug vs absent edge?

**Absent edge — strategy-thesis problem, not a labelling bug.** The classifier *is* loosely mislabelling (its "range" tag matches the objective regime no better than chance), but fixing that wouldn't help: even when you condition on a *correct* range (objective sideways) the trades are the worst-performing cell (−0.246%/trade, 6.7% win), and even when you condition on a textbook mean-reversion entry (below the lower BB, RSI 30–40) the expectancy is still ~−0.22%/trade. The fade-the-5m-dip-on-BTC edge does not survive costs, full stop. ~37 USD of losses spread evenly across regimes, band-depths, RSI levels and confidence levels with near-zero predictive correlation everywhere.

Plot: `backtest/results/diag_d_regimefit.png`. Merged dataset: `backtest/results/diag_d_merged.csv`.

## Implications for go/no-go

- **(1) Fix exit — won't save it.** Mean pnl_pct is −0.22% to −0.27% across *every* slice; the entry has no edge to harvest, so a better exit just trims a hopeless trade.
- **(2) Redesign entry signal — must abandon the mean-reversion premise, not tweak it.** The current entry is already a clean band/RSI extreme (no midzone noise) and it loses; making it "more oversold" doesn't flip the sign. A redesigned entry would have to be a *different thesis* (e.g. trend-following / breakout), which is effectively (3).
- **(3) Abandon the strategy family — supported.** "Range/fade on 5m BTC" is not a real edge after costs; the regime classifier is a red herring. Recommend NO-GO on patching, and either pivot the entry to a momentum/breakout thesis or retire the `advanced` family on this market/timeframe.
