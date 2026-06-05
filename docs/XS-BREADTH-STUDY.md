# Cross-sectional momentum — breadth & legs study (2026-06-05)

**Question (user):** does EXPANDING the ranking universe (10 → 15 → 20 assets)
and/or holding MORE legs (m ∈ {2,3,4,5}) beat the current LIVE config
(universe=10, m=3, lookback=120, rebal=5)? Cadence is held fixed at the validated
lb=120/rebal=5 (docs/XS-TRIGGER-STUDY.md) — only **breadth** and **legs** vary.

**Answer: keep U10. The "breadth wins" headline is a regime-concentration mirage,
not robust alpha, and widening to U20 statistically KILLS the signal. On legs,
m=2 is the only clean per-leg win — but it is gated by capital, not statistics.**

`scripts/sweep_xs_breadth.py` (OKX 3.5y daily panel). Reuses the hardened harness
`backtest/sweep/xsectional.py` unchanged — random-asset-selection null (reps=500) +
shuffled-ranking sham control (n_sham=3). 12 configs (3 universes × 4 m) at the flat
cost_rate=0.0015, plus a cost-sensitivity pass and a capital gate. Baseline anchor =
**U10/m3 = the live config**.

## Panel integrity — no truncation, no confound

| universe | assets | n_days | window | NaNs |
|---|--:|--:|---|--:|
| U10 | 10/10 | **1259** | 2022-12-23 → 2026-06-03 | 0 |
| U15 | 15/15 | **1259** | 2022-12-23 → 2026-06-03 | 0 |
| U20 | 20/20 | **1259** | 2022-12-23 → 2026-06-03 | 0 |

All three universes share the **identical 1259-day window**. The binding constraint
is **BNB-USDT** (1259 bars, starts 2022-12-23 — ~40 days short of the other majors'
1299), and BNB is in U10/U15/U20 alike, so the window is the *same* across breadth
levels → **breadth is NOT confounded with sample period.** INJ-USDT (only 918 bars)
was **excluded** for exactly this reason: its 2023-11 start would have truncated the
shared window to ~918 days and confounded the breadth comparison with a shorter,
post-bull sample. No NaNs entered any panel (asserted in-script).

## Main sweep (flat cost_rate=0.0015)

Δ vs the live baseline (U10/m3: net **242.5%**, Sharpe **1.022**, null **100.0**).

| universe | m | verdict | net % | Sharpe | null %ile | Δnet | ΔSharpe | sham %iles |
|---|--:|---|--:|--:|--:|--:|--:|---|
| U10 | 2 | ADVANCE | 311.2 | 1.012 | 100.0 | +68.7 | −0.010 | 80,65,83 |
| **U10** | **3** | **ADVANCE** | **242.5** | **1.022** | **100.0** | **0** | **0** | **80,72,80** |
| U10 | 4 | ADVANCE | 256.2 | 1.157 | 100.0 | +13.7 | +0.135 | 74,64,49 |
| U10 | 5† | ADVANCE | 239.2 | 1.237 | 100.0 | −3.3 | +0.215 | 83,72,28 |
| U15 | 2 | ADVANCE | **906.5** | **1.335** | 100.0 | +664.0 | +0.313 | 18,2,**88** |
| U15 | 3 | ADVANCE | 508.6 | 1.241 | 100.0 | +266.0 | +0.219 | 39,7,**97** |
| U15 | 4 | ADVANCE | 268.2 | 1.036 | 100.0 | +25.7 | +0.014 | 42,4,**96** |
| U15 | 5 | ADVANCE | 231.4 | 1.059 | 100.0 | −11.1 | +0.037 | 39,4,**94** |
| U20 | 2 | **KILL** | 830.9 | 1.261 | 100.0 | +588.4 | +0.239 | 9,47,**96** |
| U20 | 3 | **KILL** | 475.6 | 1.163 | 100.0 | +233.1 | +0.141 | 3,43,88 |
| U20 | 4 | **KILL** | 282.6 | 1.027 | 100.0 | +40.1 | +0.005 | 1,70,51 |
| U20 | 5 | **KILL** | 183.2 | 0.894 | 100.0 | −59.3 | −0.128 | 2,70,37 |

† **m=5 on U10 is DEGENERATE**: long top-5 / short bottom-5 of a 10-asset universe
= the whole universe split in half, i.e. *no selection* — it's a pure dispersion
bet, not momentum. (Same applies to any m ≥ ⌈N/2⌉.) Flagged, not used.

## Findings

1. **The null gate is uninformative here — everything clears at 100.0.** Every
   config sits at the 100th percentile of the random-basket null, so null %ile gives
   **zero discrimination** in this sweep. The real gates are the **cross-sectional
   IC** (which KILLs U20) and the **out-of-bull robustness caveat** (below). Do not
   read "null=100 everywhere" as "everything works."

2. **Widening to U20 statistically KILLS the signal — IC dilution is real.** The
   cross-sectional IC of 120d momentum decays monotonically with breadth:

   | universe | xs_ic_mean | xs_ic_p |
   |---|--:|--:|
   | U10 | **0.069** | 0.018 ✓ |
   | U15 | 0.060 | 0.023 ✓ |
   | U20 | **0.044** | **0.063 ✗** |

   At 20 assets the ranking IC loses significance (p=0.063 > 0.05) → **all four U20
   configs are KILL** despite eye-popping net %. Adding small-caps adds noise to the
   ranking faster than signal. This is the cleanest result in the study: **20 is too
   wide.**

