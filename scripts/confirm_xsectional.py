#!/usr/bin/env python3
"""M4 — confirm + robustness-check the cross-sectional momentum lead.

The sweep (M3) flagged cross-sectional momentum as the only candidate to clear
the random-basket null (99.8th pct, +149% / 3.5y), killed only on a borderline
IC (p=0.10). This script:

  1. Re-runs the base config with reps=1000 (the plan's confirmation step).
  2. Sweeps a NEIGHBOURHOOD grid (lookback × rebal × m) to test whether the
     null result is ROBUST (most of the neighbourhood clears) or a lone spike
     (fragile). This is a robustness check, NOT a best-config hunt — we report
     the whole distribution, and we note the multiple-testing exposure: with
     ~N configs at a 5% bar, ~0.05·N clear by chance alone.

Writes backtest/results/sweep/xsectional_confirm.json.

Deployment note: this candidate is a dollar-neutral PERP long-short → it needs
perp permission on OKX (acctLv>=2), which the M0 access probe resolves once
credentials are supplied. Confirmable offline; deployable only if perp unlocks.
"""

from __future__ import annotations

import json
import os
import sys
from itertools import product
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "backtest"))
sys.path.insert(0, str(PROJECT_ROOT))

from sweep import xsectional  # noqa: E402
from sweep.xsectional import XSConfig  # noqa: E402

RESULTS = PROJECT_ROOT / "backtest" / "results" / "sweep"

LOOKBACKS = [30, 60, 90, 120, 180]
REBALS = [5, 10, 20]
MS = [2, 3]


def main() -> int:
    base = xsectional.run(cfg=XSConfig(lookback=90, rebal=5, m=3), reps=1000, n_sham=3)
    print(f"BASE (reps=1000): {base.verdict}  null_pct={base.metrics['null_percentile']}  "
          f"net={base.metrics['net_return_pct']}%  ic={base.metrics['xs_ic_mean']} "
          f"(p={base.metrics['xs_ic_p']})  sham={base.metrics['sham_percentiles']}")

    grid = []
    for lb, rb, m in product(LOOKBACKS, REBALS, MS):
        v = xsectional.run(cfg=XSConfig(lookback=lb, rebal=rb, m=m), reps=500, n_sham=1)
        row = {
            "lookback": lb, "rebal": rb, "m": m, "verdict": v.verdict,
            "null_pct": v.metrics.get("null_percentile"),
            "net_pct": v.metrics.get("net_return_pct"),
            "sharpe": v.metrics.get("sharpe"),
            "xs_ic": v.metrics.get("xs_ic_mean"), "xs_ic_p": v.metrics.get("xs_ic_p"),
        }
        grid.append(row)
        print(f"  lb={lb:3d} rb={rb:2d} m={m}: {v.verdict:8s} null={row['null_pct']:5} "
              f"net={row['net_pct']:8}% sharpe={row['sharpe']} ic={row['xs_ic']} p={row['xs_ic_p']}")

    n = len(grid)
    cleared_null = [g for g in grid if isinstance(g["null_pct"], (int, float)) and g["null_pct"] > 95]
    ic_sig = [g for g in grid if isinstance(g["xs_ic_p"], (int, float)) and g["xs_ic_p"] < 0.05
              and isinstance(g["xs_ic"], (int, float)) and g["xs_ic"] > 0.03]
    advanced = [g for g in grid if g["verdict"] == "ADVANCE"]

    summary = {
        "base_reps1000": base.to_json(),
        "grid": grid,
        "grid_size": n,
        "cleared_null_count": len(cleared_null),
        "cleared_null_frac": round(len(cleared_null) / n, 3),
        "ic_significant_count": len(ic_sig),
        "advanced_count": len(advanced),
        "expected_false_clears_at_5pct": round(0.05 * n, 1),
        "interpretation": (
            "Null robustness = fraction of the neighbourhood clearing the random-basket "
            "null. If >> 5% (the chance rate) the edge is structurally present; if ~5% "
            "it's likely noise. ADVANCE requires IC significance too (the current gap)."
        ),
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / "xsectional_confirm.json"
    out.write_text(json.dumps(summary, indent=2))

    print("\n" + "=" * 70)
    print("CROSS-SECTIONAL MOMENTUM — CONFIRMATION SUMMARY")
    print("=" * 70)
    print(f"  base (reps=1000): null {base.metrics['null_percentile']}th pct, "
          f"net {base.metrics['net_return_pct']}%")
    print(f"  neighbourhood: {len(cleared_null)}/{n} configs clear the null "
          f"(chance rate ~{0.05*n:.1f}); {len(ic_sig)}/{n} have significant IC; "
          f"{len(advanced)} ADVANCE")
    if len(cleared_null) > 0.5 * n:
        print("  -> NULL EDGE IS ROBUST across the neighbourhood (not a single-config spike).")
    elif len(cleared_null) <= summary["expected_false_clears_at_5pct"] * 1.5:
        print("  -> fragile: clears at ~chance rate; likely noise.")
    else:
        print("  -> partial: clears in a sub-region; investigate that region honestly.")
    if advanced:
        cfgs = sorted({(g["lookback"], g["rebal"]) for g in advanced})
        print(f"  {len(advanced)} config(s) fully ADVANCE (null + significant IC) at "
              f"(lookback,rebal)={cfgs}.")
        print(f"  CAUTION (multiple testing): {n} configs scanned; ~{0.05*n:.1f} IC-"
              "significant by chance. Treat the ADVANCE as a lead to confirm on a "
              "holdout / forward paper, not a proven edge.")
    elif len(cleared_null) > 0.5 * n:
        print("  VERDICT: strong null edge, but cross-sectional IC not yet significant. "
              "Lead worth a paper instance under the lighter regime — IC gap = open risk.")
    print(f"\n  -> {out}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
