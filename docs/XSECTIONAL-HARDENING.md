# Cross-sectional momentum — hardening (M4.5), 2026-06-04

Before building any paper runner, the sweep lead was stress-tested for the two
open risks (short-leg funding + multiple-testing / out-of-sample).
Script: `scripts/harden_xsectional.py` → `backtest/results/sweep/xsectional_harden.json`.

## Results (lead config: lookback=120, rebal=5, m=3, 10 OKX perps, 1259 days)

### A. Short-leg funding — a manageable headwind
Calibrated on ~3 months of real OKX funding (all 10 assets) applied to the
strategy's actual momentum weights:
- **+4.0%/yr headwind** (1.1 bps/day). Momentum longs recent winners (often
  crowded → elevated funding) and shorts losers, so it pays net funding. Real,
  but small relative to the gross edge.

### B. Funding sensitivity — ~5× headroom
Flat funding-drag sweep on the full sample:
| drag (bps/day) | net return | null %ile |
|---|---|---|
| 0.0 | +242% | 100 |
| 1.1 (realistic) | +167% | 100 |
| 2.0 | +117% | 100 |
| 5.0 | +10% | 100 |
| 10.0 | −65% | 100 |
- **Breakeven ≈ 6 bps/day**; realistic drag is 1.1 bps/day → comfortable margin.
- The null percentile stays 100 even when absolute return goes negative, because
  the random baskets pay the same drag — the *relative* edge over random
  survives; only the *absolute* return needs the funding headroom.

### C. Out-of-sample (train 70% / holdout 30%) — the decisive test
| config | holdout null %ile | holdout net | holdout IC (p) |
|---|---|---|---|
| base lb=90/rebal=5 | **14.8 (FAILS)** | −39% | −0.011 (0.84) |
| **lead lb=120/rebal=5** | **97.4 (clears)** | +10.2% | **0.119 (p=0.047)** |
- The **lead clears the null AND has a significant IC on data the lb/rebal
  selection never saw** → the grid's IC-significance is **not pure
  multiple-testing luck**. The base (lb=90) does *not* survive OOS — so config
  choice matters and lb=120 is the one to carry forward.
- Note the honest magnitude: OOS net is **+10% over ~1.25y**, not the +242%
  full-sample figure (inflated by a strong early period). Expect a **modest**
  market-neutral return, not a moonshot.

### D. Walk-forward stability (4 sequential windows, lead)
| window | null %ile | IC |
|---|---|---|
| 1 | 100 | 0.101 |
| 2 | 97 | 0.048 |
| 3 | 83 | 0.066 |
| 4 | 92 | 0.208 |
- **IC is positive in all four windows** (sign-stable), but only 2/4 strictly
  clear the null (W3/W4 are 83rd/92nd — positive but inside the band). A
  real-but-variable-strength edge, stronger in some regimes than others.

## Verdict: PARTIAL — paper with caution + tight kill-criteria
The lead is a **real, modest, OOS-surviving, funding-viable market-neutral
edge** — neither a confirmed fake nor a slam-dunk. It earns a paper instance
under the lighter regime, with:
- config **lb=120, rebal=5, m=3** (the OOS survivor, not lb=90);
- explicit modelling of perp funding on both legs (≈4%/yr headwind today);
- kill-criteria tied to the OOS expectation (~single-digit to low-double-digit
  %/yr market-neutral), not the full-sample number;
- needs perp access (acctLv≥2) → gated on the M0 probe / user keys for live;
  paper can run in DRY_RUN on public prices without keys.

## Data-integrity note (bug found + fixed during M4.5)
`backtest/okx_backfill.py` re-fetched candles on every run, so a short
`--days 100` funding refresh **overwrote the long candle history**. Fixed with a
`--no-candles` flag (refresh funding without clobbering candles); full daily
history was restored. Always refresh funding with `--no-candles`.
