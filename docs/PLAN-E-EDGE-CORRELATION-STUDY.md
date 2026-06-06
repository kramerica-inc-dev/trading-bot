# Plan E (cross-sectional REVERSAL) — OOS edge gate + correlation with momentum (2026-06-06)

**Questions (user):** (1) Does **Plan E** — the cross-sectional 72h REVERSAL basket
(long laggers / short leaders, top-3 / bottom-3 of 10 OKX perps, daily rebalance,
k_exit=6 hysteresis, sign=-1) — clear a **proper null OUT-OF-SAMPLE**? It was deployed
(8 stop-loss variants + a trailing winner) but **was never null-gated.** (2) Is its
daily return stream **uncorrelated** with the live cross-sectional **MOMENTUM** lane
(lb=120 / rebal=5 / m=3, sign=+1, no exit)? Decision tree: edge ✘ → retire/keep-paper;
edge ✔ + low |corr| → blend (2-sleeve neutral); edge ✔ + strong negative corr → pick
the better one.

**Answer: RETIRE / keep-paper. Plan E has NO genuine OOS edge — it is a COST-TRAP, not
a no-edge.** On the OKX 3.45y daily panel the reversal ranking *does* sort better than a
random reversal basket (net-cost OOS null reads 100th), but the obs **loses money**
(OOS net **-22.83%**, full **-87.92%**) and, decisively, **fails its OWN zero-cost OOS
null (91.5th < 95th)** once turnover cost is neutralized on both sides. The 100th-pctile
reading is an artifact: daily-rebalanced random reversal baskets bleed turnover even
harder (OOS null median -66%, p95 -45.8%), so Plan E "wins" the null while still going
to zero. The reversal IC is statistically real but tiny (+0.0349, p=0.0019) — far too
small to pay daily turnover. Correlation with momentum is **mildly negative**
(Pearson -0.19 full / -0.27 OOS), which would help a blend, **but the edge gate fails
first**, so the correlation question is moot for a live blend.

`backtest/sweep/xs_reversal.py` + `scripts/study_plan_e_edge.py`, OKX 3.45y daily panel
(10 assets, 1259d, 2022-12-23 → 2026-06-03), train-70 / holdout-30 continuous (book
carried across the boundary, like `docs/XS-TRIGGER-STUDY.md` and
`docs/XS-SENTIMENT-TILT-STUDY.md`), cost_rate=0.0015 (same as `xsectional`) + ~6%/yr
flat funding drag on gross. Writes `backtest/results/sweep/xs_reversal_edge.json`. A
faithful 1H cross-check (`backtest/plan_e_faithful.py`) is confirmatory only.

## ⚠ STALE-FILE TRAP (called out per instruction)

**`backtest/plan_e_cross_sectional.py` is STALE — do NOT use it to characterize Plan E.**
It uses a **24h** lookback (not 72h), **MOMENTUM** direction (`np.argsort(-signal)` with
**no sign flip** → long leaders / short laggers, the *opposite* of the live `sign=-1`
reversal), a **4h** rebalance (not 24h), and **no k_exit hysteresis**. It bears almost
no resemblance to the deployed Plan E. The faithful 1H check here imports the LIVE
decision functions (`compute_signal` / `rank_signals` / `select_positions`) directly
from `scripts/plan_e_runner.py` so the port cannot drift.

## CRITICAL trust gate — MOMENTUM ANCHOR (PASSED)

Before trusting any reversal number, the cloned continuous-carry engine must reproduce
the **known momentum result** on the MOM config (sign=+1, k_exit=None, lb=120/rebal=5/m=3):

| metric | this clone | reference |
|--|--|--|
| full-sample net | **133.58%** | XS-SENTIMENT-TILT W100 tau=0 baseline = 134% ✓ |
| full-sample null %ile | **100.0th** | XS-BREADTH U10 m3 = 100 ✓ (≥ 98 anchor) |
| sham percentiles | **[72.5, 29.2, 42.2]** (all < 95) → sham FAILS ✓ | |
| cross-sectional IC | **+0.0691, p=0.0179** | XS-BREADTH json = 0.0691 / 0.0179 ✓ (exact) |

