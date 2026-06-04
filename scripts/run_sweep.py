#!/usr/bin/env python3
"""Broad-sweep fan-out (M3) — run every candidate through the feasibility gate.

Runs the single-asset directional candidates (on the OKX BTC daily series) plus
the market-neutral cross-sectional candidate (on the OKX 10-asset panel), ranks
the verdicts, and writes backtest/results/sweep/sweep_verdicts.json.

NO-GO is a valid, cheap result: if nothing ADVANCEs we document it and stop —
we do not force a build.

What this wave does NOT cover (logged, not silently dropped):
  * Carry / funding-timing (B1/B2/B3): OKX public funding is only ~3 months and
    BloFin funding is not a valid OKX proxy (docs/OKX-DATA-NOTES.md) → carry
    feasibility on OKX is data-limited; collect OKX funding forward first.
  * Cross-venue basis (B6): needs Bitvavo/Kraken adapters + synchronized books.
  * Variance-risk-premium (B7): needs an options client (OKX has none in repo).
  * Maker-rebate MM (B10): only honest via live paper-fill measurement.

Usage:
    python -m scripts.run_sweep [--reps 500]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "backtest"))
sys.path.insert(0, str(PROJECT_ROOT))

from daily_backtester import load_daily_btc  # noqa: E402
from sweep_feasibility import evaluate  # noqa: E402
from sweep.directional import build_candidates  # noqa: E402
from sweep import xsectional  # noqa: E402

OKX_BTC_DAILY = PROJECT_ROOT / "backtest" / "data" / "okx" / "BTC-USDT_1Dutc.csv"
RESULTS = PROJECT_ROOT / "backtest" / "results" / "sweep"

NOT_COVERED = {
    "B1/B2/B3 carry/funding": "OKX public funding ~3mo + BloFin not a valid OKX proxy "
                              "(docs/OKX-DATA-NOTES.md) — data-limited, collect OKX funding forward",
    "B6 cross-venue basis": "needs Bitvavo/Kraken adapters + synchronized order books",
    "B7 variance-risk-premium": "needs an options client (OKX options not in repo)",
    "B10 maker-rebate MM": "only honest via live paper-fill measurement",
}


def _rank_key(v):
    order = {"ADVANCE": 0, "KILL": 1, "VOID": 2}
    pct = v.metrics.get("null_percentile")
    pct = pct if isinstance(pct, (int, float)) else -1
    return (order.get(v.verdict, 9), -pct)


def main() -> int:
    ap = argparse.ArgumentParser(description="Broad-sweep fan-out (M3)")
    ap.add_argument("--reps", type=int, default=500)
    ap.add_argument("--seed", type=int, default=20260604)
    args = ap.parse_args()

    if not OKX_BTC_DAILY.exists():
        print(f"Missing {OKX_BTC_DAILY} — run: python -m backtest.okx_backfill --bar 1Dutc")
        return 1

    df = load_daily_btc(str(OKX_BTC_DAILY))
    verdicts = []

    print(f"Running directional candidates on OKX BTC daily ({len(df)} bars, reps={args.reps})...")
    for cand in build_candidates():
        v = evaluate(cand, df, reps=args.reps, seed=args.seed)
        verdicts.append(v)
        print(f"  {v.verdict:8s} {v.name}")

    print("Running cross-sectional candidate on OKX 10-asset panel...")
    vx = xsectional.run(reps=args.reps, seed=args.seed)
    verdicts.append(vx)
    print(f"  {vx.verdict:8s} {vx.name}")

    verdicts.sort(key=_rank_key)

    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / "sweep_verdicts.json"
    payload = {
        "ranked": [v.to_json() for v in verdicts],
        "advanced": [v.name for v in verdicts if v.verdict == "ADVANCE"],
        "not_covered_this_wave": NOT_COVERED,
        "reps": args.reps, "seed": args.seed,
    }
    out.write_text(json.dumps(payload, indent=2))

    print("\n" + "=" * 78)
    print("SWEEP VERDICTS (ranked)")
    print("=" * 78)
    for v in verdicts:
        pct = v.metrics.get("null_percentile")
        ret = v.metrics.get("net_roi_pct", v.metrics.get("net_return_pct"))
        print(f"  {v.verdict:8s} {v.name:24s} null_pct={pct} net={ret}%")
        for r in v.reasons:
            print(f"           - {r}")
    print("-" * 78)
    adv = payload["advanced"]
    if adv:
        print(f"  ADVANCE: {adv} -> M4 (1000-rep null confirmation), then M5 paper.")
    else:
        print("  NO-GO: nothing cleared the gate this wave. Valid, cheap result — "
              "no build forced.")
    print("  Not covered this wave (logged, not dropped):")
    for k, why in NOT_COVERED.items():
        print(f"    - {k}: {why}")
    print(f"\n  verdicts -> {out}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
