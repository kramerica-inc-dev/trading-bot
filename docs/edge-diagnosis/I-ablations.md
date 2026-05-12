# Axis I — Consolidated Ablation / Counterfactual Table

Strategy `advanced` / MultiIndicatorConfluence, BTC-USDT 5m, 185 trades (all long, 0 shorts, ~all `range` regime), $115 book, ~0.22% round-trip friction (0.06% fee/side + 0.05% slippage/fill). Baseline run = `backtest/run_baseline.py`. This axis isolates *which component destroys value* by swapping pieces out, and pins the strategy against naive baselines and against random-trading-with-the-same-turnover-and-friction.

Throwaway script: `backtest/diag_i_ablations.py`. Plots: `backtest/results/diag_i_bh_stops.png`, `diag_i_random_band.png`, `diag_i_signflip.png`. The already-run ablations from axes A/B/C are cited verbatim (scripts `backtest/diag_b_ablations.py`, `backtest/diag_c_friction.py`, `backtest/diag_a_direction.py`).

## 1. The consolidated table

`alpha` = ROI − (−9.46% BH passive). `gross` = PnL before fees & slippage, on the same trades, in $ on a $115 book. "—" = not applicable / not computed.

| # | Variant | ROI % | Win rate | Calmar | Max DD % | Alpha vs BH | #trades | Gross PnL | Source |
|---|---|---|---|---|---|---|---|---|---|
| 0 | **Baseline (`advanced`, canonical)** | **−32.9** | 12.4% | −0.98 | 33.1 | **−23.4** | 185 | ≈ −$1.2 (mid) / −$18.3 (fill) | run_baseline / diag_summary.json |
| | *— exit-geometry ablations (axis B) —* | | | | | | | | |
| 1 | TP target removed (wide) | −32.1 | — | — | — | −22.6 | ~185 | — | diag_b |
| 2 | SL ×2 | −32.6 | — | — | — | −23.1 | ~185 | — | diag_b |
| 3 | SL ×3 | −32.2 | — | — | — | −22.7 | ~185 | — | diag_b |
| 4 | SL ×10 | −30.6 | — | — | — | −21.1 | ~185 | — | diag_b |
| 5 | Time-exits OFF | −31.9 | — | — | — | −22.4 | ~185 | — | diag_b |
| 6 | Only-time-exit (TP & SL off) | −0.58 | — | — | — | +8.9 | ~185 | — | diag_b |
| 7 | **Strategy entries + passive-hold-to-end (best-possible exit)** | **−0.27** | — | — | — | **+9.2** | ~185 | — | diag_b (re-confirm) |
| | *— friction ablations (axis C) —* | | | | | | | | |
| 8 | Zero cost (full backtest, no fee/slip) | −1.1 | 45.9% | — | — | +8.4 | 185 | — | diag_c |
| 8b | Zero cost (same 185 trades, mid prices) | −1.1 | 45.9% | — | — | +8.4 | 185 | **−$1.25** | diag_i |
| 9 | Half cost | −17.0 | — | — | — | −7.5 | 185 | — | diag_c |
| | *— direction baseline (axis A) —* | | | | | | | | |
| 10 | Random entry, same frequency (analytic estimate) | ≈ −41 (of summed notional) | ~50% | — | — | — | ~185 | — | diag_a |
| | *— NEW: buy-and-hold + a stop —* | | | | | | | | |
| 11 | **BH passive (no stop)** — the benchmark | **−9.5** | — | −0.18 | 52.4 | 0.0 | 0 | — | diag_i |
| 12 | BH + fixed −10% stop (then cash) | −10.1 | — | −0.26 | 39.0 | −0.6 | 1 | — | diag_i |
| 13 | BH + fixed −20% stop (then cash) | −20.1 | — | −0.44 | 45.8 | −10.6 | 1 | — | diag_i |
| 14 | **BH + trailing −10% stop (then cash)** | **+17.7** | — | **+1.76** | **10.1** | **+27.2** | 1 | — | diag_i |
| | *— NEW: random / sign-flip on the strategy's footprint —* | | | | | | | | |
| 15 | **Random 185 long entries, holding-time matched to strategy, same fee+slip, ×400** | mean **−31.6** (median −31.8; 5–95% **[−36.2, −27.0]**) | ~50% | — | — | −22.2 | 185 | — | diag_i |
| 16 | **Sign-flipped — go SHORT on the same 185 entries (est. from mid moves), same friction** | **−30.7** | 6.5% | — | — | −21.3 | 185 | **+$1.25** | diag_i |
| 16b | (reference) sign-flip *gross only*, zero cost — short side | +1.1 | — | — | — | — | 185 | +$1.25 | diag_i |

