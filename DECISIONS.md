# Strategy & Architecture Decisions

A dated log of binding decisions about strategy direction, feature scope, and
what is deliberately **not** being worked on. Entries are append-only; if a
decision is reversed, add a new entry rather than editing the old one.

---

## 2026-04-18 — Current feature set is frozen; edge must come from new information

**Context.** On 2026-04-17 an ML diagnostic (logistic regression on the seven
regime-condition features vs. 12-bar forward returns) produced AUC **0.5030**
— statistically indistinguishable from random. Ten rounds of threshold
re-tuning, weight re-balancing, regime-conditioning, and grid expansion across
two competition rounds had been rearranging a predictor set that has no
measurable edge on forward returns.

**Decision.** The existing seven-condition feature set is frozen. No further
optimization work will be done on it. Future strategy lift must come from
**new information sources**, not from re-aggregating existing features.

**Banned for the next 3 months unless a concrete bug is found:**
- Re-running `backtest/calibrate_per_timeframe.py` with new parameter grids
- Re-balancing the weights / score contributions of the seven trend conditions
- Adjusting `trend_min_score`, `min_confidence`, ATR multipliers, or anchor
  thresholds on the current strategy
- "One more calibration pass" on `efficiency_ratio`, `trend_strength`, or
  `anchor_slope`

**Still allowed:**
- Bug fixes if the scoring math is provably wrong
- Execution improvements (order sizing, slippage, TP/SL reliability, circuit
  breakers, reconciliation)
- Monitoring, observability, logging improvements
- Adding genuinely new signals (funding rate, open interest, cross-sectional,
  on-chain). These introduce new information; they are not tuning the
  existing strategy.

**Review condition.** Revisit this decision only after at least one new
information source (funding rate, OI divergence, or cross-sectional ranking)
has been live for 4+ weeks with measurable results. At that point we will
have a new baseline to compare against.

**Why this matters.** The temptation under drawdown will be to "tune our way
out." The AUC result says that path is closed. Recording the decision here
so future work does not drift back into it by default.

---

## 2026-04-18 — Planned path: A → C → D → E (funding → OI → mean-reversion → cross-sectional)

**Context.** After freezing the current feature set, we identified four
candidate work streams that add new information or new strategy surface:
A (funding rate signal), C (OI divergence filter), D (mean-reversion strategy
for chop regimes), E (cross-sectional multi-asset). B (maker-only execution)
was considered and deferred — realistic fee savings are ~4bps round-trip and
likely eaten by adverse selection.

**Decision.** Execute in strict order A → C → D → E. Each phase must be live
and measured for at least 2 weeks before the next begins. Do not parallelize
— risk budget and debugging surface are the constraints, not engineering time.

**Rationale.**
- A introduces the single most documented retail-accessible edge (funding).
  Cheapest to ship, highest expected information gain.
- C reuses A's data infrastructure and acts as a filter on both the legacy
  trend strategy and A.
- D is a new strategy, not an addition. Doubles monitoring surface. Requires
  strategy router. Must wait until A+C are stable.
- E is the largest project and the most durable edge, but requires
  multi-symbol data collection, portfolio accounting, and a restructured
  backtest framework. Defer until A/C/D validate the approach.

**Review condition.** Each phase gates on the previous phase being live and
not introducing regressions. If A fails to show any lift after 4 weeks live,
pause before C and re-examine assumptions.

---
