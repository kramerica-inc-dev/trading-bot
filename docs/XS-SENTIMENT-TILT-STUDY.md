# Asymmetric sentiment-tilt + stop-loss on the momentum basket (2026-06-06)

**Question (user):** the live lane is a DOLLAR-NEUTRAL cross-sectional momentum
basket (long top-3 / short bottom-3 of 10 OKX perps, lb=120, rebal=5). Can a
market-SENTIMENT / regime signal **asymmetrically tilt** the book — net-LONG in a
bull, net-SHORT in a bear — and a **stop-loss** tame sudden reversals? Does it beat
the neutral baseline OUT-OF-SAMPLE after the null gate and on drawdown? Express the
answer as knobs for conservative / balanced / aggressive.

**Answer: NO. Keep the dollar-neutral baseline (tau=0). No tilt clears the OOS
null, and every tilt monotonically worsens drawdown.** The tilt's apparent OOS gain
is the directional beta riding the holdout regime — the *random* tilted basket
captures it just as well (the tilt does not clear its own tilt-carrying null) — i.e.
it is exactly the fragile pro-cyclical beta artifact `docs/XS-BETA-STUDY.md` already
flagged, now amplified. A stop-loss does not rescue it: the trailing equity stop
*cuts* OOS return; a regime flip-to-neutral helps marginally but still fails the gate.

`backtest/sweep/xs_sentiment_tilt.py` + `scripts/study_sentiment_tilt.py`, OKX 3.45y
daily panel (10 assets, 1259d), train-70 / holdout-30 continuous (book carried across
the boundary, like `docs/XS-TRIGGER-STUDY.md`), cost_rate=0.0015 (same as xsectional)
+ ~6%/yr flat funding drag on gross. Gross held constant at 2.0 across tau (fair
comparison); realized net exposure reported. Writes
`backtest/results/sweep/xs_sentiment_tilt.json`.

## Lookahead control (the #1 trap) — PASSED

The regime signal s_t in {+1 bull, -1 bear} is the sign of BTC's trailing return
over window W using closes **strictly before t**: `sign(close[t-1]/close[t-1-W]-1)`.
The day-t book is set at the close of t-1 and earns over t-1->t, so using close[t]
would peek at the very bar being traded. `_assert_no_lookahead` poisons closes[t:]
to +inf and confirms s[:t] is unchanged — **passed for W in {50,100}**. The sham
(shuffled asset ranking) FAILS the gate in every config (sham percentiles all <=79,
never >95) → the gate is discriminating; **no VOID configs, no data issues**.

Causal regime composition (traded region): bull 58-61% / bear 39-42% of days
(full sample is BTC-rising-dominated, as the priors note); the holdout is closer to
balanced (W100 holdout 44% bull). No neutral days — BTC trend rarely sits exactly flat.

## Tilt grid (lb=120/rebal=5/m=3) — full-sample | continuous OOS holdout

tau=0 is the dollar-neutral baseline; tau=1 == long-only in a bull / short-only in a
bear. "null" = percentile vs the random dollar-neutral basket carried through the
**same tilt** (so the null also holds the directional bet — isolating the *momentum-
ranking* contribution). Gate = OOS null > 95.