Notes on the new rows:
- **Baseline gross**: the strategy's gross PnL on mid-to-mid prices over the 185 trades is **−$1.25** on $115 (axis C). Friction (~$18) is what turns that into the −$37.8 / −32.9% net. The signal contributes a *gross* loss; it has no edge even before costs.
- **Random-entry test (row 15)** is the faithful version: 185 entries at uniformly-random bars, holding times drawn (with replacement) from the strategy's own `bars_held` distribution (median 3, mean 3.75 bars), the strategy's mean per-trade notional, the same 0.06% fee/side + 0.05% slip/fill, repeated 400×. A compound-equity proxy variant gave essentially the same answer (mean −33.6%, 5–95% [−37.5%, −29.8%]).
- **Sign-flip (row 16)**: short the same 185 entry→exit windows. Gross (zero-cost) the short side makes +$1.25 — i.e. exactly the mirror of the long side's −$1.25 — which is *within noise of zero*, not a real anti-signal you could harvest. After paying the same ~$18 friction the short version loses −30.7%, basically the same hole as the long version. (Consistent with axis E: RSI/MACD-hist IC ≈ −0.27 looks "wrong-signed" but at this turnover and friction the implied edge is far below the cost floor — flipping the sign just loses differently.)

## 2. Interpretation — ranking the value-destroyers

**1. The entry signal (largest by far).** Replace the entries with *nothing* (passive hold from t0, row 7/11) and the book goes from −32.9% to break-even (−0.27%) or −9.5% (the benchmark). Every trade the signal opens is, gross of costs, a coin-flip that on average drifts −0.0007% favorable — the strategy buys ~185 times and pays the spread ~185 times for no expected price move (axes B, C, E, H all converge on this; here it's the −$1.25 gross row).

**2. Transaction-cost drag (the amplifier, not the cause).** Halving costs only gets to −17% (row 9); zeroing them gets to −1.1% (rows 8/8b) — so ~97% of the *net* loss is friction, but friction on a zero-edge signal. Cost is the multiplier; the missing edge is the root.

**3. Exit geometry (essentially zero leverage).** Every TP/SL/time-exit tweak in axis B lands between −30.6% and −32.6% — a ≤2 pp swing. The *only* exit change that matters is removing the exit machinery entirely (rows 6, 7), which is just another way of saying "stop trading the signal." TP being set inside the friction floor (axis B) is real but it's a rounding error next to the no-edge entry.

**Which single change moves the needle most?** **None of the exit or cost tweaks.** Only *removing the entries entirely* — passive hold — reaches break-even. There is no parameter, no exit, no cost reduction that turns this signal into alpha.

**Is the strategy ≈ "random long entries paying the spread"?** **Yes, statistically.** The strategy's −32.9% sits at the **~32nd percentile of the random-entry-same-turnover-same-friction distribution** (mean −31.6%, 5–95% band **[−36.2%, −27.0%]**) — comfortably *inside* the band, marginally *worse* than the median random run, never *better*. It is indistinguishable from "open 185 random longs on 5m BTC and pay the round-trip cost each time." The **sign-flip** confirms it: inverting the signal doesn't make money (−30.7% net, +$1.25 gross — noise), so the signal isn't anti-predictive either, it's just noise. And the **BH+stop** rows are the clincher from the other direction: a *trivial* rule — hold BTC, exit on a trailing −10% stop, sit in cash — returns **+17.7% with a 10% DD and +1.76 Calmar** over the very same period (it rides the May-2025 top and is flat through the −52% drawdown), beating the strategy's −32.9%/−0.98 Calmar by ~50 pp of return and turning a 33% DD into 10%. The "advanced" strategy isn't just losing — it's losing to a one-line stop on buy-and-hold.

## Implications for go/no-go

Axis I closes the loop on every other axis: the strategy is **statistically a random long-only BTC trader paying the bid-ask spread ~185 times** — inside the 5th–95th-percentile band of that null, no better than its median, and worse than a one-rule trailing-stop-on-buy-and-hold by ~50 percentage points of return and a Calmar swing from −0.98 to +1.76. No exit retune (≤2 pp), no realistic cost reduction (best case −17%), and no signal-flip (still −30.7%) recovers an edge; the only thing that reaches break-even is *not trading the signal at all*. There is no salvageable component. **Verdict: (3) ABANDON the `advanced` / MultiIndicatorConfluence strategy family.** Any future work should start from a signal with a demonstrated gross edge *larger than the ~0.22% round-trip cost floor at the intended turnover* — this one's gross edge is −$1.25 on $115, i.e. negative — and should be benchmarked against the trailing-stop-on-buy-and-hold baseline, not just against passive BH.
