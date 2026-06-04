# VRP deepening — faithful option replication (B7), 2026-06-04

The first VRP pass (`docs/SWEEP-RESULTS.md`, `backtest/sweep/vrp.py`) used a
**variance-swap proxy**: monthly P&L = `(IV − RV − cost)` in vol points, verdict
**PASS-TAIL-RISK** (+5.4 vp/mo at 2-volpt cost). That proxy is the idealised
payoff of a *perfectly-hedged variance swap* and cannot answer the two questions
that decide whether short-vol is tradable, so this pass replaces it with a
faithful model: an actual Black-Scholes (r=0) **short ATM straddle, delta-hedged
daily at frozen entry IV, held to a 30d expiry settling at intrinsic**, with
explicit hedge-transaction and option-spread costs, plus an optional
**long-OTM-wings tail-hedge**. Engine: `backtest/sweep/vrp_replication.py`
(8 unit tests). Data: Deribit DVOL (30d ATM IV index, 1899d) × OKX BTC daily
close (1299d), overlap 2022-11-13 → 2026-06-03, 43 monthly cycles.

> **All headline numbers here were independently reproduced to the digit by a
> 5-agent adversarial validation workflow** (2026-06-04). It CONFIRMED Q1 and the
> Q2 conclusion, and it CAUGHT TWO things the first draft got wrong: a capital
> -tail number (−16% → −10.1%) and the *direction* of the dynamic-sizing rule
> (the original thesis was backwards). Both are corrected below.

Two questions:
1. Does the premium survive faithful replication (dollar-gamma weighting +
   discrete daily hedging + the real cost stack)?
2. Can the tail be hedged cheaply enough to keep the edge?

---

## Q1 — the premium SURVIVES faithful replication (confirmed)

Naked delta-hedged short straddle, vol-point P&L (comparable to the proxy's
+5.4):

| hedge cost | opt spread | mean vp/mo | t (p) | Sharpe | null %ile | sub-periods |
|---|---|---|---|---|---|---|
| 0 bps | 0 vp | +8.12 | 2.98 (0.005) | 1.58 | 99.7 | 3/3 |
| 6 bps | 1 vp | **+6.39** | **2.34 (0.024)** | **1.24** | **98.4** | **3/3** |
| 12 bps | 2 vp | +4.65 | 1.70 (0.097) | 0.90 | 94.6 | 3/3 |

At a realistic execution stack (6 bps per delta-hedge trade, 1 vol-point option
entry spread) the faithful naked straddle earns **+6.4 vp/mo, t=2.34, Sharpe
1.24, 98th-percentile vs the random-sign null, 3/3 sub-periods** — *better* than
the proxy's +5.4. **The dollar-gamma weighting did not erode the edge.** The
validation independently re-derived this and confirmed: no look-ahead (entry IV
uses `DVOL[t]` only; realised vol is reported, never fed into P&L); the MTM
telescoping is correct (IV==RV Monte-Carlo nets ~0, no sign error); and the
vol-point normalisation does **not** inflate — the *dollar* t-stat (3.03) is
actually higher than the vol-point t-stat (2.34).

**Roll-timing robustness** (the 43-obs sample is small; the non-overlapping
cycle start matters): across all 30 start offsets — mean vp/mo min **+4.39**,
median +6.33, max +10.22; **t>2 in 93% of offsets; mean>0 in 100%**. Not a
lucky-alignment artifact.

**Outlier leverage (honest caveat).** +6.39 leans partly on the Nov-2022
FTX-era cycle (+77.5 vp at entry IV 108%). Dropping it → **+4.69 vp, p=0.038**
(survives); dropping both extremes → +6.03 vp, t=3.39, p=0.0016; median +7.3 vp,
65% of months positive, Wilcoxon p=0.005. Real but wide-banded on 43 obs.