| W | tau | FULL net% | Sharpe | Calmar | maxDD% | null | **OOS net%** | **OOS Calmar** | **OOS maxDD%** | **OOS null** | net-exp |
|--|--|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| 100 | **0.0** | 134 | 0.76 | 0.62 | -45 | 100 | **-9.0** | **-0.25** | **-34** | **93.2** | 0.00 |
| 100 | 0.25 | 261 | 0.90 | 0.87 | -52 | 100 | +6.3 | 0.23 | -27 | 93.2 | 0.06 |
| 100 | 0.50 | 297 | 0.89 | 0.76 | -65 | 100 | +14.1 | 0.38 | -36 | 92.0 | 0.11 |
| 100 | 0.75 | 208 | 0.86 | 0.48 | -80 | 99.8 | +12.7 | 0.25 | -48 | 89.8 | 0.17 |
| 100 | 1.00 | 63 | 0.85 | 0.17 | -89 | 99.5 | +2.5 | 0.04 | -59 | 89.5 | 0.23 |
| 50 | 0.25 | 182 | 0.81 | 0.63 | -56 | 100 | -0.6 | -0.02 | -30 | 92.5 | 0.04 |
| 50 | 0.50 | 143 | 0.72 | 0.38 | -78 | 100 | -0.3 | -0.01 | -44 | 92.5 | 0.08 |
| 50 | 1.00 | -34 | 0.59 | -0.12 | -98 | 99.2 | -22.0 | -0.32 | -67 | 88.0 | 0.15 |

(W50 baseline == W100 baseline: tau=0 ignores W.)

### What the grid says
1. **In-sample, the tilt SHINES — and it's a mirage.** A W=100 tilt nearly triples
   full-sample net (134% -> 261-297%) and lifts full Calmar (0.62 -> 0.87 at tau0.25).
   This is the bull-sample artifact the priors predicted: the full sample is BTC-
   rising, so a net-long-in-a-bull tilt is rewarded. It is **not** evidence of edge.
2. **OUT OF SAMPLE, the tilt's net% beats the neutral baseline — but does NOT clear
   the null.** Baseline OOS is -9.0% (already sub-gate at 93.2). The best tilt
   (W100/tau0.5) lifts OOS to +14.1% / Calmar 0.38 — superficially a win. **But its
   OOS null is 92.0th — BELOW 95, and BELOW the baseline's own 93.2.** The random
   *tilted* basket captures the same +14% because the null carries the same
   directional beta. So the OOS gain is the **tilt's market exposure, not the
   momentum ranking** — precisely the fragile pro-cyclical beta of XS-BETA-STUDY,
   now amplified. Adding beta is not adding alpha.
3. **Drawdown degrades monotonically with tau — the risk is directional, and it's
   real.** Full maxDD -45% -> -65% -> -89% -> -98% as tau 0->0.5->0.75->1.0; OOS
   maxDD -34% -> -36% -> -48% -> -59%. tau=1 (long/short-only) is a portfolio-
   wrecker (full -98%, OOS -22% net). Any net% a tilt adds is bought with directional
   risk, exactly as the gate is designed to expose. W=50 is strictly worse than
   W=100 (noisier regime -> more whipsaw, lower OOS).

## Stop / de-risk overlays (on the best tilt, W=100 / tau=0.5)

| overlay | FULL net% | Calmar | maxDD% | OOS net% | OOS Calmar | OOS maxDD% | OOS CVaR5% | OOS null |
|--|--:|--:|--:|--:|--:|--:|--:|--:|
| none | 297 | 0.76 | -65 | 14.1 | 0.38 | -36 | -10.5 | 92.0 |
| **flip-to-neutral** | 295 | 0.75 | -66 | **17.0** | **0.47** | -35 | -10.5 | 91.8 |
| trail 10% (arm +5%) | 221 | 0.60 | -68 | **-5.0** | -0.13 | -39 | -10.4 | **78.2** |
| trail 15% (arm +5%) | 244 | 0.66 | -65 | 11.6 | 0.30 | -37 | -10.5 | 89.8 |
| flip + trail 10% | 215 | 0.59 | -67 | -2.6 | -0.07 | -38 | -10.4 | 78.8 |

### What the overlays say
- **Flip-to-neutral** (drop the tilt to 0 when the regime reverses the held tilt)
  is the only overlay that helps: OOS net 14.1 -> 17.0, OOS Calmar 0.38 -> 0.47,
  maxDD essentially unchanged. But it **does not change the verdict** — still 91.8th
  OOS null (< 95). It modestly de-risks the tilt; it does not create edge.
