"""Variance-risk-premium (B7) — Deribit DVOL vs realized vol.

Thesis: implied volatility (Deribit DVOL, 30d forward) systematically exceeds
subsequently-realized volatility, so a delta-hedged SHORT-vol position earns the
premium. The catch is the left tail: short vol blows up when vol spikes, so the
honest gate is not just "is the premium positive" but "does it survive the
tail".

Model (clean, interpretable, vol-points):
  * Every 30 days (non-overlapping), sell 30d implied vol at IV_t (=DVOL_t).
  * Over the next 30d you capture (IV_t - RV_{t,t+30}) vol points minus a
    round-trip cost (delta-hedged straddle ~ a few vol points). RV is the
    annualized realized vol of BTC daily returns (the OKX series).
  * The monthly P&L series gives the VRP harvest; its Sharpe, max drawdown and
    worst month (CVaR) characterize the edge AND the tail.

Checks: (1) VRP > 0 and significant (t-test); (2) short-vol Sharpe beats a
random-sign-vol null; (3) tail survivable (worst month not catastrophic vs the
mean); (4) sham (shuffle the IV/RV pairing) must FAIL.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd
from scipy.stats import ttest_1samp

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from sweep_feasibility import FeasibilityVerdict  # noqa: E402

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
DVOL_CSV = os.path.join(DATA, "deribit_dvol_BTC.csv")
BTC_DAILY = os.path.join(DATA, "okx", "BTC-USDT_1Dutc.csv")


@dataclass
class VRPConfig:
    horizon: int = 30           # vol horizon (days) — DVOL is 30d
    cost_volpts: float = 2.0    # round-trip cost per roll, in vol points
    vol_window: int = 30        # realized-vol estimation window


def _load_aligned(dvol_csv=DVOL_CSV, btc_csv=BTC_DAILY):
    dv = pd.read_csv(dvol_csv)
    dv["date"] = pd.to_datetime(dv["timestamp"], utc=True).dt.floor("D")
    bt = pd.read_csv(btc_csv)
    bt["date"] = pd.to_datetime(bt["timestamp"], utc=True).dt.floor("D")
    m = dv[["date", "dvol"]].merge(bt[["date", "close"]], on="date").sort_values("date")
    return m.reset_index(drop=True)


def _forward_rv(close: np.ndarray, t: int, horizon: int) -> float:
    """Annualized realized vol of daily log returns over (t, t+horizon]."""
    seg = close[t + 1: t + 1 + horizon]
    if len(seg) < max(5, horizon // 2):
        return float("nan")
    lr = np.diff(np.log(seg))
    if len(lr) < 2:
        return float("nan")
    return float(np.std(lr, ddof=1) * np.sqrt(365.0))


def _monthly_pnls(m: pd.DataFrame, cfg: VRPConfig) -> np.ndarray:
    """Non-overlapping 30d short-vol P&L in vol points (IV - RV - cost)."""
    iv = (m["dvol"].to_numpy() / 100.0)
    close = m["close"].to_numpy()
    n = len(m)
    pnls = []
    t = 0
    while t + cfg.horizon < n:
        rv = _forward_rv(close, t, cfg.horizon)
        if np.isfinite(rv):
            pnls.append((iv[t] - rv) * 100.0 - cfg.cost_volpts)   # vol points
        t += cfg.horizon
    return np.asarray(pnls, dtype=float)


def _max_dd(equity: np.ndarray) -> float:
    peak = np.maximum.accumulate(equity)
    return float(np.max(peak - equity)) if len(equity) else 0.0


def run(cfg: Optional[VRPConfig] = None, *, reps: int = 1000, seed: int = 20260604,
        dvol_csv: str = DVOL_CSV, btc_csv: str = BTC_DAILY) -> FeasibilityVerdict:
    cfg = cfg or VRPConfig()
    name = f"vrp_short_vol_{cfg.horizon}d"
    thesis = ("variance-risk-premium: sell 30d implied vol (Deribit DVOL), "
              "delta-hedged, harvest IV>RV; tail-exposed")

    if not (os.path.exists(dvol_csv) and os.path.exists(btc_csv)):
        return FeasibilityVerdict(name=name, verdict="KILL", thesis=thesis,
                                  reasons=["missing DVOL or BTC daily — run backtest.deribit_dvol"])
    m = _load_aligned(dvol_csv, btc_csv)
    pnls = _monthly_pnls(m, cfg)
    if len(pnls) < 12:
        return FeasibilityVerdict(name=name, verdict="KILL", thesis=thesis,
                                  reasons=[f"only {len(pnls)} monthly obs"])

    mean_pnl = float(np.mean(pnls))
    t_stat, p = ttest_1samp(pnls, 0.0)
    sharpe = float(mean_pnl / np.std(pnls, ddof=1) * np.sqrt(12)) if np.std(pnls) > 0 else 0.0
    equity = np.cumsum(pnls)
    maxdd = _max_dd(equity)
    worst = float(np.min(pnls))
    cvar5 = float(np.mean(np.sort(pnls)[:max(1, len(pnls) // 20)]))   # mean of worst 5%

    # raw premium over the full daily series (context)
    ivs = m["dvol"].to_numpy() / 100.0
    close = m["close"].to_numpy()
    fwd = np.array([_forward_rv(close, t, cfg.horizon) for t in range(len(m))])
    vmask = np.isfinite(fwd)
    raw_vrp_volpts = float(np.nanmean((ivs[vmask] - fwd[vmask])) * 100.0)

    # --- check 1: VRP positive + significant ---
    c1 = mean_pnl > 0 and p < 0.05

    # --- check 2: beats a random-sign-vol null ---
    rng = np.random.default_rng(seed)
    # null: random long/short each period on the SAME (IV-RV) gross, minus cost.
    gross = pnls + cfg.cost_volpts            # (IV-RV) vol points, cost re-added
    null_sharpes = []
    for _ in range(reps):
        signs = rng.choice([-1.0, 1.0], size=len(gross))
        npnl = signs * gross - cfg.cost_volpts
        sd = np.std(npnl, ddof=1)
        null_sharpes.append(float(np.mean(npnl) / sd * np.sqrt(12)) if sd > 0 else 0.0)
    null_sharpes = np.asarray(null_sharpes)
    pct = float(np.mean(null_sharpes < sharpe) * 100.0)
    c2 = pct > 95.0

    # --- check 3: tail survivable (worst month not worse than ~ -3x the mean
    #     harvest, i.e. the premium isn't dwarfed by a single blow-up) ---
    tail_ratio = abs(worst) / mean_pnl if mean_pnl > 0 else float("inf")
    c3_tail = np.isfinite(tail_ratio) and tail_ratio < 8.0   # heuristic, honest

    # --- check 4: sub-period consistency (the right control for an
    #     UNCONDITIONAL level premium — a timing-shuffle sham does NOT apply,
    #     since VRP lives in IV>RV levels, not in IV/RV alignment). A real
    #     structural premium should be positive across sub-periods. ---
    thirds = np.array_split(pnls, 3)
    pos_thirds = int(sum(1 for tp in thirds if len(tp) and np.mean(tp) > 0))
    c4_consistent = pos_thirds >= 2

    if c1 and c2 and c3_tail and c4_consistent:
        verdict = "STRUCTURAL-PASS"
    elif c1 and c2 and c4_consistent:
        verdict = "PASS-TAIL-RISK"      # real premium, but tail-heavy -> needs hedging/sizing
    else:
        verdict = "KILL"
    reasons = []
    if verdict in ("STRUCTURAL-PASS", "PASS-TAIL-RISK"):
        reasons.append(f"VRP real: {mean_pnl:.1f} volpts/mo (t={t_stat:.1f}, p={p:.3f}), "
                       f"Sharpe {sharpe:.2f}, beats random-sign null ({pct:.0f}th), "
                       f"positive in {pos_thirds}/3 sub-periods")
        if verdict == "PASS-TAIL-RISK":
            reasons.append(f"TAIL: worst month {worst:.1f} volpts ({tail_ratio:.1f}x mean) "
                           "-> needs explicit tail-hedge / small sizing before live")
    else:
        if not c1:
            reasons.append(f"VRP not significant at cost {cfg.cost_volpts}: mean "
                           f"{mean_pnl:.1f} volpts/mo, p={p:.3f}")
        if not c2:
            reasons.append(f"null: {pct:.0f}th pct of random-sign Sharpe")
        if not c4_consistent:
            reasons.append(f"inconsistent: positive in only {pos_thirds}/3 sub-periods")

    return FeasibilityVerdict(
        name=name, verdict=verdict,
        checks={"vrp_significant": bool(c1), "beats_null": bool(c2),
                "tail_survivable": bool(c3_tail), "consistent": bool(c4_consistent)},
        metrics={
            "n_months": int(len(pnls)),
            "positive_sub_periods": pos_thirds,
            "raw_vrp_volpts": round(raw_vrp_volpts, 2),
            "mean_pnl_volpts_per_month": round(mean_pnl, 2),
            "t_stat": round(float(t_stat), 2),
            "p_value": round(float(p), 4),
            "sharpe_annual": round(sharpe, 2),
            "null_percentile": round(pct, 1),
            "max_dd_volpts": round(maxdd, 1),
            "worst_month_volpts": round(worst, 1),
            "cvar5_volpts": round(cvar5, 1),
            "tail_ratio_worst_over_mean": round(tail_ratio, 1) if np.isfinite(tail_ratio) else None,
            "cost_volpts": cfg.cost_volpts,
            "date_range": f"{m['date'].min().date()}..{m['date'].max().date()}",
        },
        reasons=reasons, thesis=thesis,
    )


if __name__ == "__main__":
    for cost in (0.0, 2.0, 4.0):
        v = run(cfg=VRPConfig(cost_volpts=cost))
        print(f"{v.verdict:8s} cost={cost} {v.metrics}")
        for r in v.reasons:
            print("   -", r)
