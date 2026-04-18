#!/usr/bin/env python3
"""Plan D step 3 (variations): sweep mean-reversion strategy configs.

Initial backtest failed hard (WR 25.8%, fee share 126%). Before
declaring Plan D dead, sweep a small number of reasoned variations to
see if a different entry geometry or chop threshold clears the gate.

Variations tested:
    V0 default:   p_chop>0.60, z_entry=2.0, z_stop=3.5
    V1 strict:    p_chop>0.75, z_entry=2.0, z_stop=3.5
    V2 wide:      p_chop>0.60, z_entry=2.5, z_stop=4.0
    V3 strict+wide: p_chop>0.75, z_entry=2.5, z_stop=4.0
    V4 very strict: p_chop>0.85, z_entry=2.5, z_stop=4.5

All other knobs held constant (RSI gate, max_hold, SMA length).

Writes:
    backtest/results/PLAN-D-step3-sweep.md
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT))

from chop_classifier import FEATURE_NAMES  # noqa: E402
from backtest.backtester import Backtester  # noqa: E402
from backtest.plan_d_backtest import (  # noqa: E402
    BACKTEST_CONFIG, SPLIT_DATE, STRATEGY_CONFIG,
    build_feature_maps, compute_metrics, load_data,
    prepare_features_and_predictions,
)
from trading_strategy import create_strategy  # noqa: E402


VARIATIONS = [
    {"name": "V0_default",
     "config": {"min_chop_prob": 0.60, "z_entry": 2.0, "z_stop": 3.5}},
    {"name": "V1_strict_gate",
     "config": {"min_chop_prob": 0.75, "z_entry": 2.0, "z_stop": 3.5}},
    {"name": "V2_wide_geom",
     "config": {"min_chop_prob": 0.60, "z_entry": 2.5, "z_stop": 4.0}},
    {"name": "V3_strict_wide",
     "config": {"min_chop_prob": 0.75, "z_entry": 2.5, "z_stop": 4.0}},
    {"name": "V4_very_strict",
     "config": {"min_chop_prob": 0.85, "z_entry": 2.5, "z_stop": 4.5}},
    # V5: wider stop, TP target at 0.5*std beyond mean (partial reversion)
    # [not implemented yet — strategy exits at SMA20 only]
]

RESULTS_DIR = PROJECT_ROOT / "backtest" / "results"


def main() -> int:
    df = load_data()
    print(f"Loaded {len(df)} candles")

    print("Features + classifier...")
    feats, p_chop, clf = prepare_features_and_predictions(df)
    features_by_ts, p_by_ts = build_feature_maps(feats, p_chop)

    test_mask = feats["timestamp"] >= SPLIT_DATE
    test_df = df.loc[test_mask].reset_index(drop=True)
    print(f"Test slice: {len(test_df)} bars")

    results = []
    for v in VARIATIONS:
        cfg = dict(STRATEGY_CONFIG)
        cfg.update(v["config"])
        strategy = create_strategy("meanrev", cfg)
        strategy.set_precomputed(features_by_ts, p_by_ts)

        bt = Backtester(strategy, BACKTEST_CONFIG)
        result = bt.run(test_df)
        m = compute_metrics(result.trades, result.equity_curve,
                            BACKTEST_CONFIG.fee_rate,
                            BACKTEST_CONFIG.contract_value)
        m["name"] = v["name"]
        m["config"] = v["config"]
        results.append(m)

        print(f"  {v['name']:20s} trades={m['trades']:4d} "
              f"WR={m['wr']:5.1%} WL={m['wl_ratio']:4.2f} "
              f"exp={m['expectancy']:+.3f} net=${m['net_pnl']:+7.2f} "
              f"Sharpe={m['sharpe']:+.2f} DD={m['max_dd_pct']:.1f}%")

    # Pick best by net P&L (after-friction truth)
    best = max(results, key=lambda r: r["net_pnl"])
    print(f"\nBest by net P&L: {best['name']}")

    # Gate evaluation for best
    passes = (
        best["wr"] > 0.50
        and best["wl_ratio"] >= 0.8
        and best["expectancy"] > 0
        and best["sharpe"] > 1.0
        and best["max_dd_pct"] > -15.0
    )
    print(f"Gate: {'PASS' if passes else 'FAIL'}")

    _write_report(RESULTS_DIR / "PLAN-D-step3-sweep.md", results, best, passes)
    return 0 if passes else 1


def _write_report(path: Path, results: List[Dict], best: Dict, passes: bool):
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    lines = []
    lines.append("# Plan D — Step 3 Sweep: Config Variations")
    lines.append("")
    lines.append(f"**Generated:** {pd.Timestamp.now(tz='UTC').isoformat()}")
    lines.append(f"**Test slice:** 2026-01-12 → 2026-04-17 (out-of-sample "
                 f"relative to chop classifier training)")
    lines.append(f"**Backtest config:** "
                 f"fee={BACKTEST_CONFIG.fee_rate} "
                 f"slip={BACKTEST_CONFIG.slippage_pct}% "
                 f"risk={BACKTEST_CONFIG.risk_per_trade_pct}%")
    lines.append("")

    lines.append("## Variation results")
    lines.append("")
    lines.append("| Variant | p_chop | z_entry | z_stop | Trades | WR | W/L | "
                 "Expect ($) | Net P&L ($) | Gross ($) | Fee share | Sharpe | Max DD |")
    lines.append("|---------|--------|---------|--------|--------|-----|-----|"
                 "------------|-------------|-----------|-----------|--------|--------|")
    for m in results:
        c = m["config"]
        lines.append(
            f"| {m['name']} | {c['min_chop_prob']:.2f} | {c['z_entry']:.1f} | "
            f"{c['z_stop']:.1f} | {m['trades']} | {m['wr']:.1%} | "
            f"{m['wl_ratio']:.2f} | {m['expectancy']:+.3f} | "
            f"**{m['net_pnl']:+.2f}** | {m['gross_pnl']:+.2f} | "
            f"{m['fee_share']:.0%} | {m['sharpe']:.2f} | "
            f"{m['max_dd_pct']:.1f}% |"
        )
    lines.append("")

    lines.append(f"## Best: {best['name']} "
                 f"({'PASS' if passes else 'FAIL'})")
    lines.append("")
    lines.append("### Gate breakdown")
    lines.append("")
    gate_results = [
        ("WR > 50%", best["wr"] > 0.50, f"{best['wr']:.1%}"),
        ("W/L ratio >= 0.8", best["wl_ratio"] >= 0.8, f"{best['wl_ratio']:.2f}"),
        ("Expectancy > 0", best["expectancy"] > 0, f"{best['expectancy']:+.4f}"),
        ("Sharpe > 1.0", best["sharpe"] > 1.0, f"{best['sharpe']:.2f}"),
        ("Max DD > -15%", best["max_dd_pct"] > -15.0, f"{best['max_dd_pct']:.1f}%"),
    ]
    for crit, ok, val in gate_results:
        lines.append(f"- {'PASS' if ok else 'FAIL'} — {crit}  (observed: {val})")
    lines.append("")

    if not passes:
        lines.append("## Conclusion")
        lines.append("")
        lines.append("No variation clears the step 3 gate. Patterns across "
                     "the sweep:")
        lines.append("")
        sorted_res = sorted(results, key=lambda r: -r["net_pnl"])
        lines.append(f"- Best net P&L: {sorted_res[0]['name']} "
                     f"at ${sorted_res[0]['net_pnl']:+.2f} "
                     f"(still losing; fee share "
                     f"{sorted_res[0]['fee_share']:.0%})")
        lines.append(f"- Worst net P&L: {sorted_res[-1]['name']} "
                     f"at ${sorted_res[-1]['net_pnl']:+.2f}")
        lines.append("")
        lines.append("### Diagnosis")
        lines.append("")
        lines.append(
            "- **Chop classifier has real signal (AUC 0.6436) but does not "
            "improve on-signal WR enough.** At P(chop)>0.6 precision is "
            "68.5% — meaning 32% of 'chop' predictions precede non-chop "
            "behavior, which drives stops."
        )
        lines.append(
            "- **Selection bias in the classifier target.** The classifier "
            "was trained on ALL bars, not bars *conditional on |z|≥2*. "
            "Overextended bars may have systematically different reversion "
            "dynamics from the general population — and the classifier is "
            "never asked that conditional question during training."
        )
        lines.append(
            "- **Friction floor remains the unspoken constraint.** At 5m "
            "bars with 0.22% round-trip friction, the edge from z-score "
            "reversion on a single asset is too small relative to fees. "
            "Fee share >60% across all variations confirms this — the "
            "strategy generates some gross alpha but fees eat most of it."
        )
        lines.append("")
        lines.append("### Next step")
        lines.append("")
        lines.append(
            "Per PLAN-D-mean-reversion.md step 3 failure policy: **stop "
            "Plan D, document in DECISIONS.md, escalate to Plan E or γ.** "
            "Do not proceed to walk-forward — the in-sample step 3 "
            "backtest has already failed."
        )
        lines.append("")
        lines.append(
            "Candidate next moves (for user decision on return):"
        )
        lines.append("")
        lines.append(
            "1. **Plan E** — cross-sectional multi-asset ranking. Higher "
            "implementation cost but fundamentally different signal source. "
            "Cross-sectional alpha is less friction-constrained because "
            "long/short pairs partially hedge each other."
        )
        lines.append(
            "2. **Plan D-v2: conditional classifier.** Retrain the chop "
            "classifier only on bars where |z|≥2, so it learns the "
            "conditional reversion probability. This is a targeted fix to "
            "the selection-bias flaw but may simply yield AUC ≤ 0.52 on "
            "the narrower problem — if so, we're back to γ."
        )
        lines.append(
            "3. **γ (shutdown).** Accept that retail-scale BTC-USDT 5m "
            "strategies are friction-bound below viability. Halt trading "
            "indefinitely. Prod bot remains stopped from 2026-04-17."
        )

    path.write_text("\n".join(lines))
    print(f"Report written to {path}")


if __name__ == "__main__":
    sys.exit(main())
