"""VRP DVOL-richness filter — walk-forward / out-of-sample validation.

The deepening (`docs/VRP-DEEPENING.md`) found that the short-vol edge lives in
HIGH-IV entries: corr(entry DVOL, cycle P&L) ~ +0.64, and the grind tail starts
from CALM. So the economically-motivated rule is "sell vol only when DVOL is
rich, stand aside when it is cheap" — the INVERSE of the naive "cut when vol
spikes" thesis. But the in-sample tail benefit was knife-edge (the −50 vp month
entered at DVOL 47.1; a 48% cut keeps it, 47% does not) and leave-one-out
reversed the Sharpe ranking. So the open question this module answers:

    Does the DVOL-richness filter survive a CAUSAL / out-of-sample design,
    or is it an in-sample-tuned, single-outlier artifact?

Design (all look-ahead-free):
  * Causal trailing-percentile rule: at each cycle entry t, trade full size iff
    DVOL[t] >= the p-th percentile of DVOL over the trailing `lookback` days
    (strictly < t), else `size_low`. No future data, no global threshold — the
    cutpoint adapts to the regime seen so far. This IS a walk-forward by
    construction.
  * Expanding-window optimised threshold: choose the train-Sharpe-maximising
    absolute DVOL threshold on cycles[:k], apply to cycle k. Tests whether even
    an *optimised* threshold generalises out-of-sample.
  * Subset null: does selecting the high-DVOL cycles beat selecting a RANDOM
    same-count subset? This isolates SELECTION skill from "trade fewer months".
  * Matched-tail sizing: size each variant so CVaR-5% = 10% of capital, then
    compare %/yr — the apples-to-apples economic metric (a filter trades fewer
    months but can run larger per-trade size at the same tail budget).

Everything rides the faithful engine in `vrp_replication.py`.
"""

from __future__ import annotations

import os
import sys
from typing import List, Tuple

import numpy as np
import pandas as pd
from scipy.stats import ttest_1samp

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vrp_replication import load_aligned, replicate_cycle, ReplConfig  # noqa: E402

YEAR = 12.0  # monthly cycles -> annualisation


# ---------------------------------------------------------------------------
# Per-cycle table (with entry index for causal trailing windows)
# ---------------------------------------------------------------------------