The clone reproduces momentum to the decimal → **engine trustworthy; reversal numbers
proceed.** Lookahead poison-test (`closes[t:]=+inf` must not change any port return at
day < t) **PASSED for BOTH** the MOM path *and* the REV stateful k_exit=6 path — the
hysteresis carries no future information. Determinism confirmed (fixed seed → identical
nulls across two runs). Panel has **0 NaNs**.

## Plan E reversal — the edge gate (lb=3 / rebal=1 / m=3, sign=-1, k_exit=6)

| | full-sample | continuous OOS holdout |
|--|--:|--:|
| net % | **-87.92** | **-22.83** |
| Sharpe | -1.28 | -0.55 |
| Calmar | -0.51 | -0.62 |
| max DD % | -90.3 | -35.9 |
| CVaR-5% (daily) | -5.48 | -4.15 |
| **net-cost null %ile** | 98.0 | **100.0** |
| **zero-cost null %ile** | 62.0 | **91.5** ← fails > 95 |
| sham %iles | [82.0, 74.5, 18.5] → FAIL ✓ | |
| signed reversal IC | **+0.0349, p=0.0019** | |
| regime split (1st / 2nd half) | -70.9% / -58.5% | (both halves bleed) |

### What the gate says — adversarial honesty

1. **The 100th-percentile net-cost null is a COST-TRAP artifact, not an edge.** The
   strategy loses money (OOS -22.83%) yet reads 100th because the *random* daily-
   rebalanced reversal baskets it is benchmarked against bleed turnover even harder
   (OOS null distribution: median **-66.2%**, p95 **-45.8%**, max -24.0%). "Loses less
   than random" is not "makes money." This is exactly the kind of misleading null
   reading the V2.1 discipline warns about — the proper test must neutralize cost.

2. **Zero-cost is the decisive test, and it FAILS.** With cost=0 and funding=0 on BOTH
   obs and null, Plan E's gross OOS edge is +46.78% but the zero-cost random reversal
   baskets reach p95 **+54.74%** → obs sits at the **91.5th percentile, below the 95th
   gate.** So even before any friction, the reversal *ranking* adds essentially nothing
   over a random reversal basket. There is no causal cross-sectional reversal edge here
   worth paying for.

3. **The reversal IC is real but trivial.** Signed IC = +0.0349 (p=0.0019) — a genuine
   weak short-horizon reversal tendency (equivalently, momentum IC is negative at the
   3-day horizon, the mirror of the +0.069 at 120d). But |IC| barely clears the 0.03
   floor, and at daily rebalance the turnover cost (cost_rate 0.0015 × ~2.0 gross every
   day) dwarfs it. Cost destroys -47pp of the +47pp gross OOS edge.

4. **Robust to the grid — uniformly negative.** Every lb ∈ {2,3,5} × k_exit ∈ {3,6}
   config is net-negative full AND OOS; the net-cost OOS null is 99–100th everywhere
   (the same cost-trap). k_exit=6 always beats k_exit=3 (less turnover bleed: lb=3 OOS
   -22.8% vs -45.8%) — hysteresis helps, but never enough to turn positive. The sham
   fails in every config (no VOID, gate is discriminating).

## Robustness grid (REV, lb × k_exit) — full | OOS

| lb | k_exit | FULL net% | OOS net% | OOS net-cost null | OOS maxDD% | OOS Calmar | IC | sham | n_rebal |
|--:|--:|--:|--:|--:|--:|--:|--:|:--:|--:|
| 2 | 3 | -99.5 | -51.1 | 100.0 | -53.6 | -0.93 | 0.0314 | OK | 1256 |
| 2 | 6 | -94.1 | -26.3 | 100.0 | -35.4 | -0.72 | 0.0314 | OK | 1256 |
| 3 | 3 | -98.9 | -45.8 | 100.0 | -47.9 | -0.93 | 0.0349 | OK | 1255 |
| **3** | **6** | **-87.9** | **-22.8** | **100.0** | **-35.9** | **-0.62** | **0.0349** | **OK** | **1255** |
| 5 | 3 | -97.6 | -56.6 | 100.0 | -58.3 | -0.95 | 0.0428 | OK | 1253 |
| 5 | 6 | -86.7 | -33.6 | 99.0 | -41.3 | -0.79 | 0.0428 | OK | 1253 |

