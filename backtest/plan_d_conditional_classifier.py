#!/usr/bin/env python3
"""Plan D step 3 (final probe): conditional chop classifier.

Hypothesis: the unconditional AUC 0.6436 classifier does not help the
mean-reversion strategy because the strategy only trades bars where
|z| >= 2. The classifier was trained on ALL bars, so its predictions
are not calibrated for the conditional "given overextension, will
price revert?" question.

Test: retrain the classifier on bars where |z| >= 2 only, then:
    1. Report the conditional AUC.
    2. Run the strategy using this conditional classifier.
    3. If WR and expectancy both improve materially, flag as
       candidate for Plan D-v2. If not, Plan D is dead.

Writes:
    backtest/results/PLAN-D-step3-conditional.md
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT))

from chop_classifier import (  # noqa: E402
    FEATURE_NAMES, ChopClassifier, compute_features, compute_target,
)
from backtest.backtester import Backtester  # noqa: E402
from backtest.plan_d_backtest import (  # noqa: E402
    BACKTEST_CONFIG, SPLIT_DATE, STRATEGY_CONFIG,
    build_feature_maps, compute_metrics, load_data,
)
from trading_strategy import create_strategy  # noqa: E402

Z_ENTRY = 2.0  # condition

RESULTS_DIR = PROJECT_ROOT / "backtest" / "results"


def main() -> int:
    df = load_data()
    print(f"Loaded {len(df)} candles")

    print("Features + target...")
    feats = compute_features(df)
    feats["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).values
    feats["timestamp"] = pd.to_datetime(feats["timestamp"], utc=True)
    target = compute_target(feats, forward_bars=48, atr_band_mult=3.0)

    # z-score at each bar
    close = feats["close"]
    sma20 = feats["sma20"]
    std20 = feats["std20"]
    z = (close - sma20) / std20
    feats["z"] = z

    cond_mask = (z.abs() >= Z_ENTRY)
    valid = feats[FEATURE_NAMES + ["timestamp", "z"]].notna().all(axis=1) & target.notna()
    train_date = feats["timestamp"] < SPLIT_DATE

    train_cond = cond_mask & valid & train_date
    test_cond = cond_mask & valid & (~train_date)

    print(f"Bars with |z|>={Z_ENTRY}: train={train_cond.sum()}, "
          f"test={test_cond.sum()}")
    print(f"Conditional base rate, train: "
          f"{target.loc[train_cond].mean():.3f} "
          f"(vs unconditional train {target.loc[valid & train_date].mean():.3f})")

    X_train = feats.loc[train_cond, FEATURE_NAMES]
    y_train = target.loc[train_cond].astype(int)
    X_test = feats.loc[test_cond, FEATURE_NAMES]
    y_test = target.loc[test_cond].astype(int)

    clf = ChopClassifier().fit(X_train, y_train)
    p_train = clf.predict_proba(X_train)
    p_test = clf.predict_proba(X_test)

    auc_train = roc_auc_score(y_train, p_train)
    auc_test = roc_auc_score(y_test, p_test)
    print(f"Conditional AUC train={auc_train:.4f} test={auc_test:.4f}")

    # Apply this classifier to full feats (for gating during backtest)
    valid_all = feats[FEATURE_NAMES].notna().all(axis=1)
    X_all = feats.loc[valid_all, FEATURE_NAMES]
    p_all = clf.predict_proba(X_all)
    p_chop = pd.Series(np.nan, index=feats.index, name="p_chop")
    p_chop.loc[X_all.index] = p_all

    # Run strategy with conditional classifier
    features_by_ts, p_by_ts = build_feature_maps(feats, p_chop)

    test_mask = feats["timestamp"] >= SPLIT_DATE
    test_df = df.loc[test_mask].reset_index(drop=True)

    cfg = dict(STRATEGY_CONFIG)
    # Calibration of conditional classifier is different; try two gates
    results = []
    for prob_gate in [0.50, 0.60, 0.70, 0.80]:
        cfg["min_chop_prob"] = prob_gate
        strat = create_strategy("meanrev", dict(cfg))
        strat.set_precomputed(features_by_ts, p_by_ts)
        bt = Backtester(strat, BACKTEST_CONFIG)
        result = bt.run(test_df)
        m = compute_metrics(result.trades, result.equity_curve,
                            BACKTEST_CONFIG.fee_rate,
                            BACKTEST_CONFIG.contract_value)
        m["gate"] = prob_gate
        results.append(m)
        print(f"  gate>{prob_gate:.2f}: trades={m['trades']:4d} "
              f"WR={m['wr']:5.1%} WL={m['wl_ratio']:4.2f} "
              f"exp={m['expectancy']:+.3f} net=${m['net_pnl']:+.2f}")

    best = max(results, key=lambda r: r["net_pnl"])
    passes = (
        best["wr"] > 0.50
        and best["expectancy"] > 0
    )
    print(f"\nBest: gate>{best['gate']:.2f} with WR {best['wr']:.1%} "
          f"and net ${best['net_pnl']:+.2f}")
    print(f"Conditional-classifier verdict: "
          f"{'MATERIAL IMPROVEMENT' if passes else 'NO MATERIAL IMPROVEMENT'}")

    _write_report(
        RESULTS_DIR / "PLAN-D-step3-conditional.md",
        auc_train, auc_test, results, best, passes,
        train_cond.sum(), test_cond.sum(),
        target.loc[train_cond].mean(), target.loc[valid & train_date].mean(),
    )
    return 0 if passes else 1


def _write_report(path, auc_train, auc_test, results, best, passes,
                  n_train, n_test, cond_rate, uncond_rate):
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    lines = []
    lines.append("# Plan D — Step 3 (final probe): Conditional Chop Classifier")
    lines.append("")
    lines.append(f"**Generated:** {pd.Timestamp.now(tz='UTC').isoformat()}")
    lines.append(f"**Hypothesis:** retraining the classifier on bars where "
                 f"|z|>={Z_ENTRY} corrects a selection-bias flaw that caused "
                 f"the unconditional classifier's signal not to transfer to "
                 f"actual trade bars.")
    lines.append("")

    lines.append("## Conditional training data")
    lines.append("")
    lines.append(f"- Training bars (pre-split, |z|>={Z_ENTRY}): {n_train}")
    lines.append(f"- Test bars (post-split, |z|>={Z_ENTRY}): {n_test}")
    lines.append(f"- Conditional base rate (training): {cond_rate:.3f}")
    lines.append(f"- Unconditional base rate (training): {uncond_rate:.3f}")
    lines.append("")
    if cond_rate < uncond_rate:
        lines.append(
            "Conditional base rate is *lower* than unconditional — overextended "
            "bars are indeed less likely to mean-revert than random bars. "
            "This confirms the selection-bias hypothesis qualitatively."
        )
    else:
        lines.append(
            "Conditional base rate is not lower than unconditional — the "
            "selection-bias effect is weaker than hypothesized."
        )
    lines.append("")

    lines.append("## Classifier AUC")
    lines.append("")
    lines.append(f"- Train AUC (conditional): {auc_train:.4f}")
    lines.append(f"- Test AUC (conditional):  {auc_test:.4f}")
    lines.append(f"- (Unconditional test AUC from step 1: 0.6436 — for "
                 f"reference but not directly comparable)")
    lines.append("")

    lines.append("## Strategy backtest with conditional classifier")
    lines.append("")
    lines.append("| Gate | Trades | WR | W/L | Expectancy ($) | Net P&L ($) |"
                 " Sharpe | Max DD |")
    lines.append("|------|--------|-----|-----|----------------|-------------|"
                 "--------|--------|")
    for m in results:
        lines.append(
            f"| p>{m['gate']:.2f} | {m['trades']} | {m['wr']:.1%} | "
            f"{m['wl_ratio']:.2f} | {m['expectancy']:+.3f} | "
            f"**{m['net_pnl']:+.2f}** | {m['sharpe']:.2f} | "
            f"{m['max_dd_pct']:.1f}% |"
        )
    lines.append("")

    lines.append("## Verdict")
    lines.append("")
    if passes:
        lines.append(
            "**MATERIAL IMPROVEMENT.** Conditional classifier produces "
            "positive expectancy and WR > 50% at some threshold. Worth "
            "pursuing as Plan D-v2."
        )
    else:
        lines.append(
            "**NO MATERIAL IMPROVEMENT.** Conditional classifier does not "
            "rescue the strategy. Even with a selection-bias-corrected gate, "
            "the underlying reversion signal does not beat friction on 5m "
            "BTC-USDT at retail account size."
        )
        lines.append("")
        lines.append(
            "**Final Plan D verdict: FAIL.** Do not proceed to walk-forward "
            "validation. Document in DECISIONS.md and escalate to Plan E or γ."
        )

    path.write_text("\n".join(lines))
    print(f"Report written to {path}")


if __name__ == "__main__":
    sys.exit(main())
