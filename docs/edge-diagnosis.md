# Edge diagnosis — why the 'advanced' strategy underperforms BTC buy-and-hold

> 8-axis post-mortem of `MultiIndicatorConfluence` (`scripts/advanced_strategy.py`) on BTC-USDT 5m.
> Inputs: the Fase-1 baseline run (185 trades, ~370 days, $115 start) exported to
> `backtest/results/diag_trades.csv` / `diag_bars.csv` / `diag_summary.json`.
> Per-axis detail: `docs/edge-diagnosis/{A..H}-*.md`. Plots: `backtest/results/diag_{a..h}_*.png`.

## TL;DR

The strategy returns **−32.9%** over the period while a passive BTC buy-and-hold returns **−9.5%** — an alpha of **−23.4%**, in a market that *fell*. Every one of the 8 axes converges on the same explanation:

- **Gross PnL, before fees/slippage, is ≈ −$1.2 on $115 — break-even.** The −$37.84 net loss is **~97% transaction-cost drag** ($19.96 fees + $16.63 slippage over 185 round-trips of ~$90 notional at ~0.22% round-trip).
- **The entry signal has no edge — not even gross.** Mean per-trade gross move ≈ −0.008% (≈ 0). Median gross move 0.13%, vs the 0.22% round-trip cost floor → **82% of trades are "dead on arrival"** (can't clear costs even if the price call is right). MFE caps at ~1% *ever*; only 40/185 trades saw a favorable excursion above 0.22%, only 5 above 0.5%.
- **Direction is a coin flip, slightly worse.** BTC-up hit rate after a buy: 51–55% vs a 50% null (not significant; bootstrap percentile ~23rd at h=5). Mean MFE 0.14% < mean |MAE| 0.20% → entries carry *negative* directional information. Adverse selection: BTC averages −2.16 bps/bar while the bot is long vs +0.015 bps while flat.
- **The "confluence" scores are uninformative.** confidence / quality_* / regime_confidence / htf_alignment all have |IC| ≈ 0 vs realized PnL and vs forward returns — which is exactly why Fase 5's risk-score did nothing. The only non-zero ICs (RSI, MACD-hist vs 15-bar forward return ≈ −0.27) have the **wrong sign**: the strategy buys when they're elevated, and elevated predicts *lower* forward returns.
- **The exit geometry is unwinnable by construction.** R:R = 0.226 (avg win +0.061% vs avg loss −0.269%) → needs an **81.6% win rate** to break even; actual is 12.4%. The take-profit target (≈ 0.06–0.08%) sits at ~1/3 of the friction floor, so even *winning* trades net negative (TP bucket: gross +$4.74, net −$2.60). Stops sit inside 5m noise (median 2-bar hold, MAE −0.27%).
- **It loses everywhere, all the time.** Negative in 13/13 months, in all three objective regimes (bull/bear/sideways) — **worst in sideways**, the regime its own thesis is supposed to own. Equity decline is linear (R² 0.985, ≈ −$0.20/trade). It **never beat buy-and-hold in any rolling 30d or 90d window**. Weekly corr(#trades, PnL) = −0.84: it loses in near-mechanical proportion to how much it trades.
- **It's flat 99.35% of the time but ~95% of equity when in a trade.** Allocation effect ≈ 0 (at its average 0.62% exposure it's just a cash fund — a tiny positive vs the falling benchmark). The entire −33% is the timing/churn ("selection") effect. A flat-ish bot can underperform a falling benchmark only by churning a no-edge signal at full size — which is precisely what this is.

## Go / no-go on the three fix directions

### (1) Fix the exit structure — **NO-GO**
The exit machinery *is* mis-scaled (TP inside the friction floor, SL inside 5m noise, stale-exit round-trips ~0.20%) and the stop-loss bucket alone accounts for −$29 of the −$38. But ablations show fixing it recovers almost nothing: TP-wide → −32.1%, SL×2/×3/×10 → −32.6%/−32.2%/−30.6%, time-exits off → −31.9%, and the **theoretically best possible exit — "enter on the signal, never exit until end of data" — returns just −0.27%, i.e. break-even**, because the entries themselves produce no harvestable move (MFE caps at ~1%). An exit fix stops the bleed; it cannot create profit. Not worth the effort on its own.

### (2) Redesign the entry signal — **only as a ground-up rebuild, and even then speculative**
A genuine fix would have to: move to an hours-to-days horizon so the ~0.22% round-trip amortizes; cut turnover by ~10×; enable shorts (BTC fell over the period and the bot is long-only); discard the confluence scores (provably uninformative); and probably flip the RSI/MACD polarity. That is a new strategy, not a patch — and the effect sizes the data even hints at (~0.1% over 15 bars) are themselves *below* the 5m cost floor, so the rebuild only becomes viable on a higher timeframe. Any continuation must gate on: **forward-return separation from a random-entry null demonstrated out-of-sample, before any backtest optimization** — the bar Plan D and Plan E both used.

### (3) Abandon the 'advanced' / MultiIndicatorConfluence family — **GO (recommended)**
All 8 axes independently vote this. Together with Plan D (single-asset mean-reversion) also failing and the v2.7 improvement plan's Fases 3–6 all coming back NO-GO, the picture is unambiguous: **5m high-turnover indicator-confluence on BTC is structurally a friction harvester with no latent edge** — there is no hidden alpha masked by bad exits, one bad regime, or one bad stretch; the gross signal is flat-to-negative in every cut. Recommendation: **retire `MultiIndicatorConfluence` as a live candidate.** It stays in the repo for reference; the inert infra from Fases 4–6 (regime_multipliers / risk_scoring / bear_check, all default-off) can be left as-is.

Note: this concerns only the *single-asset "advanced" strategy*. The live production system runs **Plan E** (cross-sectional multi-asset, the `plan-e@*` services) and is untouched by this diagnosis.

## What this diagnosis delivered that's reusable

- `backtest/diag_export.py` — re-runs the baseline and dumps the trades/bars CSVs the analysis used.
- Per-axis throwaway analysis scripts `backtest/diag_{a,b,c,d,e,g,h}_*.py` and the per-axis write-ups in `docs/edge-diagnosis/`.
- A template for "is there an edge?" triage: gross-vs-net per-trade expectancy, MFE/MAE capture ratios, IC of every signal component, random-entry bootstrap null, exposure/Brinson decomposition. Reuse this on any future strategy *before* tuning it.