(lb=3 / k_exit=6 is the live config = bold row.)

## Correlation with the live MOMENTUM lane (same engine, slice [120:])

Both sleeves were simulated on the SAME 3.45y daily panel through the SAME continuous-
carry engine; daily-return series sliced `[120:]` (both warmed up).

| window | Pearson (p) | Spearman | n |
|--|--:|--:|--:|
| full | **-0.189** (p≈0) | -0.118 | 1138 |
| OOS holdout | **-0.266** (p≈0) | -0.116 | 377 |
| rolling-60d | mean -0.172, range [-0.812, +0.417], 74% of windows negative | | 1079 |

Correlation is **mildly negative and statistically significant**, never strongly
negative. *If* Plan E had an edge, this -0.19/-0.27 would make it a useful diversifier
for a 2-sleeve neutral blend (the decision tree's "blend" branch). **But the edge gate
fails, so this is moot** — you cannot blend a sleeve that has no causal edge and bleeds
cost; you would just be adding a negative-expectancy, mildly-negatively-correlated drag.

## Faithful 1H cross-check (confirmatory only) — DISAGREES in sign

`backtest/plan_e_faithful.py` runs the EXACT live `compute_signal` (72h, sign=-1) /
`rank_signals` / `select_positions` (k_exit=6) on the 1H panel
(`backtest/data/*_1H.csv`), daily 00:00 UTC rebalance, cost 0.0011/side (live taker +
slippage, no funding drag), same random-basket null.

| metric | faithful 1H | daily 3.5y OOS |
|--|--:|--:|
| coverage | ~1y (2025-04 → 2026-04) | 3.45y |
| net % | **+22.62** | -22.83 |
| Sharpe (ann) | 0.766 | -0.55 |
| max DD % | -29.6 | -35.9 |
| null %ile | 100.0 (p95 -21.3) | 100.0 (cost-trap) |
| n_rebal | 362 | 1255 |

**AGREE/DISAGREE: DISAGREES in the sign of net return.** The faithful 1H is net-POSITIVE
and clears its null; the daily 3.5y is net-negative. The divergence is fully explained
by (a) a much shorter, recent, lower-friction sample (cost 0.0011/side and no funding
drag vs 0.0015 + ~6%/yr) and (b) a favorable 2025-26 regime — not a contradiction of the
edge verdict. **The 3.5y daily null gate governs** (the 1H window is too short and too
cheap to gate on). The 1H positive is a recent-window + lower-cost artifact; it is *not*
evidence of a durable edge.

## Decision

```
edge ✘ (cost-trap, fails zero-cost OOS null 91.5 < 95) → RETIRE / keep-paper
```

**RETIRE from any live-capital consideration; keep paper-logging at most.** Plan E has no
causal cross-sectional reversal edge that survives cost on the 3.5y daily gate. The
mild negative correlation with momentum (-0.19/-0.27) would have made it a blend
candidate had the edge cleared — it does not, so there is nothing to blend. Live capital
stays with the momentum lane (`scripts/xs_runner.py`, already forward-papering). Do NOT
combine.

## Files

- `backtest/sweep/xs_reversal.py` — reversal engine (clone of `xs_sentiment_tilt` minus
  the tilt, plus sign-flip + index-space k_exit hysteresis; reuses `xsectional`
  primitives).
- `backtest/plan_e_faithful.py` — faithful 1H port of the LIVE runner logic
  (confirmatory).
- `scripts/study_plan_e_edge.py` — driver (anchor trust gate, edge gate, zero-cost null,
  correlation, robustness grid, determinism, decision).
- `backtest/results/sweep/xs_reversal_edge.json` — full results.