**Cost sensitivity** decomposes into real, controllable levers (not the proxy's
opaque single `cost_volpts`): ≈ −1.0 vp/mo per +1 vp option spread, ≈ −0.75 vp/mo
per +6 bps hedge cost. Significance (p<0.05) holds to ~6 bps + 1 vp; marginal
(p≈0.10) only at 12 bps + 2 vp. **Maker option execution + passive limit
delta-hedging materially protect the edge** — "dies above ~3 vol points" is an
*execution-quality* constraint, not a verdict.

*Null caveat:* the random-sign null only reshuffles signs on the fixed magnitude
distribution — it is close to a restatement of the one-sided t-test, not
independent evidence, and does not address autocorrelation or the
cost×wing×offset multiple-testing grid. A moving-block bootstrap is the proper
null before "98th percentile" is treated as load-bearing (open gap).

---

## Q2 — the tail is a multi-day BURST, not a single gap → vanilla wings fail

The tail is intact: naked worst month −50 vp, CVaR-5% −31 vp (~5× the mean
monthly harvest). What *kind* of tail it is decides whether wings can hedge it.

Decomposing the worst months (gap-share = worst-single-day variance ÷ total
monthly variance; a single clean crash ≈ 0.7–0.9, pure grind ≈ 0.03–0.10):

| cycle start | P&L vp | IV% | RV% | max-day% | gap-share |
|---|---|---|---|---|---|
| 2023-02-11 | **−50.1** | 47 | **67** | 9.2 | **0.23** |
| 2026-01-26 | −12.2 | 39 | 83 | 15.1 | 0.40 |
| 2024-11-02 | −9.9 | 60 | 60 | 9.9 | 0.30 |
| 2023-10-09 | −9.4 | 37 | 42 | 9.8 | 0.60 |

