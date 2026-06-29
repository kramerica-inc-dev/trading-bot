#!/usr/bin/env python3
"""Causal market-regime LABELER for the XS-momentum book — DIAGNOSTIC ONLY.

Per REGIME-TAG-PLAN (2026-06-29): regime *gating* of this book is a closed dead
lane (13 overlays killed, DECISIONS.md 2026-06-10). This module does NOT gate,
size, or trade anything. It produces a transparent, DESCRIPTIVE label of the
current market state so the operator can SEE when we're in the kind of regime
where the edge historically lived (the 2023-bull) versus where it was flat —
the dominant risk the walk-forward sweep surfaced (XS-SWEEP-PLAN result).

Descriptive, not predictive: the rule is fixed and fit to NOTHING (no forward
returns, no parameters tuned on outcomes), so it sidesteps the overfit/look-ahead
that killed the predictive classifier (AUC 0.46-0.49). Causal: uses only closes
up to and including the latest bar.

Hard rule: this is read-only w.r.t. trading. Nothing here may feed _targets /
gross_exposure / the order path. The accompanying test pins that.
"""

from __future__ import annotations

from typing import Dict, Optional

import numpy as np

# Favorability per regime, GROUNDED in the per-regime decomposition on HL data
# (backtest/sweep/xs_regime_decomp_hl.json, 2026-06-29): bull_trend|high_disp is
# the powerhouse (Sharpe 2.27, +45bps/day, 39% of PnL); chop|high_disp is sharply
# negative (Sharpe -6.6, -69bps/day); the rest are modestly positive/flat. This
# is a DIAGNOSTIC reference derived from data — NOT a trading rule (gating is the
# closed dead lane). bear_trend|high_disp wasn't observed; flagged adverse by
# analogy to chop|high_disp (high dispersion without an up-trend = the danger
# bucket).
REGIME_FAVORABILITY = {
    "bull_trend|high_disp": "strong",
    "bull_trend|low_disp": "neutral",
    "chop|low_disp": "neutral",
    "bear_trend|low_disp": "neutral",
    "chop|high_disp": "adverse",
    "bear_trend|high_disp": "adverse",
}


def _trail_return(arr: np.ndarray, window: int) -> Optional[float]:
    if len(arr) <= window or arr[-1 - window] <= 0:
        return None
    return float(arr[-1] / arr[-1 - window] - 1.0)


def compute_regime(closes: Dict[str, np.ndarray], *, market: str = "BTC",
                   trend_window: int = 30, disp_lookback: int = 30,
                   bull_thr: float = 0.05, bear_thr: float = -0.05,
                   disp_hi_thr: float = 0.20) -> dict:
    """Label the CURRENT regime from daily closes (oldest->newest per coin).

    Returns {label, trend, dispersion, features, favorable, in_distribution}.
    Causal: only the latest bar's trailing windows are used. Returns
    label="unknown" (favorable=False) when there's insufficient data — a missing
    label must never be read as a trading signal.
    """
    feats: Dict[str, Optional[float]] = {}

    # 1) trend regime — market (BTC) trailing return vs its realized vol
    mkt = closes.get(market)
    btc_ret = _trail_return(mkt, trend_window) if mkt is not None else None
    feats["btc_trend_ret"] = btc_ret
    if btc_ret is None:
        return {"label": "unknown", "trend": "unknown", "dispersion": "unknown",
                "features": feats, "favorability": "unknown", "in_distribution": False}
    if btc_ret >= bull_thr:
        trend = "bull_trend"
    elif btc_ret <= bear_thr:
        trend = "bear_trend"
    else:
        trend = "chop"

    # 2) cross-sectional dispersion — spread of trailing returns across the book
    trails = [r for r in (_trail_return(a, disp_lookback) for a in closes.values()) if r is not None]
    disp = float(np.std(trails)) if len(trails) >= 3 else None
    feats["xs_dispersion"] = disp
    dispersion = "unknown" if disp is None else ("high_disp" if disp >= disp_hi_thr else "low_disp")

    # 3) breadth — fraction of the book with positive trailing return (context)
    feats["breadth_pos"] = (round(sum(1 for r in trails if r > 0) / len(trails), 3)
                            if trails else None)

    label = f"{trend}|{dispersion}"
    favorability = REGIME_FAVORABILITY.get(label, "unknown")
    return {"label": label, "trend": trend, "dispersion": dispersion,
            "features": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in feats.items()},
            "favorability": favorability,
            "in_distribution": favorability in ("strong", "neutral")}
