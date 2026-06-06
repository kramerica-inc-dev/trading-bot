#!/usr/bin/env python3
"""Driver for the asymmetric SENTIMENT-TILT study on the cross-sectional momentum
basket (backtest/sweep/xs_sentiment_tilt.py).

Question: can a CAUSAL market-regime signal tilt the dollar-neutral book net-long
in a bull / net-short in a bear, and does a stop-loss tame reversals — WITHOUT
degrading OOS or worsening drawdown? Decisive gates are the CONTINUOUS OOS holdout
(book carried across the boundary like XS-TRIGGER) and the risk-adjusted metrics
(max-DD, Calmar, CVaR). The random-basket null + shuffle sham guard the gate.

Fixed lb=120 / rebal=5 / m=3, OKX 3.5y daily panel (the 10-asset universe), same
cost_rate as xsectional. Sweeps regime window W in {50,100} x tilt tau in
{0,0.25,0.5,0.75,1.0}; then the stop/de-risk overlays on the best tilt only.

Writes backtest/results/sweep/xs_sentiment_tilt.json. Does NOT deploy or touch git.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "backtest"))
from sweep.xsectional import load_panel, DEFAULT_ASSETS  # noqa: E402
from sweep.xs_sentiment_tilt import (  # noqa: E402
    TiltConfig, evaluate, simulate, regime_signal, _assert_no_lookahead,
    max_drawdown_pct, calmar, cvar_pct, COST_RATE, FUNDING,
)

RESULTS = PROJECT_ROOT / "backtest" / "results" / "sweep"
LB, REBAL, M = 120, 5, 3
REGIME_WS = [50, 100]
TAUS = [0.0, 0.25, 0.5, 0.75, 1.0]
REPS = 400


def _fmt(r):
    f, h = r["full"], r["holdout"]
    return (f"{f['net_pct']:>8} {f['sharpe']:>5} {f['calmar']:>6} {f['max_dd_pct']:>7} "
            f"{f['null_pct']:>5} | {h['net_pct']:>8} {h['calmar']:>6} {h['max_dd_pct']:>7} "
            f"{h['null_pct']:>5} {f['mean_net_exposure']:>6}  "
            f"{'OK' if r['sham_fails'] else 'VOID':>4}")


def main() -> int:
    panel = load_panel(DEFAULT_ASSETS)
    if panel.empty:
        print("No OKX panel — run: python -m backtest.okx_backfill --bar 1Dutc")
        return 1
    closes = panel.to_numpy()
    n = len(panel)
    cut = int(n * 0.70)
    years = n / 365.0
    print(f"panel: {panel.shape[1]} assets x {n} days ({years:.2f}y); "
          f"train {cut}d / holdout {n - cut}d; cost_rate={COST_RATE} funding/day={FUNDING}")

    # --- lookahead guard (the #1 trap) ---
    for w in REGIME_WS:
        _assert_no_lookahead(closes, w)
    print("lookahead guard PASSED for regime windows", REGIME_WS,
          "(s_t poison-test: closes[t:] cannot change s[:t])")

    # report regime composition (bull/bear day fractions, causal)
    regime_summary = {}
    for w in REGIME_WS:
        s = regime_signal(closes, w)
        sv = s[LB:]                 # only the traded region
        regime_summary[w] = {
            "bull_frac": round(float(np.mean(sv > 0)), 3),
            "bear_frac": round(float(np.mean(sv < 0)), 3),
            "neutral_frac": round(float(np.mean(sv == 0)), 3),
            "holdout_bull_frac": round(float(np.mean(s[cut:] > 0)), 3),
        }
    print("regime composition (traded region):", json.dumps(regime_summary))

    out = {
        "spec": {"lookback": LB, "rebal": REBAL, "m": M, "regime_ws": REGIME_WS,
                 "taus": TAUS, "cost_rate": COST_RATE, "funding_per_day": FUNDING,
                 "reps": REPS, "n_days": n, "n_assets": int(panel.shape[1]),
                 "years": round(years, 2), "holdout_days": n - cut,
                 "train_frac": 0.70},
        "regime_composition": regime_summary,
        "grid": {},
    }

    # --- main grid: W x tau (no overlays) ---
    print("\n===== TILT GRID (lb=120/rebal=5/m=3) — full | holdout, plus null & sham =====")
    print(f"{'W  tau':>10} {'FULLnet':>8} {'shrp':>5} {'calmar':>6} {'maxDD':>7} "
          f"{'null':>5} | {'OOSnet':>8} {'calmar':>6} {'maxDD':>7} {'null':>5} {'netexp':>6}  sham")
    best = None
    for w in REGIME_WS:
        for tau in TAUS:
            cfg = TiltConfig(lookback=LB, rebal=REBAL, m=M, regime_w=w, tau=tau)
            r = evaluate(closes, cfg, cut=cut, reps=REPS)
            out["grid"][f"W{w}_tau{tau}"] = r
            print(f"{w:>4} {tau:>5} {_fmt(r)}")
            # pick the best tilt for overlays: best OOS Calmar among tau>0 that
            # still clears the OOS null (else fall back to best OOS net).
            cand_key = (r["holdout"]["null_pct"] > 95.0, r["holdout"]["calmar"],
                        r["holdout"]["net_pct"])
            if tau > 0 and (best is None or cand_key > best[0]):
                best = (cand_key, w, tau, r)

    # baseline (tau=0) reference at each W for the deltas
    baseline = {w: out["grid"][f"W{w}_tau0.0"] for w in REGIME_WS}
    out["baseline_tau0"] = {str(w): baseline[w] for w in REGIME_WS}

    # --- overlays on the best tilt only ---
    _, bw, btau, br = best
    print(f"\nbest tilt for overlays: W={bw} tau={btau} "
          f"(OOS net {br['holdout']['net_pct']}% calmar {br['holdout']['calmar']} "
          f"null {br['holdout']['null_pct']}th)")
    print("\n===== STOP / DE-RISK OVERLAYS on the best tilt =====")
    print(f"{'overlay':>22} {'FULLnet':>8} {'calmar':>6} {'maxDD':>7} {'null':>5} | "
          f"{'OOSnet':>8} {'calmar':>6} {'maxDD':>7} {'cvar5':>7} {'null':>5}  sham")
    overlays = [
        ("none", dict(flip_neutral=False, trail_stop=False)),
        ("flip_to_neutral", dict(flip_neutral=True, trail_stop=False)),
        ("trail_10pct_arm5", dict(trail_stop=True, arm_pct=0.05, trail_pct=0.10)),
        ("trail_15pct_arm5", dict(trail_stop=True, arm_pct=0.05, trail_pct=0.15)),
        ("flip+trail_10pct", dict(flip_neutral=True, trail_stop=True,
                                  arm_pct=0.05, trail_pct=0.10)),
    ]
    out["overlays_on_best"] = {"W": bw, "tau": btau, "rows": {}}
    for label, kw in overlays:
        cfg = TiltConfig(lookback=LB, rebal=REBAL, m=M, regime_w=bw, tau=btau, **kw)
        r = evaluate(closes, cfg, cut=cut, reps=REPS)
        out["overlays_on_best"]["rows"][label] = r
        f, h = r["full"], r["holdout"]
        print(f"{label:>22} {f['net_pct']:>8} {f['calmar']:>6} {f['max_dd_pct']:>7} "
              f"{f['null_pct']:>5} | {h['net_pct']:>8} {h['calmar']:>6} {h['max_dd_pct']:>7} "
              f"{h['cvar5_pct']:>7} {h['null_pct']:>5}  {'OK' if r['sham_fails'] else 'VOID'}")

    # --- verdict: does ANY tau>0 beat tau=0 OOS AND clear the OOS null? ---
    verdict_rows = []
    for w in REGIME_WS:
        b0 = baseline[w]["holdout"]
        for tau in TAUS:
            if tau == 0.0:
                continue
            h = out["grid"][f"W{w}_tau{tau}"]["holdout"]
            beats_net = h["net_pct"] > b0["net_pct"]
            beats_calmar = h["calmar"] > b0["calmar"]
            clears_null = h["null_pct"] > 95.0
            sham_ok = out["grid"][f"W{w}_tau{tau}"]["sham_fails"]
            if beats_net and clears_null and sham_ok:
                verdict_rows.append({
                    "W": w, "tau": tau, "oos_net": h["net_pct"],
                    "oos_calmar": h["calmar"], "oos_maxdd": h["max_dd_pct"],
                    "oos_null": h["null_pct"], "beats_calmar": beats_calmar,
                    "baseline_oos_net": b0["net_pct"],
                    "baseline_oos_calmar": b0["calmar"],
                    "baseline_oos_maxdd": b0["max_dd_pct"],
                })
    out["tilts_beating_baseline_oos_and_null"] = verdict_rows
    out["verdict"] = ("TILT-BEATS-NEUTRAL-OOS" if verdict_rows
                      else "NEUTRAL-BASELINE-STAYS")

    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "xs_sentiment_tilt.json").write_text(json.dumps(out, indent=2))
    print("\n" + "=" * 78)
    print("VERDICT:", out["verdict"])
    if verdict_rows:
        for v in verdict_rows:
            print(f"  W{v['W']} tau{v['tau']}: OOS {v['oos_net']}% (vs base "
                  f"{v['baseline_oos_net']}%), calmar {v['oos_calmar']} (vs "
                  f"{v['baseline_oos_calmar']}), maxDD {v['oos_maxdd']}, null {v['oos_null']}th")
    else:
        for w in REGIME_WS:
            b0 = baseline[w]["holdout"]
            print(f"  W{w} baseline OOS: net {b0['net_pct']}% calmar {b0['calmar']} "
                  f"maxDD {b0['max_dd_pct']} null {b0['null_pct']}th — no tau>0 beats it OOS+null")
    print("  results -> backtest/results/sweep/xs_sentiment_tilt.json")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