Mean gap-share 0.27 (all), 0.37 (worst-6) — far from a clean gap (≈0.9). The
blow-up month was a **multi-day high-vol burst** (RV 67 vs IV 47, no single jump
> 9%; its top-3 days carry ~61% of the month's variance), *not* a single crash.
**And the bursts were mostly to the UPSIDE** — the −50 vp month had a +9.2% up-day
and +9.8% net drift. Either way, static OTM wings (gap insurance) cost premium
every month and catch almost nothing of a multi-day, often-upside burst.

Tail-hedge grid (long *symmetric* strangle, realistic 6 bps / 1 vp costs):

| wing Δ | ratio | skew | mean vp/mo | p | null %ile | worst vp |
|---|---|---|---|---|---|---|
| naked | — | — | +6.39 | 0.024 | 98.4 | −50.1 |
| 0.10 | 0.5 | 0 | +5.22 | 0.046 | 97.5 | −48.7 |
| 0.15 | 1.0 | 0 | +2.89 | 0.205 | 88.4 | −45.9 |
| 0.15 | 1.0 | 5 | +0.53 | 0.821 | 58.7 | −48.9 |
| 0.25 | 1.0 | 5 | −3.26 | 0.074 | 2.9 | −44.0 |

No *symmetric* strangle caps the tail while keeping the premium. The validation
probed harder structures the grid missed: **put-only** wings (drop the useless
call wing, since the bursts skew upside) are *less bad* — put-only Δ0.25 r1.0 at
zero skew ≈ +33%/yr (Sharpe 1.52 vs naked 1.24 on the vol-point summary) — **but
still do not beat naked** once a realistic skew (OTM puts 5 vp richer) is applied
and across roll offsets (put-only with skew beats naked Sharpe in **0/30**
offsets). So the conclusion holds; the blanket "no wing config" is just narrowed
to "no wing config, symmetric or put-only, beats naked."

**Capital translation** (size so the CVaR-5% monthly loss = 10% of capital),
both legs on a **consistent dollar scale** (the first draft mistakenly compared
naked on a vol-point scale and hedged on a dollar scale):

| structure | n straddles | return/yr | worst month |
|---|---|---|---|
| **naked** (6 bps/1 vp) | 4.03 | **+40.6%** | **−10.1%** of capital |
| tail-hedged (Δ0.15, r1.0, skew5) | 4.36 | **+7.4%** | −10.6% of capital |

So static tail-hedging **cuts return ~5×** (40.6% → 7.4%) and, on a consistent
dollar scale, makes the realised worst month **marginally worse** (−10.6% vs
−10.1%), not better. On listed vanilla options, tail-hedging is a near-pure cost
— the "wings are a wash" conclusion is, if anything, *stronger* than the first
draft claimed.

---

## Reframed verdict

> The VRP premium is **real, faithful-replication-proof, and roll-robust** — the
> single most validated edge this project has found. Its binding constraint is
> **not** premium existence; it is **tail management**, and static OTM wings
> cannot do it without handing the premium back.

Three options, in order of how cheaply they can be checked:

1. **Naked-but-small (risk-budget sizing).** Run the straddle unhedged at a size
   where the tail is survivable: **~+40.6%/yr at a ~−10% worst-month budget**
   (CVaR-sized), or +60%/yr at a −15% budget — scale to taste. The worst month
   is a real, accepted drawdown, not a blow-up. Coherent and aggressive.
2. **A DVOL-richness regime FILTER — and note the original thesis was
   BACKWARDS.** The first draft proposed cutting size when vol *rises*; the
   validation shows that is wrong. The short-straddle edge lives almost entirely
   in **high-IV entries** (corr(entry IV, P&L) = **+0.64**; the 23 rich-IV
   cycles average **+12.1 vp** vs **−0.2 vp** for the 20 cheap-IV cycles), and
   the grind tail *starts from calm* (the −50 vp month entered at DVOL 47%, below
   median, and surprised upward). So the correct, economically-motivated rule is
   the inverse: **sell vol only when DVOL is rich (>~50%), stand aside when it is
   cheap.** In-sample this preserves the mean and roughly halves CVaR (Sharpe
   1.24 → ~1.5; +60%/yr → +76%/yr at a matched 15% budget — *but the OOS section
   below runs this properly and corrects it down hard: that %/yr is largely a
   whole-sample-CVaR sizing look-ahead*). **Discount this
   hard:** best-of-sweep multiple-testing p = 0.16 (not significant after
   penalising the threshold/direction search), the worst-month fix is
   **knife-edge** (worst stays −50 vp at threshold 47%, drops to −10 vp at 48%,
   with the cycle sitting at DVOL 47.1%), and **leave-one-out reverses the Sharpe
   ranking** (naked 1.70 > filter 1.56 once the −50 cycle is dropped). Adopt only
   as a *coarse/binary* filter and **walk-forward / roll-offset re-validate it
   out-of-sample before any capital.**
3. **Shorter tenor / alternative structures** (weekly rolls, ratio spreads,
   calendars) — untested, and would also yield more independent observations to
   fight the 43-obs small sample.

**Recommended next step (per the validation critic): walk-forward-validate the
DVOL-richness filter (option 2) offline on data already in hand — the cheapest
high-value test — and do NOT build the heavy Deribit options-adapter + delta
-hedge loop yet.**

---

## DVOL-richness filter — walk-forward / OOS validation (the recommended next step, now run)

Option 2 above was "suggestive, not proven" in the first pass. This section runs
the recommended test: does the "sell vol only when DVOL is rich" rule survive a
**causal / out-of-sample** design? Engine: `backtest/sweep/vrp_richness.py`
(6 tests, incl. a look-ahead-free property test). The honest causal rule trades
full size iff `DVOL[t] ≥ the p-th percentile of DVOL over the trailing
lookback` (strictly before t) — a relative, adaptive cutpoint, no global tuning.

Economic context: corr(entry DVOL, cycle P&L) = **+0.638**; the high-DVOL half of
cycles averages **+12.3 vp** vs **+0.2 vp** for the low-DVOL half. The premium is
genuinely concentrated in rich-IV entries.

| variant | deploy | Sharpe | matched-tail %/yr | worst | subset-null |
|---|---|---|---|---|---|
| naked | 1.00 | 1.24 | 40.6 | −10.1% | — |
| in-sample best abs threshold (overfit) | 0.58 | 1.56 | 50.7 | −10.6% | 95th |
| **causal trailing-median (stand aside)** | **0.44** | **1.54** | **64.5** | −12.2% | **97th** |
| causal trailing-median (half size) | 0.72 | 1.44 | 65.1 | −11.1% | 97th |
| expanding-window OPTIMISED threshold (OOS) | 0.70 | 1.07 | **36.1** | −50.1% | — |

> **Note: the `matched-tail %/yr` column sizes off the WHOLE-SAMPLE CVaR — a
> backtest-wide sizing look-ahead. The honest causal-sized figures are in
> finding 3 below.** A 5-agent adversarial validation (2026-06-04) reproduced
> every number to the digit and forced three corrections, folded in here.

Findings (corrected — the selection signal is real; the headline %/yr and the
"tail-reduction" framing were not):
1. **The causal high-DVOL SELECTION signal is real.** The DVOL-selected cycles
   beat random same-count subsets at the **99.9th percentile on mean**, 97.2nd on
   Sharpe, 96.6th on matched-tail return; a random same-count subset beats naked
   only **12.4%** of the time. High-IV entries genuinely earn more, causally
   (trailing-percentile, strictly `< t`). **But borderline under multiple
   testing:** across 36 (lookback × pctl) configs the subset-null median is 95th
   and **0/36 clear Bonferroni** — real, not robust on 43 obs.
2. **NOT a tail-reduction overlay — retract that framing.** At the matched 10%
   CVaR budget the realised **worst month is *worse* for the filter (−12.2% vs
   naked −10.1%)**: it cuts the −50 vp cycle then re-levers ~1.9× on the surviving
   tail. The benefit is *leverage at equal whole-sample CVaR*, not a smaller
   drawdown.
3. **The 40.6 → 64.5%/yr headline is a sizing look-ahead.** `matched_tail_ann`
   sizes off the whole-sample CVaR (worst 2 of 43 cycles). Re-sized with a
   **causal expanding-window CVaR** (`matched_tail_ann_causal`, prior cycles
   only) the gap collapses to **+4 to +9 pp** (naked ~48–52 vs causal ~55.5%/yr).
   The honest deployable uplift is single-digit pp, not +24.
4. **The whole %/yr edge traces to one-to-two cycles.** Drop the −50 vp cycle and
   causal still edges naked on %/yr (66 vs 52) but **loses Sharpe** (1.56 vs 1.70);
   drop the **two** worst dollar cycles and the %/yr **win reverses** (naked 72.4
   vs causal 67.6). The grid "94% of 72 configs beat naked" is the *same single
   −50-cycle exclusion* re-expressed across correlated configs — one event, not 72.
5. **Confirmed (these hold):** the signal is genuinely look-ahead-free; the
   +0.638 corr and +12.3 vs +0.2 vp half-split are real; and **tuning the cutpoint
   FAILS OOS** — the expanding-window *optimised* threshold keeps the −50 cycle
   (entry DVOL 47.1) → Sharpe 1.07 / 36%/yr / worst −50 vp, *below* naked. The
   robust form is the parameter-light trailing rule, not an optimised number.

**Corrected verdict on option 2:** the DVOL-richness rule is a **causal entry
filter with genuine, null-beating per-trade SELECTION skill — but not a risk
overlay.** It does not reduce the realised drawdown (it raises it), its headline
%/yr gain is mostly a whole-sample-CVaR sizing artifact (+4–9 pp under causal
sizing), it is borderline under multiple testing, and the entire edge rests on
1–2 of 43 cycles. Deployable form: a modest *entry gate* (skip cheap-DVOL months),
**causally** sized and small — not a leverage/tail overlay.

**The statistical deepening is now exhausted** — 43 non-overlapping cycles with
the edge resting on one-to-two events; no further re-slicing fixes n. The only
step that adds genuine new information is **forward data**: collect the Deribit
DVOL surface live and paper-log the causal gate going forward (≥~12 independent
forward cycles) before any runner. Do **not** build more in-sample grids, and do
**not** ship a live executor on this evidence.

## Deribit feasibility (for the eventual build — not the gating step)

Probed against the public Deribit API:
- **DVOL** (the engine's vol input) is **fully backfillable to 2021** via the
  repo's paginating fetcher — valid and reproducible.
- **The full option SURFACE is live-snapshot only.** `get_book_summary_by_currency`
  / `get_ticker` return per-strike `mark_iv` + `bid/ask_iv` + greeks (a real
  25SEP26 skew read: 77 IV @30k → 39 ATM → 65 @300k) — but Deribit serves only
  ~24h of per-trade IV and **no expired-option chain**, so **historical skew
  cannot be backfilled.** Replacing the engine's parametric `skew_volpts` with
  real IVs needs a **forward-collector** started today (snapshot the book on a
  schedule). Worth running as a *background* data collector, not a blocker.
- **Adapter** = OAuth2 (`client_credentials`) token manager + WS market-data
  (book/ticker per expiry) + REST order router (`post_only` maker on options,
  IOC on the perp hedge) + a greeks reconciler (`get_account_summary`). Options
  are inverse, priced in BTC, fee ~3 bps (capped 12.5% of premium). **Delta-hedge
  venue = BTC-PERPETUAL on Deribit itself**, same (portfolio-margin) account —
  one-account loop; `get_ticker` already returns server-side greeks. **Perp
  funding on the hedge leg is a real recurring cost the engine does NOT model
  yet** (flagged gap).
- **Regulatory (NL retail, 2026): MED, and unlike OKX-EU/BloFin it is not
  hard-blocked.** Deribit (offshore Panama entity, now Coinbase-owned) does not
  restrict NL; an NL individual can KYC and trade options today — but it is
  **unregulated-in-EU** (crypto options fall under MiFID II, not MiCA, so MiCA
  can't legitimise them). The compliant route (Coinbase EU, MiFID II, NL live) is
  **futures-only in 2026**; options are roadmap. So the venue is *accessible* but
  *compliance-grey* — a live-decision gate, not a research gate.

## Open gaps (honest)
1. ~~Walk-forward / OOS validation of the richness filter~~ — **DONE** (section
   above): the causal SELECTION signal survives (null-beating), but it is **not** a
   risk overlay (realised drawdown larger), the %/yr gain is mostly a sizing
   look-ahead (+4–9 pp causally), it's borderline under multiple testing, and the
   edge rests on 1–2 of 43 cycles. The dataset is exhausted — next info is forward
   data only.
2. **Moving-block bootstrap null** — only the weak sign-flip null exists.
3. **Hedge-leg perp funding** unmodelled — a real recurring cost on an inverse
   perp; could erode the 6.4 vp; never quantified.
4. **Shorter tenor / alternative structures** untested.
5. **Real surface skew** unavailable historically (parametric only; the
   wing-fail conclusion is robust across the swept 0/5/10 vp range, but the exact
   crossover is assumption-driven).
6. **Multiple-testing** correction across the full grid only spot-checked.

## Modelling conventions
- Wings priced at ATM IV + parametric `skew_volpts` (only the 30d ATM index is
  available, not the surface). Hedging at frozen entry IV is the standard
  "hedge at implied" convention; terminal P&L is IV-path-independent (held to
  expiry, settle at intrinsic). 43 non-overlapping monthly obs is small; the
  roll-offset sweep is the mitigation. Full walk-forward + ≥1000-rep proper
  nulls + the live regulatory/risk review remain required before any real-money
  VRP.
