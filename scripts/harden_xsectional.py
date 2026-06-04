#!/usr/bin/env python3
"""M4.5 — harden the cross-sectional momentum lead before building infra.

Addresses the two open risks from the sweep (docs/SWEEP-RESULTS.md):

  A. SHORT-LEG FUNDING. The perp long-short pays/receives funding. We calibrate
     the *realistic* funding drag from the ~3 months of OKX funding we have for
     all 10 assets (the OKX public limit), applied to the strategy's actual
     momentum weights, and report its sign + annualized magnitude.
  B. FUNDING SENSITIVITY. Sweep a flat funding-headwind on the full-history
     backtest to find the breakeven drag (where the null edge disappears).
  C. OUT-OF-SAMPLE. Split the 3.5y panel into train (first 70%) / holdout (last
     30%) and re-test the null + IC on each — the holdout is data the
     lb/rebal selection never saw, so it deflates the multiple-testing risk.
  D. WALK-FORWARD STABILITY. Null percentile + IC sign across sequential
     sub-periods (is the edge regime-robust or one-period luck?).

Writes backtest/results/sweep/xsectional_harden.json.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "backtest"))
sys.path.insert(0, str(PROJECT_ROOT))

from sweep.xsectional import (  # noqa: E402
    XSConfig, DEFAULT_ASSETS, load_panel, load_funding_panel,
    _portfolio_returns, _total_return_pct, _sharpe, _cross_sectional_ic,
)

RESULTS = PROJECT_ROOT / "backtest" / "results" / "sweep"
BASE = XSConfig(lookback=90, rebal=5, m=3)
LEAD = XSConfig(lookback=120, rebal=5, m=3)


def _obs_null(closes, cfg, reps, seed, *, funding_panel=None, flat_drag=0.0):
    obs = _total_return_pct(_portfolio_returns(closes, cfg, selection="momentum",
                                               funding_panel=funding_panel,
                                               flat_drag_daily=flat_drag))
    rng = np.random.default_rng(seed)
    null = np.array([
        _total_return_pct(_portfolio_returns(closes, cfg, selection="random", rng=rng,
                                             funding_panel=funding_panel,
                                             flat_drag_daily=flat_drag))
        for _ in range(reps)
    ])
    return obs, float(np.mean(null < obs) * 100.0)


def main() -> int:
    panel = load_panel(DEFAULT_ASSETS)
    if panel.empty:
        print("No OKX panel — run backtest.okx_backfill --bar 1Dutc")
        return 1
    assets = list(panel.columns)
    closes = panel.to_numpy()
    fpanel = load_funding_panel(panel.index, assets)
    out: dict = {"n_days": len(panel), "n_assets": len(assets)}

    # --- A. realistic funding drag (only the ~3mo window has data) ---
    gross = _portfolio_returns(closes, LEAD, selection="momentum")
    net = _portfolio_returns(closes, LEAD, selection="momentum", funding_panel=fpanel)
    drag_series = gross - net                         # >0 == funding cost
    fund_days = int(np.count_nonzero(fpanel.any(axis=1)))
    active = drag_series[np.abs(drag_series) > 0]
    mean_daily = float(np.mean(active)) if len(active) else 0.0
    out["funding"] = {
        "funded_days": fund_days,
        "mean_daily_drag_bps": round(mean_daily * 1e4, 3),
        "annualized_drag_pct": round(mean_daily * 365 * 100, 2),
        "sign": ("headwind" if mean_daily > 0 else "tailwind" if mean_daily < 0 else "neutral"),
        "note": "calibrated on ~3mo OKX funding x realized momentum weights",
    }
    print(f"A. realistic funding over {fund_days}d: {out['funding']['annualized_drag_pct']}%/yr "
          f"({out['funding']['sign']}, {out['funding']['mean_daily_drag_bps']} bps/day)")

    # --- B. funding-drag sensitivity (full sample, lead cfg) ---
    realistic = max(mean_daily, 0.0)
    drags = sorted({0.0, realistic, 0.0002, 0.0005, 0.0010, 0.0020})
    sens = []
    for d in drags:
        obs, pct = _obs_null(closes, LEAD, reps=400, seed=20260604, flat_drag=d)
        sens.append({"drag_bps_day": round(d * 1e4, 2), "net_pct": round(obs, 1),
                     "null_pct": round(pct, 1)})
        print(f"B. drag {d*1e4:5.1f} bps/day -> net {obs:8.1f}%  null {pct:5.1f}th")
    out["funding_sensitivity"] = sens
    survivors = [s for s in sens if s["null_pct"] > 95]
    out["funding_breakeven_bps_day"] = (max(s["drag_bps_day"] for s in survivors)
                                        if survivors else None)

    # --- C. out-of-sample train/holdout split ---
    n = len(closes)
    cut = int(n * 0.70)
    out["oos"] = {}
    for label, cfg in (("base_90_5", BASE), ("lead_120_5", LEAD)):
        tr_c, ho_c = closes[:cut], closes[cut:]
        tr_obs, tr_pct = _obs_null(tr_c, cfg, reps=500, seed=1)
        ho_obs, ho_pct = _obs_null(ho_c, cfg, reps=500, seed=2)
        tr_ic, tr_p = _cross_sectional_ic(tr_c, cfg)
        ho_ic, ho_p = _cross_sectional_ic(ho_c, cfg)
        out["oos"][label] = {
            "train": {"net_pct": round(tr_obs, 1), "null_pct": round(tr_pct, 1),
                      "ic": round(tr_ic, 4), "ic_p": round(tr_p, 4)},
            "holdout": {"net_pct": round(ho_obs, 1), "null_pct": round(ho_pct, 1),
                        "ic": round(ho_ic, 4), "ic_p": round(ho_p, 4)},
        }
        h = out["oos"][label]["holdout"]
        print(f"C. {label}: holdout null {h['null_pct']}th, net {h['net_pct']}%, "
              f"IC {h['ic']} (p={h['ic_p']})")

    # --- D. walk-forward stability (4 sequential windows, lead cfg) ---
    wf = []
    bounds = np.linspace(0, n, 5).astype(int)
    for i in range(4):
        seg = closes[bounds[i]:bounds[i + 1]]
        if len(seg) < LEAD.lookback + 4 * LEAD.rebal:
            continue
        obs, pct = _obs_null(seg, LEAD, reps=300, seed=10 + i)
        ic, icp = _cross_sectional_ic(seg, LEAD)
        wf.append({"window": i + 1, "net_pct": round(obs, 1), "null_pct": round(pct, 1),
                   "ic": round(ic, 4) if np.isfinite(ic) else None})
        print(f"D. window {i+1}: null {pct:.0f}th, net {obs:.1f}%, IC {ic:.3f}")
    out["walk_forward"] = wf
    out["wf_null_clear_count"] = sum(1 for w in wf if w["null_pct"] > 95)

    # --- verdict ---
    ho_lead = out["oos"]["lead_120_5"]["holdout"]
    ho_clears = ho_lead["null_pct"] > 95
    ho_ic_ok = ho_lead["ic"] > 0.03 and ho_lead["ic_p"] < 0.05
    wf_robust = out["wf_null_clear_count"] >= 3
    verdict = ("HARDENED-PASS" if (ho_clears and wf_robust) else
               "OOS-WEAK" if not ho_clears else "PARTIAL")
    out["verdict"] = verdict
    out["holdout_clears_null"] = bool(ho_clears)
    out["holdout_ic_significant"] = bool(ho_ic_ok)
    out["wf_robust"] = bool(wf_robust)

    RESULTS.mkdir(parents=True, exist_ok=True)
    p = RESULTS / "xsectional_harden.json"
    p.write_text(json.dumps(out, indent=2))

    print("\n" + "=" * 70)
    print("HARDENING VERDICT:", verdict)
    print(f"  holdout (OOS) clears null: {ho_clears} | holdout IC significant: {ho_ic_ok}")
    print(f"  walk-forward null-clears: {out['wf_null_clear_count']}/4")
    print(f"  realistic funding: {out['funding']['annualized_drag_pct']}%/yr "
          f"({out['funding']['sign']}); breakeven drag: {out['funding_breakeven_bps_day']} bps/day")
    if verdict == "HARDENED-PASS":
        print("  -> Lead survives out-of-sample + funding. Promote to M5 paper.")
    elif verdict == "OOS-WEAK":
        print("  -> Holdout does NOT clear the null — the lead was likely in-sample / MT luck. "
              "Do NOT build a paper runner; revisit the signal.")
    else:
        print("  -> Mixed. Holdout clears but walk-forward shaky — paper with caution, "
              "tight kill-criteria.")
    print(f"  -> {p}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
