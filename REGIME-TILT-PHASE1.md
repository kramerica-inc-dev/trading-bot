# Plan E — Regime tilt Phase 1 (classifier + execution gate)

**Status:** Design only. Code is gated on (a) Plan E paper-PASS per P1
policy AND (b) classifier walk-forward AUC > 0.55 per P3.
**Created:** 2026-04-26
**Implements:** First phase of "dynamic regime tilt" — detect trending
regimes where the cross-sectional reversal thesis weakens, then gate
execution. Subsequent phases (sizing tilt, side-asymmetric gating,
signal flip) are explicitly out of scope; see §"Future phases".
**Prereq:** Plan E paper-PASS at end of P1 window. Classifier
walk-forward AUC > 0.55 on the strategy-aligned target (P3 bar).

---

## TL;DR

Plan E enters the basket precisely **because** the top-3 / bottom-3
have moved 72h in one direction; the entry hypothesis is reversion.
That hypothesis weakens in strong, persistent trends — the same
failure mode that made the legacy single-asset regime classifier (and
later Plan D's chop classifier) load-bearing.

The fleet already carries two single-feature regime heuristics:

- `plan-e-c` — BTC 24h-vs-30d vol ratio (Agent C, vol-halt)
- `plan-e-g` — breadth tail-skip via SMA-200 (Agent G)

Phase 1 of the regime-tilt program replaces both heuristics with a
**multi-feature trend classifier**, trained on a target aligned with
**the strategy's own forward P&L** rather than a price-move proxy.
The classifier output gates execution exactly the way Agent C/G do
today: emit a `skip` event when the model is confident the next 24h
basket P&L is in the bottom decile of the training distribution.

The PRD is intentionally narrow:

- Phase 1 only **gates** (skip the rebalance). It does not modify
  signal sign, leg notional, or per-side asymmetry.
- The classifier model is logistic regression with L2 — same
  discipline lesson Plan D step 1 enforced. Any fancier model is
  Phase 2+ work.
- The classifier must clear AUC > 0.55 walk-forward on the
  strategy-aligned target before it is allowed to gate live trades
  (P3). Until then it runs on the monitoring surface only.

---

## Why now (and why not earlier)

The original roadmap (`DECISIONS.md` 2026-04-18) put Plan E paper-only
for 2-4 weeks and then re-evaluated additions. Four reasons to draft
the regime classifier PRD inside the paper window rather than after:

1. **The data the classifier needs is already being generated.** Each
   instance is logging rebalance-level realized P&L and per-cycle
   universe state. Two more weeks of production paper data, layered
   on top of the existing 12-month backtest series, gives a clean
   walk-forward holdout. Building the offline pipeline now means we
   are ready to train against fresh data the moment paper-PASS lands.

2. **The single-feature gates' contribution is measurable in the same
   window.** If `plan-e-c` and `plan-e-g` each contribute < 0.3 OOS
   Sharpe lift over `plan-e-base` (the expected case from the agent
   walk-forward results), the multi-feature classifier is the
   obvious next step regardless of the precise paper-PASS verdict.
   The PRD then specifies what "next step" looks like in advance, so
   we are not designing under post-PASS time pressure.

3. **P3 has a long fuse.** Walk-forward classifier validation is an
   offline backtest task — no live exposure, no execution surface
   touched. It can run in parallel with the paper window and surface
   a go/no-go on the AUC bar before the paper window itself
   concludes.

4. **The label is novel and worth burning down early.** The Plan D
   post-mortem (`PLAN-D-step3-final.md`) showed a classifier with
   AUC 0.78 on its own target failed at the strategy level because
   the target (5m z-reversion) wasn't aligned with the strategy's
   exit mechanics. Phase 1 of the Plan E classifier inverts that
   risk by training directly on basket-level forward P&L. Catching a
   target-misalignment problem at this stage is much cheaper than
   the equivalent diagnosis post-paper.

---

## Phase 1 scope (and what is explicitly Phase 2+)

**In Phase 1:**

- One classifier model (`scripts/regime_classifier_e.py`).
- One target: realized 24h basket P&L (long-3 minus short-3 at the
  time of the cycle, mark-to-mark over the next rebalance interval),
  binarized at the bottom-quartile threshold of the training
  distribution.
- One feature set (six universe-level features, see §"Classifier
  design").
- One gate (`RegimeTiltConfig`), one paper variant (`plan-e-regime`),
  one dashboard surface.
- Skip-only behavior: when the classifier fires, the rebalance is
  skipped, existing positions stay open, the cycle is logged as
  `action: "skip"` with `reasons: ["regime_tilt"]`. No notional
  scaling, no side-asymmetry, no signal flip.

**Phase 2+ (out of scope; listed only to fence Phase 1):**

- **Sizing tilt** — scale `leg_notional_pct` by classifier
  confidence rather than skip-or-don't.
- **Side-asymmetric gate** — skip only the long legs in
  trend-up regimes, only the shorts in trend-down. Requires a
  3-class classifier (trend-up / chop / trend-down) and a separate
  P3 evaluation per side.
- **Signal flip / MOM-in-trend** — replicate the legacy "regime
  flip" idea on the cross-sectional surface. Banned until Phase 1
  has demonstrated AUC > 0.6 OOS for at least 3 months and the
  Phase 2 sizing tilt has proven non-destructive.
- **Per-asset gating** — exclude individual legs based on
  per-symbol features (auto-correlation, OI divergence, funding
  state). Subsumes Plan A and Plan C. Considered after Phase 1 +
  Phase 2.

The phase split is principled: every phase is one strict superset of
the previous, and every transition is gated by a P3-style AUC test
on the **next** phase's target.

---

## Classifier — design

### Target label

**Definition:** for a cycle anchored at UTC `t`, with long set `L_t`
and short set `S_t` (the basket Plan E would actually open if no gate
fired), let `R_t` be the equal-weighted realized return of
`(L_t − S_t)` over the interval `[t, t + rebalance_interval_hours]`.
The label is `1` if `R_t < q_25` (training-set 25th percentile),
else `0`.

**Why this label:**

- Aligned with the strategy's own exit mechanics: the rebalance
  interval is the holding period, the equal-weight basket is the
  position. There is no proxy in between.
- Bottom-quartile threshold (rather than negative-only) gives a
  more balanced training set — typical Plan E cycles are slightly
  positive on average, so a "loss day" gate would have very few
  positives. The 25th-percentile cutoff puts the classifier on the
  most informative tail without making the problem a needle-in-
  haystack.
- The label is a function of the basket *Plan E would open*, not
  the universe at large. A classifier that learns "BTC will trend
  hard" without knowing whether BTC is in the basket this cycle
  would not be helpful; this label folds the construction into the
  signal.

**Caveat:** the label uses forward returns, so training data has a
24h information bleed at the right edge. The walk-forward harness
must drop the most recent 24h of training rows in every fold.

### Features (universe-level, six)

| Feature | Definition | Intuition |
|---------|------------|-----------|
| `breadth_pos_72h` | Fraction of universe with positive 72h log-return | Trend up if near 1, trend down if near 0 |
| `breadth_above_sma200` | Reuses `compute_breadth_skip` math | High persistence above SMA-200 = trend |
| `xs_dispersion_72h` | Std of 72h log-returns across universe | High dispersion = leaders pulling away (trend) |
| `btc_vol_ratio_24_720` | Reuses `compute_vol_halt` math | Vol regime |
| `btc_trend_strength` | `|btc_72h_log_return| / btc_30d_sigma` | Single-asset trend magnitude (BTC is the only asset large enough to drive the basket directly) |
| `xs_rank_autocorr_72h` | Spearman corr of cross-sectional 72h-return rank at `t` vs at `t − 72h` | High auto-corr in ranks = persistent ordering = trend; low/negative = reshuffling = chop |

All features are universe-level scalars at cycle time. No per-asset
features in Phase 1 (those belong in per-leg gating, Phase 4+).

The first four reuse existing helper functions from
`plan_e_runner.py:346-413`; the last two are new and live alongside
the classifier module.

### Model

**`sklearn.linear_model.LogisticRegression(penalty="l2", C=1.0,
class_weight="balanced", max_iter=2000)`.**

Logistic regression with L2 was the discipline imposed on Plan D
step 1 for the same reason: the experiment under test is the
*target definition*, not the model class. If a logistic regression
on six well-chosen features can't clear AUC 0.55, no boosted-tree
will rescue the target.

Features are standardized (`StandardScaler` fit on train fold, frozen
into the saved artifact). Calibration is checked but not enforced —
the gate uses a probability threshold, not an absolute calibration.

### Walk-forward harness

Mirrors `backtest/plan_d_walk_forward.py` structure:

- 12 months of historical data + paper-window addendum once
  available.
- 3 rolling windows: train 6 months, test 3 months, step 3 months.
- Classifier retrained per window. No parameter reselection between
  windows.
- Holdout metric: AUC on the test fold.

**Gate to deploy:** all 3 windows show AUC > 0.55 on the test fold.
Any single fold below the bar disqualifies the classifier; we then
either revise the target, revise the feature set, or close out the
PRD with a "tried and failed" entry in `DECISIONS.md`.

### Persistence

Trained model + scaler serialized to
`scripts/models/regime_classifier_e_<train_end_date>.joblib`. The
runner's `RegimeTiltConfig` carries the path. Atomic load on
startup; no auto-refresh in Phase 1 (manual retraining is a known,
explicit operation per P3).

---

## Gate — design

### Config

New dataclass in `plan_e_runner.py` alongside `VolHaltConfig` /
`BreadthSkipConfig`:

```python
@dataclass
class RegimeTiltConfig:
    """Phase 1: skip rebalance when regime classifier fires."""
    enabled: bool = False
    model_path: Optional[str] = None
    p_threshold: float = 0.60       # gate fires when p(loss) > this
    min_features_required: int = 6  # fail safe: skip the gate if any
                                    # feature is NaN or unavailable
```

Loaded by `load_config()` the same way the existing flags are.

### Diagnostic + integration point

New helper alongside `compute_vol_halt` (`plan_e_runner.py:346`):

```python
def compute_regime_tilt(
    closes: Dict[str, pd.Series], cfg: RegimeTiltConfig,
) -> Tuple[bool, Dict[str, Any]]:
    """Return (should_skip, diagnostics) — log-only when disabled."""
```

Integration in `do_rebalance_cycle()` immediately after the
existing vol-halt + breadth-skip block at
`plan_e_runner.py:894-911` and **before** the outlier-exclude /
selection step. The skip path piggybacks on the existing
`halt_reasons` machinery — adding `"regime_tilt"` to that list is
all the wiring needed.

The diagnostic always runs, even when `enabled=False`, and surfaces
into the cycle log under `gates.regime_tilt`. This means we get
shadow-mode telemetry (model output but no enforcement) for as long
as the classifier needs to prove itself.

### Skip-event log shape

Existing `skip` log entries get one additional key:

```json
{
  "ts": "...",
  "action": "skip",
  "reasons": ["regime_tilt"],
  "gates": {
    "regime_tilt": {
      "enabled": true,
      "p_loss": 0.74,
      "threshold": 0.60,
      "skip": true,
      "features": {
        "breadth_pos_72h": 0.9,
        "breadth_above_sma200": 0.8,
        "xs_dispersion_72h": 0.041,
        "btc_vol_ratio_24_720": 0.85,
        "btc_trend_strength": 1.62,
        "xs_rank_autocorr_72h": 0.71
      },
      "model_id": "regime_classifier_e_2026-04-15"
    },
    "vol_halt": { ... },
    "breadth_skip": { ... }
  }
}
```

Per-feature values land in the log so post-hoc analysis can ask
"which feature was load-bearing on the false-skip days."

### Interaction with existing gates

The three gates compose as **OR** — any of them fires → skip. This
matches the existing `plan-e-cg` (C+G stacked) semantics. There is
no priority / short-circuit: all three diagnostics run every cycle
so the dashboard can show why the cycle was skipped.

When `plan-e-regime` and `plan-e-cg` reach paper-PASS, the next
fleet decision is which combination (regime alone vs. regime + C
vs. regime + G vs. all three) survives walk-forward. That is a
fleet-level decision after Phase 1 ships, not a Phase 1 decision.

### Circuit breaker / reconciliation interaction

- CB and reconcile run **before** the regime tilt check (existing
  ordering at `plan_e_runner.py:852-892` / `828`). A CB-halted
  state still skips regardless of regime; reconcile failures abort
  before any gate is evaluated.
- The regime gate does not modify portfolio state. It only emits a
  `skip` event. No CB-tripped-flag interaction; no reconcile rule
  change; no funding-tracker interaction.

---

## Paper instance

`configs/plan-e-regime.json` — same baseline as `plan-e-base` but
with `regime_tilt.enabled=true` and a frozen model artifact path:

```
{
  "instance_name": "plan-e-regime",
  ... (baseline fields identical to plan-e-base) ...
  "regime_tilt": {
    "enabled": true,
    "model_path": "scripts/models/regime_classifier_e_2026-04-15.joblib",
    "p_threshold": 0.60
  }
}
```

Added to `FULL_INSTANCES` in `deploy/deploy_multi.sh`. Becomes the
9th paper instance after `plan-e-trail` / `size17` / `maker50`. Runs
alongside the existing fleet on shared market data, circuit breaker
active, identical to `plan-e-base` in every other parameter.

---

## Failure modes

| Mode | Behavior |
|------|----------|
| Model file missing on disk | Treat as `enabled=false`. Log `"reason": "model file not found"`. Cycle proceeds. |
| Model load raises | Log + treat as disabled. Same as above. Don't crash the cycle. |
| Any feature evaluates NaN/inf | Skip the gate (not the cycle). Log `"reason": "feature N/A"`. Cycle proceeds. |
| Classifier returns NaN probability | Skip the gate. Log `"reason": "model output N/A"`. |
| `p_threshold` set < 0 or > 1 | Validation error in `load_config`; runner aborts on startup with a clear message. |
| Stale model (`train_end_date` > 90d ago) | Warn in dashboard; do **not** disable. Operator's call. |
| Two regime instances on same artifact | Fine — model artifact is read-only, no contention. |

The failure-mode discipline is consistent with existing gates
(`compute_vol_halt` returns `(False, {...})` on insufficient data
rather than crashing the runner). When in doubt, the gate fails
**open** — i.e., does not skip — so a broken classifier never
blocks the fleet from trading.

The one exception: P3 strict mode. A future `--regime-strict` flag
could fail closed (abort the cycle) if the model is missing in the
specific instance configured to use it. Out of scope for Phase 1.

---

## Persistence + dashboard

### State

No new fields on `PortfolioState`. The skip log already carries
everything needed for post-hoc analysis. (Unlike CB / funding /
reconcile, regime tilt is stateless across cycles.)

### Dashboard

Risk tab gains a "Regime gate" card alongside the existing CB and
Reconcile cards. Fields:

- Enabled? (yes/no)
- Last fired (ts of most recent `regime_tilt` reason)
- Skip rate over last 30 cycles
- Current `p_loss` (latest cycle)
- Model id / training cutoff date

Compare tab gains a column showing `regime_tilt_skips_total` so
fleet skim-reads pick up "this variant is skipping more than its
peers."

`api/plan-e/status?instance=plan-e-regime` returns the same
diagnostic block the runner logs, so the dashboard can surface
per-feature values for the most recent cycle.

---

## Test plan

Embedded in a new validation script
`tests/test_regime_classifier.py` (mirroring the funding/CB/
reconcile validation pattern):

- **Feature engineering:** 6 unit tests, one per feature, with
  hand-rolled fixtures verifying the math against a numpy
  ground-truth.
- **Walk-forward harness:** train/test split correctness — no
  forward-looking leakage at the right edge of the training fold.
- **Model serialization:** save/load round-trip preserves
  predictions to within 1e-9.
- **Gate integration:** synthetic cycle inputs that drive
  `compute_regime_tilt` to fire / not-fire, check the skip log
  shape.
- **Failure paths:** missing model file, NaN feature, NaN
  probability — gate fails open in each case, runner does not
  crash.
- **End-to-end:** dry-run cycle on a fixture universe with a known
  trending day → verify a skip is emitted and the log has the
  expected feature snapshot.

---

## Engineering effort estimate

| Component | Estimate | Risk |
|-----------|---------:|------|
| Target-label generator (basket-aligned forward P&L) | ~3h | medium (off-by-one on the basket construction) |
| Six features + universe scalars | ~3h | low |
| Walk-forward training script + AUC report | ~3h | low |
| Classifier serialization + load helper | ~1h | low |
| `RegimeTiltConfig` + `compute_regime_tilt` + integration in `do_rebalance_cycle` | ~2h | low |
| `configs/plan-e-regime.json` + `deploy_multi.sh` entry | ~30m | low |
| Dashboard surfaces (risk-tab card + Compare column) | ~2h | low |
| Test suite per §"Test plan" | ~3h | medium (feature math edge cases) |
| Walk-forward AUC report → DECISIONS.md gate entry | ~1h | low |

**Total: ~18h.** Same order as the maker-execution PRD; the work is
mostly offline (training pipeline, walk-forward script) plus a thin
integration into the runner. The risk profile is dominated by the
target-label correctness, not the model or the integration.

---

## Decision gate (P3 — go/no-go before live use)

The classifier passes Phase 1 iff **all** of:

- Walk-forward AUC > 0.55 on the strategy-aligned target across all
  3 folds.
- `plan-e-regime` paper-window OOS Sharpe ≥ `plan-e-base` − 0.2
  (allow a small drag from skips).
- `plan-e-regime` skip rate ≤ 30% (a gate that suppresses one in
  three rebalances is gating too aggressively for a binary surface
  — Phase 2 sizing tilt is the right home for that level of
  intervention).
- No CB halt on `plan-e-regime` unless `plan-e-base` also halts in
  the same week (skip rate must not be hiding a different
  pathology).

If any criterion fails, Phase 1 is closed out with a decisions-log
entry documenting the failure mode. Phase 2 is then re-evaluated
from scratch — it does not inherit Phase 1's classifier.

---

## What this plan explicitly does NOT do

- Does not change paper behavior of any existing instance. Phase 1
  is purely additive; `plan-e-base` and the seven existing variants
  remain unchanged.
- Does not introduce financial leverage (orthogonal program;
  separate prereqs per the leverage analysis).
- Does not modify the signal, lookback, k_exit, sizing, or any
  other edge-relevant parameter. Execution-gating only (P5).
- Does not promote regime-tilt to default. Even on PASS, the gate
  ships as a paper variant alongside `plan-e-base`. Promotion to
  default is a separate fleet-level decision after Phase 1 + Phase
  2 paper data.
- Does not replace `plan-e-c` / `plan-e-g`. Those instances stay in
  the fleet so the fleet decision (regime alone vs. regime + C/G)
  has data to chew on.
- Does not touch the legacy "advanced" strategy (P4 freeze still
  binding).

---

## Decision log

- 2026-04-26: Phase 1 PRD drafted inside the Plan E paper window
  (per "why now" §1-4). Gated on Plan E paper-PASS + classifier
  walk-forward AUC > 0.55. Phase 2+ (sizing tilt, side-asymmetric
  gate, signal flip, per-asset gating) explicitly fenced.
