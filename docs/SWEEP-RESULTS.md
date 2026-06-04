# Broad-sweep results (M3 + M4), 2026-06-04

Wave-1 of the OKX strategy sweep. Lighter regime: random-entry null gate is
non-negotiable; reps=500 (1000 for confirmation); paper-only this round.
Harness: `scripts/sweep_feasibility.py`; driver: `scripts/run_sweep.py`.

## Verdicts (all run on fresh OKX data)

| Candidate | Verdict | Null %ile | Net | Note |
|---|---|---|---|---|
| **xsec_momentum_90d_top3** | KILL* | **99.8** | +149% | *clears null; killed only on IC p=0.10 |
| donchian_50_20 | KILL | 76 | +191% | inside null band — no edge |
| tsmom_90 | KILL | 69 | +174% | inside band |
| trend_sma200 | KILL | 56 | +112% | inside band |
| rsi2_meanrev | KILL | 35 | −22% | cost trap (drag 170% of gross) |
| vol_target_bh | KILL | 0.2 | +100% | **worse** than random entry — vol-targeting bleeds the bull |

**The four single-asset directional families are dead on OKX data too** (all
inside the random-entry null band). This closes the loophole that the prior
failures were a BloFin-data artifact — they are not. `vol_target_bh` (the
documented "fallback") sits at the 0.2nd percentile: it actively underperforms a
random-entry baseline of the same time-in-market.

## The lead: cross-sectional momentum (market-neutral)

Dollar-neutral long-short over 10 OKX perps (long top-m / short bottom-m by
trailing return, rebalanced every few days). It is the **only** candidate to
clear the lane-specific null (random dollar-neutral baskets with matched
turnover), and it does so robustly:

**M4 neighbourhood robustness** (`scripts/confirm_xsectional.py`, grid of
lookback × rebal × m, 30 configs):
- **16/30 configs clear the null** (chance rate ≈ 1.5) → the null edge is
  **structural, not a single-config spike**. Concentrated in the **short-
  rebalance region** (rebal=5), consistent with momentum decay.
- IC is **consistently positive and right-signed** across nearly all configs
  (cross-sectional Spearman 0.02–0.09), but only **significant (p<0.05) at
  lookback=120, rebal=5** (IC=0.069, p=0.018).
- **2 configs fully ADVANCE** (null + significant IC + sham-fails):
  `lookback=120, rebal=5, m∈{2,3}` → net **+243% / +311%** over 3.5y,
  **Sharpe ≈ 1.0**, null 99.8–100th pct.

### Honest caveats
1. **Multiple testing.** 30 configs scanned; ~1.5 IC-significant by chance.
   The single IC-significant region (lb=120/rebal=5) could be partly luck — the
   ADVANCE is a **lead to confirm on a holdout / forward paper**, not a proven
   edge. The *null* robustness (16/30) is the stronger signal; the IC
   significance is the fragile part.
2. **Needs perp access.** This is a PERP long-short → requires acctLv≥2 on OKX.
   The M0 access probe (`scripts/okx_access_probe.py`) resolves this once
   credentials are supplied. On OKX demo, paper validation is likely possible
   even if EU-live perp is capped — to be confirmed by the probe.
3. **No funding/borrow cost on the short leg yet.** The current model charges
   turnover cost but not perp funding on the short side. Funding on OKX is
   currently compressed (~+0.6%/yr, see OKX-DATA-NOTES.md) so it's second-order
   now, but a perp long-short must model funding on both legs before live.

### Recommendation
Promote the cross-sectional momentum lane to a **paper instance on OKX demo**
(M5), built around `lookback≈90–120, rebal=5`, with the IC-significance gap and
the unmodelled short-leg funding flagged as the open risks. This is the first
candidate since the project began that clears the non-negotiable null gate
robustly. Everything else this wave is a confirmed KILL.

### Not covered this wave (logged, not silently dropped)
- Carry / funding-timing (B1/B2/B3): OKX public funding ~3mo + BloFin not a
  valid OKX proxy → data-limited; collect OKX funding forward.
