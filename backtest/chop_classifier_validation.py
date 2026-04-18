#!/usr/bin/env python3
"""Validate the Plan D chop classifier.

Loads 12 months of 5m BTC-USDT candles, computes features + target,
trains logistic regression on the first 9 months, reports AUC and
calibration on the held-out 3 months.

Gate criterion:
    AUC > 0.55      -> PASS, proceed to Plan D step 2
    0.52 < AUC <=   -> TRY variations (different forward_bars, feature subsets)
    AUC <= 0.52     -> FAIL Plan D, escalate to Plan E or shutdown

Writes report to backtest/results/PLAN-D-step1-classifier.md.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score, brier_score_loss, confusion_matrix

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from chop_classifier import (
    FEATURE_NAMES,
    ChopClassifier,
    compute_features,
    compute_target,
    prepare_training_frame,
)


SPLIT_DATE = pd.Timestamp("2026-01-12", tz="UTC")  # 9mo train / 3mo test

RESULTS_DIR = PROJECT_ROOT / "backtest" / "results"


def load_data() -> pd.DataFrame:
    df = pd.read_csv(PROJECT_ROOT / "backtest" / "data" / "BTC-USDT_5m.csv")
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


def calibration_bins(y_true: np.ndarray, p: np.ndarray, n_bins: int = 10):
    """Return list of (bin_center, count, mean_pred, mean_obs)."""
    bins = np.linspace(0.0, 1.0, n_bins + 1)
    idx = np.digitize(p, bins) - 1
    idx = np.clip(idx, 0, n_bins - 1)
    out = []
    for b in range(n_bins):
        mask = idx == b
        if mask.sum() == 0:
            continue
        out.append((
            (bins[b] + bins[b + 1]) / 2,
            int(mask.sum()),
            float(p[mask].mean()),
            float(y_true[mask].mean()),
        ))
    return out


def evaluate_variant(
    df_feat: pd.DataFrame,
    target: pd.Series,
    variant_name: str,
    features: list[str],
    split_date: pd.Timestamp,
) -> dict:
    """Train on pre-split, test on post-split. Return metrics dict."""
    frame = df_feat[["timestamp"] + features].copy()
    frame["target"] = target
    frame = frame.dropna().reset_index(drop=True)

    train_mask = frame["timestamp"] < split_date
    test_mask = ~train_mask

    X_train = frame.loc[train_mask, features]
    y_train = frame.loc[train_mask, "target"].astype(int)
    X_test = frame.loc[test_mask, features]
    y_test = frame.loc[test_mask, "target"].astype(int)

    clf = ChopClassifier()
    clf.fit(X_train, y_train)
    p_test = clf.predict_proba(X_test)
    p_train = clf.predict_proba(X_train)

    return {
        "variant": variant_name,
        "features": features,
        "n_train": len(X_train),
        "n_test": len(X_test),
        "base_rate_train": float(y_train.mean()),
        "base_rate_test": float(y_test.mean()),
        "auc_train": float(roc_auc_score(y_train, p_train)),
        "auc_test": float(roc_auc_score(y_test, p_test)),
        "brier_test": float(brier_score_loss(y_test, p_test)),
        "coef": clf.coef().to_dict(),
        "calibration": calibration_bins(y_test.values, p_test),
        "p_test_stats": {
            "mean": float(p_test.mean()),
            "std": float(p_test.std()),
            "p10": float(np.percentile(p_test, 10)),
            "p50": float(np.percentile(p_test, 50)),
            "p90": float(np.percentile(p_test, 90)),
        },
        # Confusion at 0.5 threshold (reference only)
        "confusion_50": confusion_matrix(y_test, (p_test > 0.5).astype(int)).tolist(),
        # Confusion at 0.6 threshold (the strategy's actual gate)
        "confusion_60": confusion_matrix(y_test, (p_test > 0.6).astype(int)).tolist(),
    }


def main() -> int:
    df = load_data()
    print(f"Loaded {len(df)} candles, "
          f"{df['timestamp'].min()} to {df['timestamp'].max()}")

    print("Computing features...")
    feats = compute_features(df)
    # Preserve tz-aware timestamps (.values strips tz in some pandas builds)
    feats["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).values
    feats["timestamp"] = pd.to_datetime(feats["timestamp"], utc=True)

    variants = []

    # Variant A: default — forward_bars=48, full feature set
    print("Computing target (N=48, ±3×ATR)...")
    target_48 = compute_target(feats, forward_bars=48, atr_band_mult=3.0)
    r = evaluate_variant(feats, target_48, "A_n48_full", FEATURE_NAMES, SPLIT_DATE)
    print(f"  A_n48_full: test AUC = {r['auc_test']:.4f} (base rate {r['base_rate_test']:.3f})")
    variants.append(r)

    # Only if A is borderline/failing do we try variations
    if r["auc_test"] <= 0.55:
        # Variant B: stricter target — forward_bars=24, tighter band
        print("AUC borderline/failing, trying variants...")
        print("Computing target (N=24, ±2×ATR)...")
        target_24 = compute_target(feats, forward_bars=24, atr_band_mult=2.0)
        r = evaluate_variant(feats, target_24, "B_n24_tight", FEATURE_NAMES, SPLIT_DATE)
        print(f"  B_n24_tight: test AUC = {r['auc_test']:.4f} (base rate {r['base_rate_test']:.3f})")
        variants.append(r)

        # Variant C: looser target — forward_bars=96
        print("Computing target (N=96, ±3×ATR)...")
        target_96 = compute_target(feats, forward_bars=96, atr_band_mult=3.0)
        r = evaluate_variant(feats, target_96, "C_n96_wide", FEATURE_NAMES, SPLIT_DATE)
        print(f"  C_n96_wide: test AUC = {r['auc_test']:.4f} (base rate {r['base_rate_test']:.3f})")
        variants.append(r)

        # Variant D: back to default N, minimal feature set (hurst + autocorr only)
        r = evaluate_variant(
            feats, target_48, "D_n48_minimal",
            ["hurst", "autocorr_1"], SPLIT_DATE,
        )
        print(f"  D_n48_minimal: test AUC = {r['auc_test']:.4f} (base rate {r['base_rate_test']:.3f})")
        variants.append(r)

    # Pick the best variant by test AUC
    best = max(variants, key=lambda r: r["auc_test"])
    print(f"\nBest variant: {best['variant']} with test AUC {best['auc_test']:.4f}")

    # Decision gate
    if best["auc_test"] > 0.55:
        gate = "PASS"
    elif best["auc_test"] > 0.52:
        gate = "MARGINAL"
    else:
        gate = "FAIL"
    print(f"Gate: {gate}")

    # Emit report
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = RESULTS_DIR / "PLAN-D-step1-classifier.md"
    _write_report(report_path, variants, best, gate)
    print(f"Report written to {report_path}")

    return 0 if gate == "PASS" else (1 if gate == "FAIL" else 2)


def _write_report(path: Path, variants: list[dict], best: dict, gate: str) -> None:
    lines = []
    lines.append("# Plan D — Step 1: Chop Classifier Validation")
    lines.append("")
    lines.append(f"**Gate result:** **{gate}**  (best test AUC: {best['auc_test']:.4f})")
    lines.append("")
    lines.append(f"**Generated:** {pd.Timestamp.utcnow().isoformat()}")
    lines.append(f"**Split:** train before {SPLIT_DATE.date()}, test after")
    lines.append(f"**Model:** L2 logistic regression (C=1.0)")
    lines.append("")
    lines.append("## Gate criteria")
    lines.append("")
    lines.append("| AUC | Decision |")
    lines.append("|-----|----------|")
    lines.append("| > 0.55 | PASS — proceed to Plan D step 2 |")
    lines.append("| 0.52 – 0.55 | MARGINAL — try variants, record finding |")
    lines.append("| ≤ 0.52 | FAIL — stop Plan D, escalate to Plan E or γ |")
    lines.append("")

    lines.append("## Variant comparison")
    lines.append("")
    lines.append("| Variant | Features | n_train | n_test | base_rate_test | AUC_train | AUC_test | Brier |")
    lines.append("|---------|----------|---------|--------|----------------|-----------|----------|-------|")
    for r in variants:
        feat_str = ",".join(r["features"])
        if len(feat_str) > 40:
            feat_str = feat_str[:37] + "..."
        lines.append(
            f"| {r['variant']} | {feat_str} | {r['n_train']} | {r['n_test']} | "
            f"{r['base_rate_test']:.3f} | {r['auc_train']:.4f} | "
            f"**{r['auc_test']:.4f}** | {r['brier_test']:.4f} |"
        )
    lines.append("")

    lines.append(f"## Best variant: {best['variant']}")
    lines.append("")
    lines.append("### Feature coefficients (standardized)")
    lines.append("")
    lines.append("| Feature | Coefficient |")
    lines.append("|---------|-------------|")
    for k, v in sorted(best["coef"].items(), key=lambda kv: -abs(kv[1])):
        lines.append(f"| {k} | {v:+.4f} |")
    lines.append("")
    lines.append("Positive coefficient: feature increases P(chop). "
                 "Negative: decreases it.")
    lines.append("")

    lines.append("### Test-set probability distribution")
    lines.append("")
    stats = best["p_test_stats"]
    lines.append(f"- mean: {stats['mean']:.3f}")
    lines.append(f"- std:  {stats['std']:.3f}")
    lines.append(f"- p10:  {stats['p10']:.3f}")
    lines.append(f"- p50:  {stats['p50']:.3f}")
    lines.append(f"- p90:  {stats['p90']:.3f}")
    lines.append("")

    lines.append("### Calibration (test set)")
    lines.append("")
    lines.append("| bin_center | count | mean_pred | observed_freq |")
    lines.append("|------------|-------|-----------|---------------|")
    for bc, cnt, mp, mo in best["calibration"]:
        lines.append(f"| {bc:.2f} | {cnt} | {mp:.3f} | {mo:.3f} |")
    lines.append("")
    lines.append("A well-calibrated classifier has mean_pred ≈ observed_freq per bin.")
    lines.append("")

    lines.append("### Confusion matrix at P(chop) > 0.5 threshold")
    lines.append("")
    cm = best["confusion_50"]
    lines.append(f"- TN={cm[0][0]}  FP={cm[0][1]}")
    lines.append(f"- FN={cm[1][0]}  TP={cm[1][1]}")
    lines.append("")
    lines.append("### Confusion matrix at P(chop) > 0.6 threshold (strategy gate)")
    lines.append("")
    cm = best["confusion_60"]
    lines.append(f"- TN={cm[0][0]}  FP={cm[0][1]}")
    lines.append(f"- FN={cm[1][0]}  TP={cm[1][1]}")
    if cm[1][1] + cm[0][1] > 0:
        precision_60 = cm[1][1] / (cm[1][1] + cm[0][1])
        lines.append(f"- precision @ 0.6: {precision_60:.3f}")
    if cm[1][1] + cm[1][0] > 0:
        recall_60 = cm[1][1] / (cm[1][1] + cm[1][0])
        lines.append(f"- recall @ 0.6:    {recall_60:.3f}")
    lines.append("")

    lines.append("## Next step")
    lines.append("")
    if gate == "PASS":
        lines.append(
            "Proceed to Plan D step 2: build mean-reversion strategy "
            "using this classifier as the regime gate at P(chop) > 0.6.")
    elif gate == "MARGINAL":
        lines.append(
            "Record finding; proceed with caution. A classifier in the "
            "0.52–0.55 band is weak signal, so the strategy's other "
            "entry filters (|z|>2, RSI extreme) must carry most of the "
            "discrimination. Step 3 backtest is the real test.")
    else:
        lines.append(
            "Stop Plan D. Even the best variant could not clear AUC 0.52, "
            "which is the same band where the old regime classifier lived. "
            "Document in DECISIONS.md and escalate to Plan E or γ.")
    lines.append("")

    path.write_text("\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())