def cycles_with_index(m: pd.DataFrame, cfg: ReplConfig) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Returns (entry_idx, pnl_volpts, pnl_dollars) for non-overlapping cycles."""
    iv_all = m["dvol"].to_numpy() / 100.0
    close = m["close"].to_numpy()
    n = len(m)
    idx, pv, pd_ = [], [], []
    t = 0
    while t + cfg.horizon < n:
        seg = close[t: t + cfg.horizon + 1]
        if len(seg) == cfg.horizon + 1 and np.all(np.isfinite(seg)) and np.all(seg > 0):
            r = replicate_cycle(seg, float(iv_all[t]), cfg)
            idx.append(t); pv.append(r.pnl_volpts); pd_.append(r.pnl_dollars)
        t += cfg.horizon
    return np.asarray(idx), np.asarray(pv, float), np.asarray(pd_, float)


# ---------------------------------------------------------------------------
# Filters (all causal)
# ---------------------------------------------------------------------------

def causal_trailing_mult(m: pd.DataFrame, entry_idx: np.ndarray, *,
                         lookback: int = 365, pctl: float = 50.0,
                         size_low: float = 0.0, min_hist: int = 60) -> np.ndarray:
    """Multiplier per cycle: 1.0 iff DVOL[t] >= trailing-pctl of DVOL over
    [t-lookback, t) (strictly before entry), else size_low. Causal by design."""
    dvol = m["dvol"].to_numpy()
    out = []
    for t in entry_idx:
        lo = max(0, t - lookback)
        hist = dvol[lo:t]                       # strictly < t -> no look-ahead
        if len(hist) < min_hist:
            out.append(1.0)                     # warm-up: default full size
        else:
            thr = np.percentile(hist, pctl)
            out.append(1.0 if dvol[t] >= thr else size_low)
    return np.asarray(out, float)


def expanding_opt_threshold_mult(entry_dvol: np.ndarray, pnl_v: np.ndarray, *,
                                 min_train: int = 12, size_low: float = 0.0,
                                 grid: np.ndarray | None = None) -> np.ndarray:
    """Walk-forward: for cycle k, pick the absolute DVOL threshold that
    maximises Sharpe on cycles[:k] (train), apply to cycle k (test). Pure OOS."""
    if grid is None:
        grid = np.arange(30, 90, 2.0)
    mult = np.ones(len(entry_dvol))
    for k in range(len(entry_dvol)):
        if k < min_train:
            mult[k] = 1.0
            continue
        d_tr, p_tr = entry_dvol[:k], pnl_v[:k]
        best_thr, best_sh = None, -1e9
        for thr in grid:
            mask = d_tr >= thr
            if mask.sum() < 4:
                continue
            sh = _sharpe(np.where(mask, p_tr, 0.0))
            if sh > best_sh:
                best_sh, best_thr = sh, thr
        if best_thr is None:
            mult[k] = 1.0
        else:
            mult[k] = 1.0 if entry_dvol[k] >= best_thr else size_low
    return mult


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def _sharpe(pv: np.ndarray) -> float:
    pv = np.asarray(pv, float)
    sd = np.std(pv, ddof=1)
    return float(np.mean(pv) / sd * np.sqrt(YEAR)) if sd > 0 else 0.0


def stats(pnl_v: np.ndarray) -> dict:
    pv = np.asarray(pnl_v, float)
    if len(pv) < 6:
        return {"n": int(len(pv)), "error": "too few"}
    mean = float(np.mean(pv))
    t, p = ttest_1samp(pv, 0.0)
    k = max(1, len(pv) // 20)
    cvar5 = float(np.mean(np.sort(pv)[:k]))
    return {"n": int(len(pv)), "mean": round(mean, 2), "t": round(float(t), 2),
            "p": round(float(p), 3), "sharpe": round(_sharpe(pv), 2),
            "worst": round(float(np.min(pv)), 1), "cvar5": round(cvar5, 1)}


def matched_tail_ann(pnl_dollars: np.ndarray, *, cvar_budget_pct: float = 10.0,
                     capital: float = 100_000.0) -> dict:
    """Size so CVaR-5% monthly loss = cvar_budget_pct of capital; report %/yr."""
    pd_ = np.asarray(pnl_dollars, float)
    k = max(1, len(pd_) // 20)
    cvar5 = float(np.mean(np.sort(pd_)[:k]))
    if cvar5 >= 0:
        return {"ann_pct": None, "note": "no left tail"}
    n = capital * cvar_budget_pct / 100.0 / abs(cvar5)
    mean_monthly = float(np.mean(pd_)) * n
    return {"n_straddles": round(n, 2),
            "ann_pct": round(mean_monthly * 12.0 / capital * 100.0, 1),
            "worst_month_pct": round(float(np.min(pd_)) * n / capital * 100.0, 1)}


def matched_tail_ann_causal(pnl_dollars: np.ndarray, *, cvar_budget_pct: float = 10.0,
                            capital: float = 100_000.0, min_hist: int = 12) -> dict:
    """Causal (no look-ahead) version of matched_tail_ann: size cycle k using the
    CVaR estimated from cycles BEFORE k only (expanding window). The whole-sample
    matched_tail_ann is a backtest-wide sizing look-ahead — this is the honest,
    deployable figure (adversarial validation, 2026-06-04)."""
    pd_ = np.asarray(pnl_dollars, float)
    realized = []
    for k in range(len(pd_)):
        hist = pd_[:k]
        if len(hist) < min_hist:
            continue
        kk = max(1, len(hist) // 20)
        cvar = float(np.mean(np.sort(hist)[:kk]))
        if cvar >= 0:
            continue
        n = capital * cvar_budget_pct / 100.0 / abs(cvar)
        realized.append(pd_[k] * n)
    if not realized:
        return {"ann_pct": None, "n_obs": 0}
    return {"ann_pct": round(float(np.mean(realized)) * 12.0 / capital * 100.0, 1),
            "n_obs": len(realized)}


def subset_null_percentile(pnl_v: np.ndarray, traded_mask: np.ndarray, *,
                           reps: int = 5000, seed: int = 20260604) -> Tuple[float, float]:
    """Does the IV-selected subset's Sharpe beat random same-count subsets?
    Returns (percentile, observed_sharpe_of_traded)."""
    pv = np.asarray(pnl_v, float)
    kk = int(traded_mask.sum())
    if kk < 3 or kk >= len(pv):
        return float("nan"), float("nan")
    obs = _sharpe(pv[traded_mask.astype(bool)])
    rng = np.random.default_rng(seed)
    null = np.array([_sharpe(pv[rng.choice(len(pv), size=kk, replace=False)])
                     for _ in range(reps)])
    return float(np.mean(null < obs) * 100.0), obs


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def _entry_dvol(m: pd.DataFrame, entry_idx: np.ndarray) -> np.ndarray:
    return m["dvol"].to_numpy()[entry_idx]


def run_offset(close: np.ndarray, dvol: np.ndarray, cfg: ReplConfig, offset: int,
               *, lookback=365, pctl=50.0, size_low=0.0):
    """Per-offset causal-filter vs naked at matched tail. Returns (naked_ann,
    causal_ann, naked_sharpe, causal_sharpe) or None."""
    H = cfg.horizon
    n = len(close)
    idx, pv, pdl = [], [], []
    t = offset
    while t + H < n:
        seg = close[t:t + H + 1]
        if len(seg) == H + 1 and np.all(seg > 0):
            r = replicate_cycle(seg, float(dvol[t] / 100.0), cfg)
            idx.append(t); pv.append(r.pnl_volpts); pdl.append(r.pnl_dollars)
        t += H
    idx = np.asarray(idx); pv = np.asarray(pv); pdl = np.asarray(pdl)
    if len(pv) < 12:
        return None
    mult = []
    for tt in idx:
        lo = max(0, tt - lookback); hist = dvol[lo:tt]
        mult.append(1.0 if (len(hist) < 60 or dvol[tt] >= np.percentile(hist, pctl)) else size_low)
    mult = np.asarray(mult)
    nk = matched_tail_ann(pdl).get("ann_pct")
    cz = matched_tail_ann(pdl * mult).get("ann_pct")
    return nk, cz, _sharpe(pv), _sharpe(pv * mult)


if __name__ == "__main__":
    m = load_aligned()
    cfg = ReplConfig(hedge_cost_bps=6.0, opt_entry_spread_volpts=1.0, wing_ratio=0.0)
    idx, pv, pdl = cycles_with_index(m, cfg)
    edv = _entry_dvol(m, idx)
    dvol_series = m["dvol"].to_numpy()
    close = m["close"].to_numpy()
    print(f"VRP richness filter — {len(pv)} cycles, "
          f"entry DVOL {edv.min():.0f}..{edv.max():.0f} (median {np.median(edv):.0f})\n")

    print("== Economic context ==")
    print(f"corr(entry DVOL, cycle P&L vp) = {np.corrcoef(edv, pv)[0,1]:+.3f}")
    hi = edv >= np.median(edv)
    print(f"high-DVOL half mean = {pv[hi].mean():+.2f} vp   low-DVOL half mean = {pv[~hi].mean():+.2f} vp\n")

    print("== 1) NAKED baseline ==")
    print("  ", stats(pv), "| sized:", matched_tail_ann(pdl))

    print("\n== 2) In-sample BEST absolute threshold (OPTIMISTIC / overfit) ==")
    best = None
    for thr in np.arange(30, 80, 1.0):
        mask = edv >= thr
        if mask.sum() < 6:
            continue
        sh = _sharpe(np.where(mask, pv, 0.0))
        if best is None or sh > best[1]:
            best = (thr, sh, mask)
    thr, sh, mask = best
    pct, obs = subset_null_percentile(pv, mask)
    print(f"   best thr={thr:.0f} (trades {mask.sum()}/{len(pv)}): full-series", stats(np.where(mask, pv, 0.0)))
    print(f"   traded-only mean={pv[mask].mean():+.2f} vp | subset-null pctl={pct:.0f} (sel vs random same-count) | sized:",
          matched_tail_ann(pdl * mask))

    print("\n== 3) CAUSAL trailing-percentile filter (HONEST / walk-forward by design) ==")
    for pctl in (40.0, 50.0, 60.0):
        for slo in (0.0, 0.5):
            mult = causal_trailing_mult(m, idx, lookback=365, pctl=pctl, size_low=slo)
            traded = (mult >= 0.99)
            sub_pct, _ = subset_null_percentile(pv, traded)
            ann = matched_tail_ann(pdl * mult)
            print(f"   pctl={pctl:.0f} size_low={slo}: deploy={mult.mean():.2f} "
                  f"sharpe={_sharpe(pv*mult):+.2f} subset-null={sub_pct:4.0f} "
                  f"ann={ann.get('ann_pct')}%/yr worst={ann.get('worst_month_pct')}% "
                  f"| traded-mean={pv[traded].mean():+.2f}")

    print("\n== 4) Expanding-window OPTIMISED threshold (pure OOS) ==")
    mult = expanding_opt_threshold_mult(edv, pv, min_train=12, size_low=0.0)
    print("   ", stats(pv * mult), f"deploy={mult.mean():.2f} sharpe={_sharpe(pv*mult):+.2f} | sized:",
          matched_tail_ann(pdl * mult))

    print("\n== 5) Roll-offset robustness (causal pctl=50, size_low=0) ==")
    res = [run_offset(close, dvol_series, cfg, off) for off in range(cfg.horizon)]
    res = [r for r in res if r]
    nk = np.array([r[0] for r in res if r[0] is not None])
    cz = np.array([r[1] for r in res if r[1] is not None])
    nsh = np.array([r[2] for r in res]); csh = np.array([r[3] for r in res])
    print(f"   naked  ann%/yr: median {np.median(nk):.0f} | causal ann%/yr: median {np.median(cz):.0f}")
    print(f"   causal Sharpe > naked Sharpe in {np.mean(csh>nsh)*100:.0f}% of {len(res)} offsets "
          f"(median ΔSharpe {np.median(csh-nsh):+.2f})")
    print(f"   causal ann > naked ann in {np.mean(cz>nk)*100:.0f}% of offsets")

    print("\n== 6) Leave-one-out (drop extreme cycles) ==")
    for label, drop in (("worst (−50)", int(np.argmin(pv))), ("best (+FTX)", int(np.argmax(pv)))):
        keep = np.ones(len(pv), bool); keep[drop] = False
        mult = causal_trailing_mult(m, idx, lookback=365, pctl=50.0, size_low=0.0)[keep]
        nk = matched_tail_ann(pdl[keep]); cz = matched_tail_ann(pdl[keep] * mult)
        print(f"   drop {label:11s}: naked sharpe={_sharpe(pv[keep]):+.2f} ann={nk.get('ann_pct')}%  "
              f"causal sharpe={_sharpe(pv[keep]*mult):+.2f} ann={cz.get('ann_pct')}%")
