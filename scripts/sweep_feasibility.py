#!/usr/bin/env python3
"""Feasibility harness (M2) — the 4 cheap checks every sweep candidate faces.

Under the lighter regime (paper-only this round) we keep the cheap, high-value
filters and drop the slow/expensive ones. A single-asset, long-or-flat candidate
exposes two pure functions:

    signal_fn(df)            -> pd.Series   # the raw directional score s_t,
                                            # computed from data <= t (no lookahead)
    exposure_fn(signal, df)  -> pd.Series   # maps a signal series -> target
                                            # exposure in [0, L_max] for day t+1
                                            # PURE, so we can feed it a *shuffled*
                                            # signal for the sham control.

`evaluate(candidate, df)` then runs the four checks and returns a
`FeasibilityVerdict`:

 1. Cost-floor / net expectancy  — backtest with real costs vs zero costs;
    the candidate must be net-positive AND not a cost-trap (fee/funding drag
    < 60% of gross). This is what diagnosed the directional family's death.
 2. Random-entry null gate (NON-NEGOTIABLE) — the realized total return must
    sit ABOVE the 95th percentile of a turnover/time-in-market-matched
    random-entry null (reuses backtest/random_entry_null.py). reps=500.
 3. Signal IC — Spearman of the raw signal vs forward return must be
    |rho|>0.03, p<0.05, and in the expected direction. (Skipped for
    non-directional candidates such as a vol-targeted buy-and-hold.)
 4. Sham/shuffle control MUST FAIL — shuffle the signal, rebuild exposure,
    re-run the null a few times; if the broken-causality version also clears
    the gate, the gate is not discriminating -> verdict VOID.

Verdict: ADVANCE iff (1,2,3) pass and the sham is confirmed-failing; else KILL
(with reasons), or VOID if the sham passed.

The harness is single-asset / long-or-flat (it rides the existing daily engine).
Market-neutral cross-sectional candidates use their own module
(backtest/sweep/xsectional.py) with a lane-specific shuffled-cross-section null.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "backtest"))
sys.path.insert(0, PROJECT_ROOT)

from daily_backtester import DailyBacktester, DailyBacktestConfig, load_daily_btc  # noqa: E402
from random_entry_null import random_entry_null, percentile_of  # noqa: E402

EPS = 1e-9
# OKX spot fee-Lv1 (conservative): maker 0.08% / taker 0.10%, half-maker fills,
# 0.05% slippage. Spot has no funding, so the directional candidates run
# funding-free. Documented in docs/OKX-DATA-NOTES.md.
DEFAULT_OKX_SPOT_CFG = DailyBacktestConfig(
    initial_balance=5000.0, fee_maker=0.0008, fee_taker=0.0010,
    maker_fraction=0.5, slippage_pct=0.05, no_trade_band_pct=15.0,
    L_max=1.0, funding_series_path=None,
)


# ---------------------------------------------------------------------------
# Candidate protocol + a precomputed-exposure strategy adaptor
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    """A single-asset, long-or-flat sweep candidate.

    `signal_fn` and `exposure_fn` must be lookahead-free: the value at bar t may
    only use data up to and including bar t. `exposure_fn` must be PURE so the
    sham can feed it a shuffled signal.
    """
    name: str
    signal_fn: Callable[[pd.DataFrame], pd.Series]
    exposure_fn: Callable[[pd.Series, pd.DataFrame], pd.Series]
    ic_horizon: int = 5
    directional: bool = True
    expected_sign: int = 1          # +1: long when signal high; -1: inverse
    thesis: str = ""


class PrecomputedExposureStrategy:
    """Adapts a precomputed per-bar exposure array to the daily-engine protocol.

    exposure[t] is the target exposure for day t+1, decided at the close of day
    t (the engine calls target_exposure(history=df[:t+1]) at bar t).
    """

    def __init__(self, exposure: List[float]):
        self._exp = list(float(x) for x in exposure)

    def reset(self):
        pass

    def target_exposure(self, history: pd.DataFrame) -> float:
        t = len(history) - 1
        return self._exp[t] if 0 <= t < len(self._exp) else 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tim_and_hold(exposure: List[float]) -> tuple[float, float]:
    """Realized time-in-market fraction and mean in-run length (days)."""
    b = [e > EPS for e in exposure]
    tim = float(np.mean(b)) if b else 0.0
    runs, cur = [], 0
    for v in b:
        if v:
            cur += 1
        elif cur:
            runs.append(cur); cur = 0
    if cur:
        runs.append(cur)
    hold = float(np.mean(runs)) if runs else 1.0
    return tim, hold


def _avg_in_exposure(exposure: pd.Series) -> float:
    ins = exposure[exposure > EPS]
    return float(ins.mean()) if len(ins) else 1.0


def _signal_ic(signal: pd.Series, df: pd.DataFrame, horizon: int) -> tuple[float, float]:
    """Spearman IC of the raw signal vs the forward `horizon`-day return."""
    close = df["close"].astype(float).reset_index(drop=True)
    sig = pd.Series(np.asarray(signal, dtype=float)).reset_index(drop=True)
    fwd = close.shift(-horizon) / close - 1.0
    m = np.isfinite(sig) & np.isfinite(fwd)
    if m.sum() < 30:
        return float("nan"), float("nan")
    rho, p = spearmanr(sig[m], fwd[m])
    return float(rho), float(p)


def _backtest_roi(exposure: pd.Series, df: pd.DataFrame, cfg: DailyBacktestConfig):
    return DailyBacktester(PrecomputedExposureStrategy(exposure.tolist()), cfg).run(df)


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

@dataclass
class FeasibilityVerdict:
    name: str
    verdict: str                       # ADVANCE | KILL | VOID
    checks: Dict[str, bool] = field(default_factory=dict)
    metrics: Dict[str, Any] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    thesis: str = ""

    def to_json(self) -> Dict[str, Any]:
        return {"name": self.name, "verdict": self.verdict, "checks": self.checks,
                "metrics": self.metrics, "reasons": self.reasons, "thesis": self.thesis}


def decide_verdict(cost_ok: bool, null_ok: bool, ic_ok: bool,
                   sham_confirmed_failing: bool) -> str:
    """Pure verdict logic. VOID dominates (a non-discriminating gate makes the
    other checks meaningless); else ADVANCE iff all three substantive checks
    pass; else KILL."""
    if not sham_confirmed_failing:
        return "VOID"
    if cost_ok and null_ok and ic_ok:
        return "ADVANCE"
    return "KILL"


def evaluate(cand: Candidate, df: pd.DataFrame, *,
             cfg: Optional[DailyBacktestConfig] = None, reps: int = 500,
             n_sham: int = 3, seed: int = 20260604) -> FeasibilityVerdict:
    cfg = cfg or DEFAULT_OKX_SPOT_CFG
    df = df.reset_index(drop=True)
    sig = pd.Series(np.asarray(cand.signal_fn(df), dtype=float))
    exp = pd.Series(np.asarray(cand.exposure_fn(sig, df), dtype=float)).clip(0.0, cfg.L_max)

    res = _backtest_roi(exp, df, cfg)
    tim, hold = _tim_and_hold(exp.tolist())
    avg_in = _avg_in_exposure(exp)

    # --- check 1: cost-floor / net expectancy ---
    zcfg = replace(cfg, fee_maker=0.0, fee_taker=0.0, slippage_pct=0.0,
                   funding_series_path=None)
    gross = _backtest_roi(exp, df, zcfg)
    cost_share = ((gross.total_roi - res.total_roi) / max(abs(gross.total_roi), EPS))
    c1 = (res.total_roi > 0.0) and (cost_share < 0.60)

    # --- check 2: random-entry null gate (non-negotiable) ---
    if tim <= EPS:
        pct = float("nan"); c2 = False
    else:
        null = random_entry_null(df, tim, hold, reps=reps, config=cfg,
                                 in_exposure=avg_in, seed=seed)
        pct = percentile_of(res.total_roi, null.total_return_pct)
        c2 = pct > 95.0

    # --- check 3: signal IC ---
    if cand.directional:
        ic, ic_p = _signal_ic(sig, df, cand.ic_horizon)
        c3 = (np.isfinite(ic) and abs(ic) > 0.03 and ic_p < 0.05
              and np.sign(ic) == cand.expected_sign)
    else:
        ic, ic_p, c3 = float("nan"), float("nan"), True

    # --- check 4: sham/shuffle control MUST FAIL ---
    rng = np.random.default_rng(seed)
    sham_pcts: List[float] = []
    for k in range(n_sham):
        shuffled = pd.Series(rng.permutation(sig.to_numpy()))
        se = pd.Series(np.asarray(cand.exposure_fn(shuffled, df), dtype=float)).clip(0.0, cfg.L_max)
        s_tim, s_hold = _tim_and_hold(se.tolist())
        if s_tim <= EPS:
            sham_pcts.append(float("nan")); continue
        sres = _backtest_roi(se, df, cfg)
        snull = random_entry_null(df, s_tim, s_hold, reps=reps, config=cfg,
                                  in_exposure=_avg_in_exposure(se), seed=seed + 1 + k)
        sham_pcts.append(percentile_of(sres.total_roi, snull.total_return_pct))
    sham_passes = sum(1 for sp in sham_pcts if np.isfinite(sp) and sp > 95.0)
    sham_confirmed_failing = sham_passes < (n_sham // 2 + 1)   # majority must fail

    # --- verdict ---
    verdict = decide_verdict(c1, c2, c3, sham_confirmed_failing)
    reasons: List[str] = []
    if verdict == "VOID":
        reasons.append(f"sham passed the gate ({sham_passes}/{n_sham}) — gate not "
                       "discriminating for this candidate; re-specify")
    elif verdict == "KILL":
        if not c1:
            reasons.append(f"cost: net_roi={res.total_roi:.1f}% cost_share={cost_share:.2f} "
                           "(needs net>0 and drag<60%)")
        if not c2:
            reasons.append(f"null: {pct:.0f}th pct (needs >95 — no edge vs random entry)")
        if not c3:
            reasons.append(f"IC: rho={ic:.3f} p={ic_p:.3f} (needs |rho|>0.03, p<0.05, right sign)")

    return FeasibilityVerdict(
        name=cand.name, verdict=verdict,
        checks={"cost_floor": bool(c1), "null_gate": bool(c2),
                "signal_ic": bool(c3), "sham_fails": bool(sham_confirmed_failing)},
        metrics={
            "net_roi_pct": round(res.total_roi, 2),
            "gross_roi_pct": round(gross.total_roi, 2),
            "cost_share": round(float(cost_share), 3),
            "calmar": round(float(res.calmar_ratio), 3),
            "max_dd_pct": round(float(res.max_drawdown_pct), 2),
            "null_percentile": (round(float(pct), 1) if np.isfinite(pct) else None),
            "ic": (round(float(ic), 4) if np.isfinite(ic) else None),
            "ic_p": (round(float(ic_p), 4) if np.isfinite(ic_p) else None),
            "time_in_market": round(tim, 3),
            "mean_hold_days": round(hold, 1),
            "n_rebalances": int(getattr(res, "n_rebalances", 0)),
            "sham_percentiles": [round(float(s), 1) if np.isfinite(s) else None for s in sham_pcts],
        },
        reasons=reasons, thesis=cand.thesis,
    )


if __name__ == "__main__":
    # Quick self-run on the built-in directional candidates over OKX BTC daily.
    from sweep.directional import build_candidates  # noqa: E402
    okx_btc = os.path.join(PROJECT_ROOT, "backtest", "data", "okx", "BTC-USDT_1Dutc.csv")
    path = okx_btc if os.path.exists(okx_btc) else None
    df = load_daily_btc(path)
    for cand in build_candidates():
        v = evaluate(cand, df)
        print(f"{v.verdict:8s} {v.name:18s} {v.metrics}")