- Cross-venue basis (B6): needs Bitvavo/Kraken adapters + synchronized books.
- Variance-risk-premium (B7): needs an options client (none in repo).
- Maker-rebate MM (B10): only honest via live paper-fill measurement.

## Wave 1b — structural candidates (broadening)

### B3 cross-sectional funding carry — PRELIM-NOEDGE (data-limited)
`backtest/sweep/funding_carry.py`, OKX-native, ~3mo window (the OKX funding
limit). Long lowest-funding / short highest-funding perps to harvest the
funding dispersion.
- **Gross funding dispersion available: ~12.4%/yr** (max−min per-asset mean
  funding) — the premium exists.
- **But not cleanly harvestable** via a simple cross-sectional perp long-short:
  funding harvested +1.3–2.7% over 93d vs **price-leg noise −5 to −16%** — a
  funding-ranked basket is not beta-neutral, so residual price drift dominates.
  Net negative, Sharpe negative across configs.
- The funding *direction* is cross-sectionally valid (lb=3/rb=1 clears the 3mo
  null at 99.6th) but absolute PnL is swamped by price noise + turnover.
- **Conclusion:** the funding edge is real but small; the clean way to harvest
  it is per-asset delta-neutral cash-and-carry (spot+perp) — exactly the
  venue-blocked carry. Cross-sectional perp-carry needs a beta hedge to work.
  Forward-collect OKX funding for a proper (non-3mo) assessment.

### B7 variance-risk-premium — STRUCTURAL-PASS / PASS-TAIL-RISK (the strongest premium)
`backtest/deribit_dvol.py` (Deribit DVOL implied-vol index, 1899d 2021→2026) +
`backtest/sweep/vrp.py`. Sell 30d implied vol (DVOL) vs realized vol of BTC
daily, non-overlapping monthly, delta-hedged proxy. Over the OKX-overlap
(2022-11 → 2026-06, 43 months):

| cost (vol pts) | mean P&L/mo | t (p) | Sharpe | sub-periods + | verdict |
|---|---|---|---|---|---|
| 0 | +7.4 | 2.85 (0.007) | 1.51 | 3/3 | STRUCTURAL-PASS |
| **2 (realistic)** | **+5.4** | **2.08 (0.043)** | **1.10** | **3/3** | **PASS-TAIL-RISK** |
| 4 | +3.4 | 1.31 (0.20) | 0.69 | 2/3 | KILL |

- **Raw VRP = +6.75 vol points** (IV systematically exceeds realized vol) — a
  real, significant, sub-period-consistent **unconditional** premium (one of the
  most robust in finance). Beats a random-sign-vol null at the 100th percentile.
- **Tail is the catch:** worst month −48 vol points (~9× the mean monthly
  harvest), CVaR-5% ≈ −35. Short vol blows up in spikes — **needs an explicit
  tail-hedge (long wings) and/or small sizing** before live.
- **Cost-sensitive:** dies above ~3 vol points round-trip → execution
  (Deribit ATM spread + delta-hedge slippage) must stay tight.
- **Caveats:** options execution on Deribit (offshore, same regulatory question
  as other venues); the backtest is a vol-points model, not full option
  replication — the +6.75 raw VRP is model-free, but tradable P&L depends on
  delta-hedge frequency / option selection not fully modelled.
- Note: the timing-shuffle sham is category-inappropriate for an *unconditional*
  level premium (it would always retain the premium); the correct control here
  is sub-period consistency (3/3) + the random-sign null + the tail gate.

## Two real candidates after broadening
1. **Cross-sectional momentum** (perp long-short, OKX): modest, OOS-survives
   (M4.5), simple execution, market-neutral. Sharpe ~0.6–1.0, 2/4 walk-forward.
2. **VRP short-vol** (options, Deribit): stronger + significant + consistent
   (Sharpe ~1.1, p=0.04, 3/3 sub-periods), but tail-exposed, cost-tight, and
   harder to execute (options + delta-hedge + tail-hedge + offshore venue).

B6 (cross-venue basis) remains low-value on daily data (needs intraday
multi-venue feeds) and is not pursued. Funding-carry (B3) is venue-blocked for
clean harvest. So the live shortlist is **momentum and/or VRP**.