- **Trailing equity stop HURTS.** A 10% trail (arm +5%) *cuts OOS return* (14.1 ->
  -5.0) and drops the OOS null to 78th, **without** cutting drawdown (maxDD -36 ->
  -39, worse). It flattens into the holdout's choppy reversals and misses the
  recovery — the classic "stop bleeds in mean-reverting chop" failure. A looser 15%
  trail does less damage but adds nothing. **A stop-loss does not cut drawdown here;
  it trades return for nothing.** CVaR-5% (daily) is ~-10.5% across overlays — the
  stop doesn't even improve the tail.

## Regime split (bull-train vs flat-holdout) — the decisive contrast

The tilt's entire allure lives in the bull-heavy **train**: full-sample (train-
weighted) net jumps 134 -> 297% with the tilt. In the near-balanced **holdout**, the
baseline is already negative (-9%) and the tilt's nominal +14% is beta, not ranking
(null 92 < 95). This is the same pattern every XS-* study found: **the momentum edge
is a 2023-bull phenomenon; nothing here makes it regime-robust.** Tilting just
re-packages the bull exposure as explicit beta and charges drawdown for it.

## Decision & risk-profile knobs

**Verdict: NEUTRAL-BASELINE-STAYS.** No tau>0 beats tau=0 OUT-OF-SAMPLE on the
non-negotiable null gate (best tilt 92.0th, baseline 93.2th, gate 95) and every tilt
worsens drawdown. The OOS net% the tilt appears to add is the random-basket-shared
directional beta, not momentum alpha. The stop-loss does not cut drawdown. Do **not**
ship a sentiment tilt or a stop-loss on this evidence.

Mapped to the named risk profiles — separating in-sample shine from OOS reality:

| profile | defensible tau | stop | rationale |
|--|--|--|--|
| **conservative** | **0.0** (dollar-neutral) | none | the only setting that clears any null OOS-adjacent; lowest maxDD (-34% OOS). The tilt only adds directional risk. |
| **balanced** | **0.0** | (optional) regime **flip-to-neutral logged as a risk gauge, not a sizing lever** | flip-to-neutral is the one overlay that helps OOS without worsening DD, but it still fails the gate — defensible only as a de-risk reflex, never to *increase* exposure. |
| **aggressive** | **0.0–0.25 max, paper-only** | flip-to-neutral | tau=0.25/W100 is the least-bad tilt (full Calmar 0.87, OOS +6%) but its OOS null is sub-gate and OOS maxDD already -27 to -34%. Only justifiable as a **forward-paper experiment**, never live, and never above tau=0.25. tau>=0.5 is unjustifiable (maxDD -65%+). |

The actionable carry-over (consistent with XS-BETA-STUDY's recommendation): **log the
realized net exposure / net beta of the live book as a risk gauge** — `mean_net_exposure`
is in the JSON and the tilt machinery computes it per-day — so the pro-cyclical tilt
is *visible and bounded*, not amplified.

## Caveats
- The OOS null carries the same tilt, by design, so it tests the *ranking* increment,
  not the directional bet. That is the correct, conservative framing (we don't want to
  reward beta) — but it means a reader must not read the tilt's +OOS-net% as a pass; it
  isn't (null < 95). Stated explicitly to avoid that exact misread.
- Continuous holdout is a single 30% tail; like XS-TRIGGER, treat the OOS numbers as
  directional, not precise (one regime). W in {50,100} and 5 tau levels is a modest
  multiple-testing surface — nothing cleared, so no correction is needed, but a future
  "best tilt" claim would have to survive it.
- Funding is a flat ~6%/yr HL-calibrated stress on gross, not measured per-asset OKX
  funding (still ~3mo public history); the tilt adds a net leg whose funding sign
  depends on the direction — a live tilt would need real per-leg funding, another
  reason it is not live-ready. Costs are the conservative 15bps/|dweight|; a tilt that
  needed tighter execution to look good would be more fragile, not less.
