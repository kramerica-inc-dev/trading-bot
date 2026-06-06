#!/usr/bin/env python3
"""Driver: Plan E (cross-sectional REVERSAL) edge gate + correlation with the live
cross-sectional MOMENTUM lane.

Two decisions:
  (1) Does Plan E (REV: lb=3/rebal=1/m=3, sign=-1, k_exit=6 hysteresis) clear a
      proper cross-sectional random-basket null OUT-OF-SAMPLE? (it was never gated).
  (2) Is its daily return stream UNcorrelated with the live MOMENTUM lane
      (lb=120/rebal=5/m=3, sign=+1, no exit)?

Decision tree:
  edge ✘                                  -> RETIRE / keep-paper
  edge ✔ + low |corr|                     -> BLEND (2-sleeve neutral)
  edge ✔ + strong NEGATIVE corr           -> PICK the better one

CRITICAL trust gate: the cloned engine on the MOMENTUM config MUST reproduce the
known momentum null (~98-100th, cf. XS-BREADTH 100 / XS-TRIGGER 98.5). If not, the
clone is broken and the reversal numbers are not trustworthy -> STOP.

Both sleeves run on the SAME OKX daily ~3.5y panel (load_panel(DEFAULT_ASSETS)) and
the SAME continuous-book-carry engine. Correlation uses both daily-return series
sliced [120:] (so both are warmed up). Robustness: lb in {2,3,5} x k_exit in {3,6}.

Writes backtest/results/sweep/xs_reversal_edge.json. Does NOT deploy or touch git.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from scipy.stats import pearsonr, spearmanr

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "backtest"))
from sweep.xsectional import load_panel, DEFAULT_ASSETS  # noqa: E402
from sweep.xs_reversal import (  # noqa: E402
    RevConfig, evaluate, simulate, _assert_no_lookahead,
    _total_return_pct, _sharpe, COST_RATE, FUNDING,
)

RESULTS = PROJECT_ROOT / "backtest" / "results" / "sweep"
REPS = 400
SEED = 20260606
WARMUP = 120                 # common slice start so both sleeves are warmed up
ROLL = 60                    # rolling-correlation window (days)

# anchor reference (the known momentum result the clone must reproduce)
ANCHOR_NULL_MIN = 98.0       # cf. XS-BREADTH 100 / XS-TRIGGER 98.5

MOM = RevConfig(lookback=120, rebal=5, m=3, reversal=False, k_exit=None)
REV = RevConfig(lookback=3, rebal=1, m=3, reversal=True, k_exit=6)


def _corr_block(a: np.ndarray, b: np.ndarray) -> dict:
    if len(a) < 5 or len(b) < 5:
        return {"pearson": None, "pearson_p": None, "spearman": None, "spearman_p": None, "n": int(min(len(a), len(b)))}
    pr, pp = pearsonr(a, b)
    sr, sp = spearmanr(a, b)
    return {"pearson": round(float(pr), 4), "pearson_p": round(float(pp), 4),
            "spearman": round(float(sr), 4), "spearman_p": round(float(sp), 4),
            "n": int(len(a))}


def main() -> int:
    panel = load_panel(DEFAULT_ASSETS)
    if panel.empty:
        print("No OKX panel — run: python -m backtest.okx_backfill --bar 1Dutc")
        return 1
    closes = panel.to_numpy()
    n = len(panel)
    cut = int(n * 0.70)
    years = n / 365.0
    assert not np.isnan(closes).any(), "NaN in panel — investigate before trusting results"
    print(f"panel: {panel.shape[1]} assets x {n} days ({years:.2f}y) "
          f"[{panel.index[0].date()} -> {panel.index[-1].date()}]; "
          f"train {cut}d / holdout {n - cut}d; cost={COST_RATE} funding/day={FUNDING}")

    out = {
        "study": "plan_e_reversal_edge_and_correlation",
        "date": "2026-06-06",
        "panel": {"n_assets": int(panel.shape[1]), "n_days": n,
                  "start": str(panel.index[0].date()), "end": str(panel.index[-1].date()),
                  "years": round(years, 2), "nans": int(np.isnan(closes).sum()),
                  "train_frac": 0.70, "holdout_days": n - cut, "warmup_slice": WARMUP},
        "spec": {"MOM": {"lookback": MOM.lookback, "rebal": MOM.rebal, "m": MOM.m,
                         "reversal": MOM.reversal, "sign": MOM.sign, "k_exit": MOM.k_exit},
                 "REV": {"lookback": REV.lookback, "rebal": REV.rebal, "m": REV.m,
                         "reversal": REV.reversal, "sign": REV.sign, "k_exit": REV.k_exit},
                 "reps": REPS, "seed": SEED, "cost_rate": COST_RATE, "funding": FUNDING},
    }

    # ---- lookahead poison-test (ranking + hysteresis state) ----
    print("\n--- lookahead poison-test (closes[t:]=inf must not change port<t) ---")
    _assert_no_lookahead(closes, MOM)
    _assert_no_lookahead(closes, REV)
    print("lookahead guard PASSED for MOM (no-hysteresis) AND REV (k_exit=6 stateful)")
    out["lookahead_poison_test"] = "PASSED"

    # ---- CRITICAL: momentum-reference anchor (trust gate) ----
    print("\n===== MOMENTUM ANCHOR (trust gate) =====")
    anchor = evaluate(closes, MOM, cut=cut, reps=REPS, seed=SEED)
    out["momentum_anchor"] = anchor
    a_full = anchor["full"]
    print(f"MOM full: net={a_full['net_pct']}% sharpe={a_full['sharpe']} "
          f"null={a_full['null_pct']}th sham={anchor['sham_percentiles']} "
          f"shamFails={anchor['sham_fails']} IC={anchor['reversal_ic']} p={anchor['reversal_ic_p']}")
    anchor_ok = (a_full["null_pct"] >= ANCHOR_NULL_MIN) and anchor["sham_fails"]
    out["anchor_trustworthy"] = bool(anchor_ok)
    if not anchor_ok:
        out["verdict"] = "STOP: ENGINE-CLONE-WRONG"
        RESULTS.mkdir(parents=True, exist_ok=True)
        (RESULTS / "xs_reversal_edge.json").write_text(json.dumps(out, indent=2))
        print(f"\n*** STOP: anchor null={a_full['null_pct']}th < {ANCHOR_NULL_MIN} OR "
              "sham passed -> clone is wrong; reversal numbers NOT trusted. ***")
        return 2
    print(f"ANCHOR OK: null {a_full['null_pct']}th >= {ANCHOR_NULL_MIN} and sham fails "
          "-> clone trustworthy; reversal numbers proceed.")

    # ---- REVERSAL edge gate (Plan E) ----
    print("\n===== PLAN E REVERSAL EDGE (lb=3/rebal=1/m=3 sign=-1 k_exit=6) =====")
    rev = evaluate(closes, REV, cut=cut, reps=REPS, seed=SEED)
    out["reversal_edge"] = rev
    rf, rh = rev["full"], rev["holdout"]
    print(f"REV full   : net={rf['net_pct']}% sharpe={rf['sharpe']} calmar={rf['calmar']} "
          f"maxDD={rf['max_dd_pct']}% cvar5={rf['cvar5_pct']} null={rf['null_pct']}th")
    print(f"REV holdout: net={rh['net_pct']}% sharpe={rh['sharpe']} calmar={rh['calmar']} "
          f"maxDD={rh['max_dd_pct']}% cvar5={rh['cvar5_pct']} null={rh['null_pct']}th")
    print(f"REV zero-cost: full={rev['zero_cost']['full_net_pct']}% oos={rev['zero_cost']['oos_net_pct']}%")
    print(f"REV IC(signed)={rev['reversal_ic']} p={rev['reversal_ic_p']} "
          f"(reversal edge expects POSITIVE signed IC == negative momentum IC)")
    print(f"REV sham={rev['sham_percentiles']} shamFails={rev['sham_fails']} "
          f"n_rebal={rev['n_rebal']} mean_gross={rev['mean_gross']}")
    print(f"REV regime split @ idx {rev['regime_split']['split_idx']}: "
          f"first net={rev['regime_split']['first']['net_pct']}% "
          f"second net={rev['regime_split']['second']['net_pct']}%")

    # ---- ZERO-COST NULL: the decisive cost-trap diagnostic ----
    # The net-cost null reads 100th ONLY because random daily-rebalanced reversal
    # baskets bleed turnover even harder. To test for a GENUINE ranking edge we must
    # neutralize cost on BOTH the obs and the null. If the zero-cost obs fails its
    # OWN zero-cost null (>95), the 100th net reading is a cost artifact, not edge.
    zcfg = RevConfig(lookback=REV.lookback, rebal=REV.rebal, m=REV.m,
                     reversal=REV.reversal, k_exit=REV.k_exit, cost_rate=0.0, funding=0.0)
    zport, _, _ = simulate(closes, zcfg, selection="momentum")
    zrng = np.random.default_rng(SEED)
    zc_full_null, zc_oos_null = [], []
    for _ in range(REPS):
        zpr, _, _ = simulate(closes, zcfg, selection="random", rng=zrng)
        zc_full_null.append(_total_return_pct(zpr))
        zc_oos_null.append(_total_return_pct(zpr[cut:]))
    zc_full_null = np.array(zc_full_null)
    zc_oos_null = np.array(zc_oos_null)
    zc_full_obs = _total_return_pct(zport)
    zc_oos_obs = _total_return_pct(zport[cut:])
    zero_cost_null = {
        "full_obs_pct": round(zc_full_obs, 2),
        "full_null_pctile": round(float(np.mean(zc_full_null < zc_full_obs) * 100), 1),
        "full_null_p95": round(float(np.percentile(zc_full_null, 95)), 2),
        "oos_obs_pct": round(zc_oos_obs, 2),
        "oos_null_pctile": round(float(np.mean(zc_oos_null < zc_oos_obs) * 100), 1),
        "oos_null_p95": round(float(np.percentile(zc_oos_null, 95)), 2),
    }
    out["zero_cost_null"] = zero_cost_null
    print(f"REV ZERO-COST NULL: full obs={zero_cost_null['full_obs_pct']}% "
          f"pctile={zero_cost_null['full_null_pctile']}th (p95={zero_cost_null['full_null_p95']}) | "
          f"OOS obs={zero_cost_null['oos_obs_pct']}% pctile={zero_cost_null['oos_null_pctile']}th "
          f"(p95={zero_cost_null['oos_null_p95']})")

    # reversal edge verdict: a real edge must (a) be net-positive OOS, (b) clear the
    # OOS net-cost null, AND (c) clear its OWN zero-cost OOS null (>95) so the null
    # reading is not merely a cost-trap artifact, with the sham failing.
    rev_edge = (rh["null_pct"] > 95.0 and rev["sham_fails"] and rh["net_pct"] > 0.0
                and zero_cost_null["oos_null_pctile"] > 95.0)
    out["reversal_clears_oos_null"] = bool(rev_edge)
    out["cost_trap"] = bool(rh["net_pct"] <= 0.0 and rh["null_pct"] > 95.0)

    # ---- CORRELATION (same engine, both sleeves, slice [120:]) ----
    print("\n===== CORRELATION (MOM vs REV daily returns, slice [%d:]) =====" % WARMUP)
    mom_port, _, _ = simulate(closes, MOM, selection="momentum")
    rev_port, _, _ = simulate(closes, REV, selection="momentum")
    # both port arrays are length n-1; slice from WARMUP so both are warmed up
    mp = mom_port[WARMUP:]
    rp = rev_port[WARMUP:]
    cut_slice = cut - WARMUP        # OOS boundary within the sliced series
    full_corr = _corr_block(mp, rp)
    oos_corr = _corr_block(mp[cut_slice:], rp[cut_slice:])
    # rolling-60d Pearson
    roll = []
    for i in range(ROLL, len(mp) + 1):
        wa, wb = mp[i - ROLL:i], rp[i - ROLL:i]
        if np.std(wa) > 0 and np.std(wb) > 0:
            roll.append(float(pearsonr(wa, wb)[0]))
    roll = np.array(roll)
    rolling = {
        "window": ROLL, "n": int(len(roll)),
        "mean": round(float(np.mean(roll)), 4) if len(roll) else None,
        "min": round(float(np.min(roll)), 4) if len(roll) else None,
        "max": round(float(np.max(roll)), 4) if len(roll) else None,
        "frac_negative": round(float(np.mean(roll < 0)), 3) if len(roll) else None,
    }
    out["correlation"] = {"full": full_corr, "oos": oos_corr, "rolling_60d": rolling,
                          "mom_full_net_pct": round(_total_return_pct(mp), 2),
                          "rev_full_net_pct": round(_total_return_pct(rp), 2)}
    print(f"FULL  : pearson={full_corr['pearson']} (p={full_corr['pearson_p']}) "
          f"spearman={full_corr['spearman']} n={full_corr['n']}")
    print(f"OOS   : pearson={oos_corr['pearson']} (p={oos_corr['pearson_p']}) "
          f"spearman={oos_corr['spearman']} n={oos_corr['n']}")
    print(f"ROLL60: mean={rolling['mean']} min={rolling['min']} max={rolling['max']} "
          f"frac_neg={rolling['frac_negative']}")

    # ---- ROBUSTNESS GRID: lb in {2,3,5} x k_exit in {3,6} ----
    print("\n===== ROBUSTNESS GRID (REV, lb x k_exit) =====")
    print(f"{'lb':>3} {'kx':>3} {'FULLnet':>8} {'OOSnet':>8} {'OOSnull':>8} {'OOSmaxDD':>9} "
          f"{'OOScalmar':>10} {'IC':>7} {'sham':>4} {'nReb':>5}")
    grid = {}
    for lb in (2, 3, 5):
        for kx in (3, 6):
            cfg = RevConfig(lookback=lb, rebal=1, m=3, reversal=True, k_exit=kx)
            r = evaluate(closes, cfg, cut=cut, reps=REPS, seed=SEED)
            grid[f"lb{lb}_kx{kx}"] = r
            h = r["holdout"]
            print(f"{lb:>3} {kx:>3} {r['full']['net_pct']:>8} {h['net_pct']:>8} "
                  f"{h['null_pct']:>8} {h['max_dd_pct']:>9} {h['calmar']:>10} "
                  f"{str(r['reversal_ic']):>7} {'OK' if r['sham_fails'] else 'VOID':>4} {r['n_rebal']:>5}")
    out["robustness_grid"] = grid

    # k_exit=6 vs 3 at lb=3 (explicit STOP-condition check)
    kx6 = grid["lb3_kx6"]["holdout"]
    kx3 = grid["lb3_kx3"]["holdout"]
    out["k_exit_6_vs_3_lb3"] = {
        "kx6_oos_net": kx6["net_pct"], "kx6_oos_null": kx6["null_pct"],
        "kx3_oos_net": kx3["net_pct"], "kx3_oos_null": kx3["null_pct"],
    }

    # ---- DETERMINISM check (same seed -> identical) ----
    rev2 = evaluate(closes, REV, cut=cut, reps=REPS, seed=SEED)
    deterministic = (rev2["full"]["null_pct"] == rev["full"]["null_pct"] and
                     rev2["holdout"]["null_pct"] == rev["holdout"]["null_pct"])
    out["determinism_ok"] = bool(deterministic)
    print(f"\ndeterminism (seed={SEED}, two runs identical null): {deterministic}")

    # ---- MECHANICAL DECISION ----
    abs_full = abs(full_corr["pearson"]) if full_corr["pearson"] is not None else 0.0
    abs_oos = abs(oos_corr["pearson"]) if oos_corr["pearson"] is not None else 0.0
    strong_neg = ((full_corr["pearson"] or 0) < -0.5) or ((oos_corr["pearson"] or 0) < -0.5)
    low_corr = max(abs_full, abs_oos) < 0.3
    if not rev_edge:
        if out["cost_trap"]:
            decision = ("RETIRE / keep-paper (COST-TRAP: net-cost null 100th is an "
                        "artifact — obs LOSES money and FAILS its own zero-cost null)")
        else:
            decision = "RETIRE / keep-paper (reversal does NOT clear the OOS null)"
    elif strong_neg:
        decision = "PICK BETTER (edge present but strong negative correlation -> they fight)"
    elif low_corr:
        decision = "BLEND (edge present + low |corr| -> 2-sleeve market-neutral)"
    else:
        decision = "BLEND-WITH-CAUTION (edge present, moderate corr) — size for the overlap"
    out["decision"] = decision
    out["decision_inputs"] = {"reversal_edge": bool(rev_edge),
                              "abs_corr_full": round(abs_full, 4),
                              "abs_corr_oos": round(abs_oos, 4),
                              "strong_negative": bool(strong_neg),
                              "low_corr": bool(low_corr)}

    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "xs_reversal_edge.json").write_text(json.dumps(out, indent=2))
    print("\n" + "=" * 78)
    print("REVERSAL OOS EDGE:", "YES" if rev_edge else "NO",
          f"(OOS null {rh['null_pct']}th, sham_fails={rev['sham_fails']}, OOS net {rh['net_pct']}%)")
    print(f"CORR full pearson={full_corr['pearson']} OOS pearson={oos_corr['pearson']}")
    print("DECISION:", decision)
    print("  results -> backtest/results/sweep/xs_reversal_edge.json")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
