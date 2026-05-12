# Strategy v1 — trend-rule bake-off & §7 validation (milestone M3)

> Status: **COMPLETE — verdict: ship B2.** No v1 trend rule (a/b/c) clears the
> random-entry null on Calmar out-of-sample on the available daily history.
> Per `DECISIONS.md` (2026-05-12) these rules are **not to be tuned** — the §7.1
> gate is a hard kill. The §7.2 grid + walk-forward and §7.3 holdout steps were
> therefore not run (they are "for survivors only"); §7.5-style robustness checks
> on the closest candidate are kept below for the record.
>
> Reproduce: `python -m backtest.run_v1_bakeoff` (bake-off + null gate);
> `python -m backtest.run_v1_bakeoff --full` (would also run §7.2/§7.3 if a rule
> survived). Plots: `backtest/results/v1_bakeoff_equity.png`, `v1_null_gate.png`,
> `backtest/results/random_entry_null_*.png`.

## 0. Data — and the caveat, front and centre

We **did** extend the daily history. `backtest/build_daily_csv.py` now defaults
to `--source venue`, which pulls native 1D candles straight from BloFin's public
market endpoint (the same paginated endpoint `data_collector.py` uses). BloFin
caps BTC-USDT 1D at **~3.3 years**: the CSV now holds **1216 fully-closed daily
bars, 2023-01-12 → 2026-05-11** (vs the previous ~370). The matching funding
series (`backtest/data/funding_btc_usdt.csv`, via `data_collector.fetch_funding_history`)
now covers 3648 8h settlements over the same span. That's a real improvement —
the window now contains the 2023 recovery, the 2024 bull leg to ~$73k, the 2025
run to BTC's Oct-2025 ATH (~$126k), and the 2025-Q4→2026 drawdown (to ~$77k):
multiple regimes, ~4 distinct legs. **It is still not long** by the standards of
a daily-bar trend study (a proper one wants 8–15 years across several
halving/macro cycles), and walk-forward splits on 1216 bars are small — so
treat the magnitudes below as indicative, not as the last word. BloFin simply
does not serve more; we did not fabricate or pull from any unvetted source.

Cost model (spec §5): blended fee 0.0280%/side (80% maker / 20% taker), 0.05%
slippage/fill, 15% relative no-trade band, real 8h funding ON. Book $5,000.
Vol-target: σ_target = 20% annualized, 30-day trailing realized vol, L_max = 1.0.

## 1. Benchmarks (full series, 2023-01 → 2026-05)

| | total ret | Calmar | max-DD | time-in-market |
|---|---:|---:|---:|---:|
| **B1** buy-and-hold | **+205.7%** | **+0.77** | 51.5% | 99.9% |
| **B2** trailing-stop-BH (10% / 20d, re-enters) | **+98.8%** | **+0.81** | 28.5% | 47.7% |
| **B2'** trailing-stop-BH (10%, one-shot → cash) | +14.7% | +0.49 | 8.5% | 2.3% |

