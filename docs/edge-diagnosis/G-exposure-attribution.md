# Axis G — Exposure Attribution (Brinson-style decomposition)

Backtest: BTC-USDT 5m, 2025-04-12 → 2026-04-17 (369 days, 106 360 bars, 185 trades, all long).
Strategy ROI −32.9% · BTC B&H −9.46% · alpha −23.4%.

## 1. Net BTC exposure

| metric | value |
|---|---|
| Avg net exposure (fraction of equity, all bars) | **0.62%** |
| % of bars with a position open | **0.65%** (693 / 106 360 bars) |
| Exposure *when in a position* — mean / median | **94.7% / 94.3%** of equity |
| Exposure when in position — min / max | 87.9% / 99.7% |
| Avg position notional | $0.585 (≈ $90 on the ~$93 avg equity *while a trade is live*) |
| Avg equity over period | $93.34 |
| Bars held per trade — mean / median | 3.7 / 3.0 (≈ 15–20 min) |

So the strategy is **flat 99.35% of the time**, and when it does trade it goes **nearly all-in** (the ATR stop is tight relative to 5% risk, so the risk-multiplier sizes the position to almost the full account). Time-in-market is microscopic; per-trade exposure is huge.

## 2. Allocation vs Selection/Timing decomposition

BTC price/benchmark return over the period = −9.46%.

| component | return |
|---|---|
| **R_alloc** — hold the strategy's *avg* exposure (0.62%) in BTC, rest cash | **+0.00%** (B&H-partial: −0.06%) |
| **R_alloc (realized)** — hold the strategy's *actual per-bar* exposure passively (i.e. same trades, but never exit early, ride each window to the bar close) | **−13.2%** |
| Strategy actual return | **−32.9%** |
| **Selection/timing effect** vs avg-exposure portfolio | **−32.9%** |
| Selection/timing effect vs realized-exposure passive | **−19.7%** |

Reading: at the *average* exposure level the strategy is essentially a cash fund — being out of a falling market is a tiny positive vs full B&H, exactly as predicted. So **0% of the −23.4% underperformance is an "exposure level" problem.** The full −32.9% is created by *which bars it chooses to be 95%-exposed in* and *how those trades resolve*. Even granting it the bars it actually picked, a passive ride through those windows would lose only −13.2%; the remaining −19.7% is destroyed by transaction costs + intrabar stop fills (see §below — this is the real story).

### The cost autopsy (the actual mechanism)

| | $ | % of $115 |
|---|---|---|
| Gross PnL **before** fees/slippage | **−$1.23** | −1.1% |
| Transaction-cost drag (185 round trips × ~$90 notional × 0.22% round-trip) | **−$36.6** | **−31.8%** |
| Net PnL (matches summary) | −$37.8 | −32.9% |

Turnover = 178× equity over the year. The underlying signal is a **coin flip (≈break-even gross)**; 185 in-and-out round-trips at 6 bps×2 fee + 5 bps×2 slippage on a near-fully-sized position bleed it to death. This is why a flat-ish bot *underperforms* a falling B&H: it's not exposure, it's churn.

## 3. Timing quality cross-check

- Equity overlay → `backtest/results/diag_g_equity.png`: the "hold 0.62% BTC, rest cash" line is flat at ~$115 (basically the cash benchmark); the "realized per-bar exposure passive" line drifts to ~$100; the strategy line collapses to ~$77. The gap between the realized-exposure line and the strategy line ($23) ≈ the cost/stop damage.
- Exposure histogram → `diag_g_expo_hist.png`; exposure-vs-price timeline → `diag_g_timing.png`.

## 4. Does it pick the worse bars to be exposed?

| | mean BTC 5m return | n bars |
|---|---|---|
| Bars **in** market | **−2.16 bps** | 693 |
| Bars **out** of market | +0.015 bps | 105 667 |
| Dollar-weighted (exposure-weighted forward) | +0.06 bps | — |

Yes — BTC's average move *while the bot is long* is mildly negative (−2 bps/bar vs ~0 baseline). It's not catastrophic adverse selection, but combined with stop-losses (86 of 185 exits, avg −0.38%/trade) it means the entry signal has slightly *negative* edge before costs. Take-profit exits average −0.04% pnl_pct (TP is reached but fees eat the gain). Entry timing is, at best, noise; at worst, slightly anti-predictive.

## Implications for go/no-go on the three fixes

- **(1) Fix the exit — necessary but nowhere near sufficient.** Stops lock the −0.38% losers, but even a perfect exit (the realized-exposure passive curve) still loses −13% and the gross signal is break-even. Tightening/loosening exits won't manufacture an edge.
- **(2) Redesign the entry signal — required, but only meaningful alongside drastically lower turnover.** The current signal is a coin flip *before* costs and faintly adverse *after*. Any new signal must clear ~0.22% round-trip cost per trade; at 5m/15-min holds that's a ~4–5 bps/bar hurdle the indicators get nowhere near. Either find a genuinely predictive signal **and** hold positions far longer (hours–days, not 15 min) so the fixed cost amortizes, or it's hopeless.
- **(3) Abandon the strategy family — strongly indicated for this configuration.** "MultiIndicatorConfluence on 5m with ~4-bar holds and full-account sizing" is structurally a fee-incinerator: ~0.5 trades/day, 178× annual turnover, gross-flat signal. Unless the entry redesign in (2) also kills the high-frequency churn (i.e. it stops being the same strategy), there is no path to beating B&H here. Recommend: do **not** invest further in exit tuning; gate any continuation on a from-scratch lower-frequency signal, otherwise retire the family.

**Bottom line:** the underperformance is 100% timing/churn-driven, ~0% exposure-level-driven. Specifically: gross signal ≈ break-even, ~$37 of transaction costs on a ~$115 account = the entire loss. Exposure attribution exonerates "amount of market exposure" and convicts "trades too often, on a non-edge signal, at full size."