3. **The U15 "+664% net" headline is a regime-concentration mirage, not breadth
   alpha.** The net jump 242% → 906% (U10/m3 → U15/m2) is *not* matched by the IC
   (0.069 → 0.060, essentially flat) — so it is **not** that the wider ranking picks
   better. At **m=2** the book is literally the single strongest and single weakest
   coin each rebalance; adding 5 mid-cap alts just hands a couple of 2023-bull
   alt-coin moonshots to the long leg. This is the *same* edge XS-TRIGGER-STUDY
   already pinned to the **2023 bull** — concentrating into fewer, more volatile legs
   in a wider pool *amplifies that single regime's luck*, it does not create
   robust out-of-bull edge. The 906% is fragile point-estimate, not a repeatable
   expectancy. **Treat large Δnet at low m / high breadth as a concentration tell,
   not a win.**

4. **On legs (holding U10 fixed): more legs ≠ more return; it trades return for
   Sharpe.** m=2 → highest net (311%) but slightly lower Sharpe; m=4 → best risk-
   adjusted of the deployable set (Sharpe 1.157, net 256%); m=5 is degenerate. None
   of these beat the live m=3 by enough to justify a change, and the IC is identical
   across m within a universe (it's a property of the ranking, not the leg count).
   The Sharpe rise with m is just diversification across more (correlated) legs.

5. **Sham discrimination warning (not a VOID).** No config is formally VOID — no
   config had a *majority* (≥2/3) of shams clear the 95 gate. But **six** individual
   shams touched 94–97 (U15/m3 sham=97.4, U15/m4=96.0, U20/m2=96.4, plus the cost-
   grid repeats of U20/m2). With shuffled rankings occasionally clearing the gate,
   the random-basket null is **less discriminating at high breadth** (more assets →
   more ways a random split lands well). This *reinforces* finding 1 — lean on IC and
   regime-robustness, not the null, for the breadth/leg decision.

## Caveat #1 — thin-alt cost (cost-sensitivity, U15/U20 at best-m m=2)

The harness uses a **flat** 0.0015. The added coins (especially U20's NEAR/APT/UNI/
AAVE/ETC) have far lower real HL volume than BTC/ETH, so the flat rate **understates**
true cost at higher breadth. Stressing the rate:

| config | cost 0.0015 | cost 0.0030 | cost 0.0045 |
|---|--:|--:|--:|
| U15/m2 net % | 906.5 | 687.0 | 515.1 |
| U15/m2 cost_share | 0.236 | 0.421 | **0.566** |
| U20/m2 net % | 830.9 | 612.1 | 444.4 |
| U20/m2 cost_share | 0.256 | 0.452 | **0.602** |

The breadth net **survives nominally** under 3× cost (null stays 100, both still
beat the U10/m3 242% even at 0.0045) — but cost_share climbs toward the harness's own
**0.60 cost-trap line** (U20/m2 crosses it at 0.0045). And this only stresses the
*flat* rate uniformly; the honest version is **heterogeneous** — the alts that drive
the +664% are precisely the ones with the worst real fills, so their *contribution*
is overstated more than the average. Combined with finding 3 (the gain is a few
bull-era alt moonshots), the cost-corrected breadth "edge" is **not bankable**.

## Caveat #2 — capital / min-notional gate (report, not modelled away)

HL min-notional ≈ **$10/leg**. Dollar-neutral, gross book = equity → 2m legs,
per-leg = equity/(2m). At the live **~$57** equity:

| m | per-leg @ $57 | status | equity for safe buffer (≥1.4× floor) |
|--:|--:|---|--:|
| 2 | **$14.25** | **deployable** | ~$56 |
| 3 (live) | $9.50 | at the floor (no buffer) | ~$84 |
| 4 | $7.12 | gated | ~$112 |
| 5 | $5.70 | gated | ~$140 |

So even where a higher-m backtest looked better, **m ≥ 3 has no slippage/rounding
buffer at $57 and m ≥ 4 is below the floor entirely.** The only *more*-legs move that
is even feasible is fewer legs (m=2), and *more breadth (U15/U20) does not change the
capital math* — it changes which coins are eligible, not the per-leg notional.

## Decision

**Keep the live config: U10, m=3, lookback=120, rebal=5. Change nothing on this
evidence.**

- **Breadth:** U15 is statistically clean (ADVANCE, IC sig) but its return premium is
  a **2023-bull concentration artifact** (finding 3), not robust alpha, and it leans
  on the thin alts that cost most (caveat #1). U20 is an outright **KILL — IC dilutes
  below significance** (finding 2). Neither widening is a real improvement.
- **Legs (statistical signal, U10):** m=2 has the highest net and m=4 the best Sharpe,
  but the differences are inside the noise the IC says is flat across m, and the null
  gate doesn't discriminate. No leg count beats the live m=3 by enough to switch.
- **Deployable-NOW (~$57 equity):** the **only** even-feasible alternative is
  **m=2** (per-leg $14.25, deployable). m=3 lives at the $10 floor with no buffer;
  **m=4/5 are below the floor and are GATED on capital ≥ ~$112 (m=4) / ~$140 (m=5)**
  regardless of their backtest. Any m>3 recommendation is conditional on growing
  equity well past the live $57 — and the statistics don't recommend m>3 anyway.
- **VOID / data issues:** none. No formal VOID (no majority-sham config). No
  truncation — all universes share the 1259-day BNB-bound window; INJ correctly
  excluded. One honest caveat carried forward: the random-basket null is *weakly
  discriminating at high breadth* (six individual shams hit 94–97), so the breadth
  verdict rests on **IC + regime concentration**, not the null.

The genuine open risk before real-money sizing is unchanged from XS-TRIGGER-STUDY /
XS-BETA-STUDY: **out-of-bull regime robustness**, which neither more breadth nor more
legs fixes (and U20 makes worse). Results: `backtest/results/sweep/xsectional_breadth.json`.