Over this longer, drift-heavy window the two B2 variants diverge enormously: the
**re-entering** B2 is the right one to compare against — the one-shot rule exits
once in mid-2023 and never re-enters, sitting in cash through the entire
2023→2025 bull (it nails max-DD at 8.5% but at the cost of +14% vs B1's +205%).
The diagnosis's "+17.7% / +1.76 Calmar / 10% DD" headline was specific to the
~370-bar 2025-04→2026-04 window (a falling/sideways market) — that exact number
no longer reproduces on the full series, and `tests/test_daily_backtester.py`
checks it against the *sliced* diagnosis window. **B2 re-entering: Calmar +0.81,
max-DD 28.5% is the bar to beat.**

## 2. Bake-off table — three §3 rules × {vol-target, plain}, default params, full series

| candidate | total ret | Calmar | max-DD | TiM | mean hold | avg in-exposure | rebalances | fees |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **(a) trailing-stop 10%/20d — vol-tgt** | +61.6% | **+0.92** | 16.9% | 47.7% | 18.7d | 0.53 | 90 | $175 |
| (a) trailing-stop 10%/20d — plain | +98.8% | +0.81 | 28.5% | 47.7% | 18.7d | 1.00 | 61 | $389 |
| **(b) MA-filter SMA100 — vol-tgt** | +30.2% | +0.44 | 18.6% | 55.7% | 25.1d | 0.49 | 91 | $144 |
| (b) MA-filter SMA100 — plain | +71.3% | +0.44 | 39.5% | 55.7% | 25.1d | 1.00 | 53 | $287 |
| **(b) MA-filter SMA200 — vol-tgt** | +24.3% | +0.45 | 15.1% | 53.9% | 41.0d | 0.50 | 72 | $87 |
| (b) MA-filter SMA200 — plain | +54.0% | +0.42 | 33.3% | 53.9% | 41.0d | 1.00 | 32 | $185 |
| **(c) Donchian 50/20 — vol-tgt** | +32.9% | +0.32 | 27.5% | 40.4% | 32.7d | 0.50 | 56 | $100 |
| (c) Donchian 50/20 — plain | +85.1% | +0.52 | 39.3% | 40.4% | 32.7d | 1.00 | 29 | $194 |

**What the vol-target overlay does (spec §4 sanity-check — it works as designed):**
across all four rules the overlay roughly **halves max-DD** (e.g. trailing 28.5%→16.9%,
SMA100 39.5%→18.6%) and lifts Calmar for the trailing rule (0.81→0.92), but it
**costs a lot of return** because at σ_target = 20% on BTC's ~40–60% annualized
vol the exposure is capped near ~0.5 the whole time — so the vol-targeted leg
gives up most of the bull. Net Calmar lift over plain is small (≤ +0.11) and for
the MA rules it's a wash.

**What the trend filter does:** the best of the three (trailing, vol-tgt, Calmar
+0.92) is essentially flat vs B1's +0.77 / B2-re's +0.81 — no meaningful
risk-adjusted improvement. The MA and Donchian rules are *worse* than both
benchmarks. None of them is a candidate worth running before the null gate even
speaks; the null gate then closes it.

## 3. §7.1 — random-entry null gate (1000 reps, matched TiM + mean-hold + in-exposure)

For each rule we measured its realized time-in-market, mean in-market run length,
and average in-market exposure over the full series, built a ~1000-rep random
in/out null matched to all three (so the null gets the same vol-target sizing),
and placed the rule's full-series total-return AND Calmar in that null.

| candidate (vol-tgt) | total-ret percentile | **Calmar percentile** | null Calmar 5–95% band | verdict |
|---|---:|---:|---:|---|
| (a) trailing-stop 10%/20d | 78.7th | **84.0th** | [−0.07, +1.46] | **INSIDE band → DROP** |
| (b) MA-filter SMA100 | 39.3th | **49.2th** | [−0.02, +1.49] | **INSIDE band → DROP** |
| (b) MA-filter SMA200 | 30.8th | **48.5th** | [−0.01, +1.47] | **INSIDE band → DROP** |
| (c) Donchian 50/20 | 59.7th | **44.5th** | [−0.10, +1.46] | **INSIDE band → DROP** |

**Verdict per rule: NONE clears the null on Calmar OOS.** The best (trailing) is
at the 84th percentile of its matched null — inside the 5–95 band, indistinguishable
from "be in the market for ~half the days, ~19 days at a time, at ~0.5 exposure,
chosen at random." The MA and Donchian rules sit right around the median (45th–50th).
Per `DECISIONS.md` (2026-05-12) — *demonstrate forward-return separation from a
random-entry null OUT-OF-SAMPLE before any backtest optimization; if a rule's
edge is inside the null band, it's dead, don't tune it* — **the bake-off ends here.**

This is the legitimate "no v1 trend rule beats a random-in/out null on the
available data" outcome the task flagged as possible — and it now holds on a
proper 3.3-year, multi-regime window, not just on the short noisy one.

## 4. §7.2 / §7.3 — not run (survivors only)

No rule survived §7.1, so the coarse grid + walk-forward (§7.2) and the
single-holdout-vs-B1/B2 (§7.3) are moot. For the record, the grid we *would*
have searched on the trailing rule (X ∈ {0.10, 0.125, 0.15, 0.20} × N ∈ {20, 35, 50},
n = 12) has full-series Calmar spread **[+0.18, +0.92], mean +0.60, std 0.20** —
a wide spread driven almost entirely by max-DD luck (no parameter region is
robustly best), the same "params don't matter, don't pretend they do" lesson as
Fases 4/5. The §7.3 holdout would be the last ~22% of bars (≈ 2025-08 → 2026-05);
the winning config would have to land within ±1σ of its train Calmar AND beat
**both** B1 and the re-entering B2 on the holdout — there is no winner to test.

## 5. §7.4 — diagnosis template (closest candidate: trailing-stop 10%/20d, vol-tgt)

Run for the record on the rule that came closest to clearing — it confirms why
even that one isn't worth running.

- **Gross vs net.** Full-series fees $175 + funding **$672** on a $5k book over
  3.3y. Funding is the dominant cost here (perp longs pay): with funding OFF the
  same rule returns +77.2% / Calmar +1.20 / 15.6% DD; with funding ON, +61.6% /
  +0.92 / 16.9%. Fees ($175 over 90 rebalances of ~0.5-notional deltas) are
  second-order. Per-rebalance gross expectancy is small and the funding floor
  (~$7.5/rebalance-equivalent over a ~19-day hold at ~0.5 exposure) eats a
  meaningful chunk of it — not "dead on arrival" like the 5m strategy, but not a
  comfortable margin either.
- **Allocation vs timing (Brinson-style).** The rule's *only* lever is the
  allocation effect: it averages ~0.25 portfolio exposure (0.5 in-exposure × 47.7%
  TiM) vs B1's ~1.0, so over a +205% market it's mostly a cash fund that
  participates ~quarter-weight. There is no positive *timing* (selection) effect —
  the §7.1 result *is* the timing-effect measurement: it's null.
- **MFE/MAE-ish capture (daily).** The trailing exit caps in-trade drawdown well
  (per-segment max-DD ≤ ~12% in bull/bear) but the re-entry on a new 20-day high
  systematically buys back *after* the rebound, so it captures the downside
  protection without much of the upside — the classic trailing-stop whipsaw cost.
- **Conditional bull/bear/sideways** (regime bars: 520 bull / 295 bear / 401 sideways):

  | segment | trailing vol-tgt ret | trailing max-DD | B1 ret | B1 max-DD |
  |---|---:|---:|---:|---:|
  | bull | +175.5% | 9.8% | +1640.8% | 15.7% |
  | bear | −11.9% | 11.9% | −79.2% | 80.5% |
  | sideways | **−33.4%** | 34.6% | −15.7% | 29.5% |

  It does its job in **bear** (−12% vs B1's −79%, DD 12% vs 80%) but **loses
  badly in sideways** — the regime a trend filter is supposed to sit out — to a
  whipsaw of stop-out / re-enter-on-breakout / stop-out. Gross expectancy is
  *not* "clearly > 0 and > the cost floor" in the regime that matters.

## 6. §7.5 — robustness (closest candidate)

- **Does it blow up in the sustained bull leg?** On the 2023-Q4→2024-Q1 bull
  sub-window: trailing vol-tgt +46.8% (DD 8.2%) vs B1 +131.0% (DD 16.3%). Doesn't
  *blow up* (no whipsaw bleed there), but gives up ~⅔ of the move — the vol-target
  cap, not the trend filter, is the cause.
- **Does it protect in the 2025-Q4→2026 drawdown?** −5.1% (DD 16.3%) vs B1 −32.5%
  (DD 51.2%). Yes — but so does plain B2.
- **Cost-knob sensitivity (full series, trailing vol-tgt):** base +61.6%/Calmar +0.92;
  2× slippage +59.0%/+0.87; all-taker fees +59.9%/+0.89; no-trade band 5% +61.5%/+0.90;
  band 25% +61.8%/+0.91. The result is **insensitive to doubling slippage / fee
  model / band** — the cost knobs are not what's killing it. **Funding** is the
  one cost that matters (+0.92 → +1.20 Calmar if removed), and it's a real,
  unavoidable cost of holding perp longs. None of this rescues a null edge.

## 7. Recommendation

> **Ship B2 (trailing-stop-on-BH, re-entering, 10% trail / 20-day re-entry high).**
> No v1 trend rule (a/b/c), with or without the vol-target overlay, demonstrates
> forward-return separation from a random-entry null out-of-sample on the
> available ~3.3-year daily BTC history — the best of the three (trailing) sits
> at the 84th percentile of its matched null on Calmar, inside the 5–95 band.
> Per the binding process rule in `DECISIONS.md` (2026-05-12) these rules are
> **not to be tuned**; the bake-off ends at §7.1.
>
> Concretely, for M5 live wiring: **the strategy to deploy as the v1 paper
> instance is B2 itself** — `daily_strategies.TrailingStopBH(trail_pct=0.10,
> breakout_days=20, reenter=True)` — with the §5 maker-first execution, §6
> circuit-breaker, and funding accounting. There is no reason to run anything
> more complex than that one-line rule until either (i) a longer daily history
> becomes available (BloFin caps at ~3.3y; an alternate vetted venue with 8–15y
> of daily BTC would let us re-run §7.1 with real statistical power), or (ii) a
> genuinely new information source clears the §7.1 bar on its own.

### Open questions for the M4/M5 decision

1. **Is "ship B2" actually worth a live paper instance**, given B2's full-series
   Calmar (+0.81) barely edges B1's (+0.77) on this window and its max-DD is still
   28.5%? The diagnosis's flattering B2 numbers were a short-window artefact. If
   the goal is "measurably better risk-adjusted than BH", B2 on the long window
   is only *marginally* better — a decision is needed on whether that clears the
   bar for a P1 paper run, or whether v1 should be parked until a longer history
   or a new signal exists.
2. **Vol-target σ_target.** If B2 *is* deployed, should it get the vol-target
   overlay (halves DD to ~17% but cuts return) at a *higher* σ_target than 20% (say
   30–35%, closer to BTC's own vol) so it doesn't bleed the bull? That's a sizing
   decision, not a signal one — but it changes the live spec.
3. **Funding drag.** Funding is the single biggest cost in the daily backtest
   (~13% of book over 3.3y for the trailing rule). A spot BTC instance, or a
   funding-aware entry timing on the perp, would change the economics — worth a
   note before M5 commits to the perp.
