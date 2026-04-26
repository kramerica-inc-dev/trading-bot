#!/usr/bin/env python3
"""Walk-forward validation for the Plan E regime classifier.

Design per REGIME-TILT-PHASE1.md §"Walk-forward harness".

Folds:
    train 6 months, test 3 months, step 3 months
    n_folds = as many as fit in the available data, minimum 2

Per fold:
    1. Compute features + raw target on the full data once (same pass).
    2. Train fold = data with timestamp in [train_start, train_end).
       Right edge is shrunk by hold_h bars to prevent forward-target
       leakage at the train/test boundary.
    3. Test fold  = data with timestamp in [test_start, test_end).
    4. Fit RegimeClassifierE on train slice (binarization threshold
       comes from train-fold quantile and is reused for test fold).
    5. Predict on test slice → AUC, base rate, calibration check.

Gate (per P3 in DESIGN-PRINCIPLES.md):
    PASS iff every fold's TEST AUC > 0.55.

The latest fold's model is saved to scripts/models/ so the runner can
load it once Phase 1 PASSes and the live gate is enabled.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backtest.plan_e_cross_sectional import load_universe  # noqa: E402
from scripts.regime_classifier_e import (  # noqa: E402
    DEFAULT_HOLD_H,
    DEFAULT_LONG_N,
    DEFAULT_LOOKBACK_H,
    DEFAULT_QUANTILE,
    DEFAULT_SHORT_N,
    DEFAULT_SIGNAL_SIGN,
    FEATURE_NAMES,
    RegimeClassifierE,
    binarize_to_loss_tail,
    compute_basket_forward_return,
    compute_features,
)
from sklearn.metrics import roc_auc_score  # noqa: E402


TRAIN_MONTHS = 6
TEST_MONTHS = 3
STEP_MONTHS = 3
AUC_GATE = 0.55
AUC_FALLBACK_VARIATIONS_BAR = 0.52   # below this stops Phase 1 outright (P3)


@dataclass
class FoldResult:
    fold_idx: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    train_n: int
    test_n: int
    train_pos_rate: float
    test_pos_rate: float
    threshold: float
    auc: float
    coef: pd.Series


def _months_offset(ts: pd.Timestamp, months: int) -> pd.Timestamp:
    """Add `months` calendar months to ts (UTC-aware)."""
    return (ts + pd.DateOffset(months=months)).tz_convert("UTC")


def build_folds(
    index: pd.DatetimeIndex,
    *,
    train_months: int = TRAIN_MONTHS,
    test_months: int = TEST_MONTHS,
    step_months: int = STEP_MONTHS,
) -> List[Tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp]]:
    """List of (train_start, train_end, test_start, test_end) windows.

    Walks forward in `step_months` increments until the test window
    runs past the end of the data.
    """
    if not isinstance(index, pd.DatetimeIndex):
        raise TypeError("index must be a DatetimeIndex")
    if index.tz is None:
        index = index.tz_localize("UTC")
    data_start = index[0]
    data_end = index[-1] + pd.Timedelta(hours=1)

    folds = []
    fold_start = data_start
    while True:
        train_end = _months_offset(fold_start, train_months)
        test_end = _months_offset(train_end, test_months)
        if test_end > data_end:
            break
        folds.append((fold_start, train_end, train_end, test_end))
        fold_start = _months_offset(fold_start, step_months)
    return folds


def evaluate_fold(
    closes: pd.DataFrame,
    features: pd.DataFrame,
    raw_target: pd.Series,
    fold: Tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp],
    *,
    fold_idx: int,
    hold_h: int = DEFAULT_HOLD_H,
    quantile: float = DEFAULT_QUANTILE,
) -> Tuple[FoldResult, RegimeClassifierE]:
    train_start, train_end, test_start, test_end = fold

    # Train/test masks. Train right-edge is pulled back by hold_h bars to
    # avoid the forward target leaking the test region.
    train_right_edge = train_end - pd.Timedelta(hours=hold_h)
    train_mask = (closes.index >= train_start) & (closes.index < train_right_edge)
    test_mask = (closes.index >= test_start) & (closes.index < test_end)

    X_train = features.loc[train_mask, list(FEATURE_NAMES)]
    y_train_raw = raw_target.loc[train_mask]
    X_test = features.loc[test_mask, list(FEATURE_NAMES)]
    y_test_raw = raw_target.loc[test_mask]

    # Drop NaN inside each slice independently
    train_join = X_train.join(y_train_raw.rename("__raw__"), how="inner").dropna()
    if train_join.empty:
        raise RuntimeError(f"Fold {fold_idx}: train slice empty after dropna")
    test_join = X_test.join(y_test_raw.rename("__raw__"), how="inner").dropna()
    if test_join.empty:
        raise RuntimeError(f"Fold {fold_idx}: test slice empty after dropna")

    clf = RegimeClassifierE()
    clf.fit(
        train_join[list(FEATURE_NAMES)],
        train_join["__raw__"],
        quantile=quantile,
        train_end_ts=train_end.isoformat(),
    )

    # Apply the train-fold threshold to the test fold so the label is
    # comparable across folds.
    y_test_label, _ = binarize_to_loss_tail(
        test_join["__raw__"], threshold=clf.threshold,
    )
    y_test_label = y_test_label.dropna().astype(int)

    p_loss = clf.predict_proba_loss(test_join[list(FEATURE_NAMES)])
    p_loss_aligned = p_loss.reindex(y_test_label.index).dropna()
    y_aligned = y_test_label.reindex(p_loss_aligned.index)

    if y_aligned.nunique() < 2:
        auc = float("nan")
    else:
        auc = float(roc_auc_score(y_aligned.values, p_loss_aligned.values))

    return FoldResult(
        fold_idx=fold_idx,
        train_start=train_start,
        train_end=train_end,
        test_start=test_start,
        test_end=test_end,
        train_n=int(clf.train_n),
        test_n=int(len(p_loss_aligned)),
        train_pos_rate=float(clf.train_pos_rate),
        test_pos_rate=float(y_aligned.mean()) if len(y_aligned) else float("nan"),
        threshold=float(clf.threshold),
        auc=auc,
        coef=clf.coef(),
    ), clf


def write_report(
    results: List[FoldResult],
    final_clf: RegimeClassifierE,
    artifact_path: Optional[Path],
    closes: pd.DataFrame,
    *,
    out_path: Path,
) -> bool:
    aucs = [r.auc for r in results if not np.isnan(r.auc)]
    all_pass = len(aucs) == len(results) and all(a > AUC_GATE for a in aucs)
    any_pass = len(aucs) > 0 and max(aucs) > AUC_GATE
    any_fail_hard = any(a < AUC_FALLBACK_VARIATIONS_BAR for a in aucs)

    lines = []
    lines.append("# Plan E — Regime classifier (Phase 1) walk-forward report")
    lines.append("")
    lines.append(f"**Generated:** {pd.Timestamp.now(tz='UTC').isoformat()}")
    lines.append(f"**Data range:** {closes.index[0]} → {closes.index[-1]}")
    lines.append(f"**Universe:** {', '.join(closes.columns)}")
    lines.append(
        f"**Walk-forward:** train={TRAIN_MONTHS}mo / test={TEST_MONTHS}mo / "
        f"step={STEP_MONTHS}mo, {len(results)} folds, "
        f"hold_h={DEFAULT_HOLD_H} (right-edge leakage trim)"
    )
    lines.append(
        f"**Target:** binarized 24h basket forward return at q{DEFAULT_QUANTILE:.2f} "
        f"(long_n={DEFAULT_LONG_N}, short_n={DEFAULT_SHORT_N}, "
        f"signal_sign={DEFAULT_SIGNAL_SIGN})"
    )
    lines.append(f"**Features:** {', '.join(FEATURE_NAMES)}")
    lines.append("")

    lines.append("## Per-fold results")
    lines.append("")
    lines.append("| Fold | Train range | Test range | Train n | Test n | "
                 "Train q25 | Test pos rate | Test AUC |")
    lines.append("|------|-------------|------------|--------:|-------:|"
                 "-----------|--------------:|---------:|")
    for r in results:
        lines.append(
            f"| {r.fold_idx} | "
            f"{r.train_start.date()} → {r.train_end.date()} | "
            f"{r.test_start.date()} → {r.test_end.date()} | "
            f"{r.train_n} | {r.test_n} | "
            f"{r.threshold:+.4f} | "
            f"{r.test_pos_rate:.2f} | "
            f"**{r.auc:.3f}** |"
        )
    lines.append("")

    lines.append("## Feature importances (final-fold standardized coefficients)")
    lines.append("")
    lines.append("| Feature | Coefficient |")
    lines.append("|---------|------------:|")
    for name, val in final_clf.coef().sort_values(key=lambda s: s.abs(),
                                                   ascending=False).items():
        lines.append(f"| {name} | {val:+.3f} |")
    lines.append("")
    lines.append("Coefficients are on standardized features; magnitude is "
                 "comparable across rows. Positive = feature increases "
                 "P(loss-tail).")
    lines.append("")

    lines.append("## Verdict")
    lines.append("")
    if all_pass:
        lines.append(f"**PASS** — every fold AUC > {AUC_GATE:.2f}.")
        lines.append("")
        lines.append("Phase 1 classifier clears the P3 bar. Next: enable "
                     "`regime_tilt` flag in `plan-e-regime` paper config "
                     "with the saved artifact path.")
    elif any_fail_hard:
        lines.append(f"**HARD FAIL** — at least one fold AUC < "
                     f"{AUC_FALLBACK_VARIATIONS_BAR:.2f}.")
        lines.append("")
        lines.append("Per P3, this terminates Phase 1 outright. Document "
                     "in DECISIONS.md and either revise the target "
                     "definition or close the PRD.")
    elif any_pass:
        lines.append(f"**INCONCLUSIVE** — at least one fold above "
                     f"{AUC_GATE:.2f}, others below. Per Plan D step 1 "
                     f"precedent, try up to 3 variations (different "
                     f"feature sets, different hold_h, different "
                     f"quantile) before giving up.")
    else:
        lines.append(f"**FAIL** — no fold cleared {AUC_GATE:.2f}.")
        lines.append("")
        lines.append("Phase 1 is gated; the classifier is not allowed to "
                     "control execution. Consider whether the issue is "
                     "feature selection, the target definition, or the "
                     "regime hypothesis itself.")
    lines.append("")

    if artifact_path is not None:
        lines.append(f"**Final-fold artifact:** `{artifact_path.relative_to(PROJECT_ROOT)}`")
        lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines))
    return all_pass


def main() -> int:
    closes = load_universe()
    if closes.index.tz is None:
        closes.index = closes.index.tz_localize("UTC")

    print(f"Universe: {len(closes.columns)} assets")
    print(f"Range: {closes.index[0]} → {closes.index[-1]}")
    print(f"Bars: {len(closes)}")

    print("\nComputing universe-level features…")
    features = compute_features(closes)

    print("Computing strategy-aligned target…")
    raw_target = compute_basket_forward_return(closes)

    folds = build_folds(closes.index)
    if len(folds) < 2:
        print(f"\nERROR: only {len(folds)} fold(s) fit in the available "
              f"data — walk-forward needs at least 2.")
        return 2
    print(f"\nWalk-forward: {len(folds)} folds")
    for i, (a, b, c, d) in enumerate(folds, 1):
        print(f"  Fold {i}: train {a.date()} → {b.date()} "
              f"| test {c.date()} → {d.date()}")

    results: List[FoldResult] = []
    last_clf: Optional[RegimeClassifierE] = None
    for i, fold in enumerate(folds, 1):
        print(f"\n--- Fold {i}/{len(folds)} ---")
        result, clf = evaluate_fold(
            closes, features, raw_target, fold, fold_idx=i,
        )
        results.append(result)
        last_clf = clf
        print(f"  Train n={result.train_n} pos_rate={result.train_pos_rate:.2f} "
              f"threshold={result.threshold:+.4f}")
        print(f"  Test  n={result.test_n} pos_rate={result.test_pos_rate:.2f} "
              f"AUC={result.auc:.3f}")

    # Save the final-fold artifact (most recent training data) for the runner.
    artifact_path: Optional[Path] = None
    if last_clf is not None and last_clf.fitted:
        train_end = pd.Timestamp(last_clf.train_end_ts).date()
        artifact_path = (
            PROJECT_ROOT / "scripts" / "models"
            / f"regime_classifier_e_{train_end}.joblib"
        )
        last_clf.save(artifact_path)
        print(f"\nFinal-fold model saved → {artifact_path.relative_to(PROJECT_ROOT)}")

    out_path = PROJECT_ROOT / "backtest" / "results" / "REGIME-TILT-PHASE1-classifier.md"
    pass_gate = write_report(
        results, last_clf, artifact_path, closes, out_path=out_path,
    )
    print(f"\nReport: {out_path.relative_to(PROJECT_ROOT)}")

    print(f"\n== Walk-forward verdict ==")
    aucs = [r.auc for r in results]
    print(f"AUCs: {[f'{a:.3f}' for a in aucs]}")
    print(f"PASS gate (all > {AUC_GATE}): {pass_gate}")
    return 0 if pass_gate else 1


if __name__ == "__main__":
    sys.exit(main())
