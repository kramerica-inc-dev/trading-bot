# Axis F — Time Evolution of the Edge

Period: 2025-04-12 → 2026-04-17 (106,360 contiguous 5m bars ≈ 369 days). Both curves start $115.
Final: strategy **$77.16** (ROI −32.9%), BTC buy&hold **$104.12** (−9.5%). Alpha −23.4 pts.
Plots: `backtest/results/diag_f_equity_ratio.png`, `diag_f_rolling_alpha.png`, `diag_f_monthly_pnl.png`, `diag_f_regime_pnl.png`, `diag_f_trade_freq.png`. Numbers: `diag_f_summary.json`, `diag_f_monthly.csv`.

## 1. Equity vs benchmark & ratio (diag_f_equity_ratio.png)
- Strategy/BH ratio is **>1.0 in only 1,052 of 106,360 bars (1.0%)**, all in the first ~2 days. Peak ratio 1.028 on 2025-04-13. Last time the ratio touched ≥1.0 was 2026-02-06 — and only because BTC was crashing, not because the strategy gained. End ratio 0.74.
- The decline is one long leg: **worst (and essentially only) drawdown −33.1%, from 2025-04-14 to 2026-04-15** — i.e. peak ≈ day 2, trough ≈ last day. There is no recovery leg.

## 2. Rolling alpha vs BH (diag_f_rolling_alpha.png)
| window | windows | % alpha < 0 | max alpha | strategy abs-return > 0? |
|---|---|---|---|---|
| 7d  | — | 58.8% | +25.1% | 3.0% of windows (max +0.13%) |
| 30d | 341 | 63.6% | +29.0% | **0.0% (max −0.86%)** |
| 90d | 281 | 41.3% | +31.8% | **0.0% (max −6.59%)** |

Reading: the "positive alpha" windows are an artifact — in months like Nov-2025 (BTC −17.5%) the strategy fell only −1.9%, so relative alpha looks great, but the **strategy's own return is negative in 100% of all 30d and 90d windows**. It never made money over any month-or-longer horizon; it just bleeds slower than BTC falls during crashes. So: no, it never genuinely beat BH on its own merits.

## 3. Monthly PnL (diag_f_monthly.csv, diag_f_monthly_pnl.png)
| month | strat $ | strat % | BTC % | dominant regime | trades |
|---|---|---|---|---|---|
|2025-04|−2.54|−2.2|+10.2|sideways|9|
|2025-05|−3.42|−3.0|+11.1|bull|18|
|2025-06|−5.39|−4.9|+2.5|sideways|18|
|2025-07|−3.84|−3.7|+8.0|bull|20|
|2025-08|−3.74|−3.8|−6.5|sideways|18|
|2025-09|−2.95|−3.1|+5.4|sideways|15|
|2025-10|−3.93|−4.2|−3.9|sideways|21|
|2025-11|−1.72|−1.9|−17.5|bear|11|
|2025-12|−2.01|−2.3|−3.0|bear|14|
|2026-01|−3.00|−3.5|−10.1|sideways|17|
|2026-02|−2.09|−2.5|−14.9|bear|8|
|2026-03|−2.53|−3.2|+1.9|sideways|11|
|2026-04|−0.68|−0.9|+13.3|sideways|5|

**13 of 13 months negative.** No catastrophic month — losses are a steady −2 to −5 $/month bleed (worst single month −$5.4 in Jun-2025). It is a "thousand cuts" pattern, not a blow-up. Notably it loses ~−3%/mo even in months BTC rallies +8–13% (Apr, May, Jul, Apr-2026) — pure dead-weight.

## 4. Loss vs bar-regime (diag_f_regime_pnl.png)
| bar regime | bars | strat total $ | strat per-bar Sharpe | BTC PnL in same bars |
|---|---|---|---|---|
| bull | 29,687 | −11.28 | −0.034 | +59.05 |
| bear | 29,367 | −6.69 | −0.027 | −93.26 |
| sideways | 47,305 | −19.88 | −0.043 | +23.32 |

It loses in **all three regimes**, and the *worst* per-bar Sharpe and the largest dollar loss are in **sideways** — exactly where a range/mean-reversion strategy should be at least neutral. It is not "a range strategy getting run over by trends"; it's negative-edge everywhere, slightly worse in chop. (Bear loss is "smallest" only because it's long-only and sits flat-ish; it still loses.)

## 5. Bleed vs trade frequency (diag_f_trade_freq.png)
- Weekly **corr(# trades, weekly equity change) = −0.84** (n=52 trading weeks). More trading ⇒ more loss, almost mechanically. Non-trading weeks: 0 PnL. Trading weeks: mean −$0.73.
- Linear fit of equity vs time: **R² = 0.985** → the bleed is essentially *linear*, a constant negative drift per trade (~−$0.20/trade × 185 trades ≈ −$38). Not episodic: the 5 worst weeks sum to only −$9.7 of the −$37.8 total (26%); the rest is uniform drip.

## Implications for go/no-go

- **Fix (1) exit only — NO.** The loss is structural and present in every regime, every month, and scales linearly with trade count. A better exit can't turn a −0.84-correlated, negative-every-month system positive; at best it reduces the per-trade cost of a coin-flip.
- **Fix (2) redesign entry signal — only if paired with (3).** Sideways is the *worst* bucket, which means the current confluence entry is not a mean-reversion edge at all — it has negative expectancy even in the conditions it's nominally built for. Any redesign is effectively a new strategy, not a tweak.
- **Fix (3) abandon the strategy family — strongly favored.** Time-evolution evidence: never beat BH on absolute terms in any 30d/90d window, 13/13 losing months, linear bleed perfectly correlated with activity, negative in all regimes. There is no latent edge being masked by bad timing or one bad stretch — there is simply no edge. Recommend retiring `MultiIndicatorConfluence` and rebuilding from a tested hypothesis rather than patching exits.
