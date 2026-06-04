"""Cross-sectional funding carry (B3) — preliminary OKX-native feasibility.

Market-neutral structural premium: each rebalance, long the m lowest-funding
perps (you RECEIVE funding when funding is negative / low) and short the m
highest-funding perps (you receive when funding is positive). Dollar-neutral on
price; the intended PnL is the harvested funding *dispersion* across assets.

HARD DATA LIMIT (docs/OKX-DATA-NOTES.md): OKX public funding history is only
~3 months, so this is a PRELIMINARY read, not a null-gated verdict — ~3 months
gives only ~15-30 rebalances. The honest output is: (a) the gross funding
dispersion available, (b) whether funding harvest survives the price-leg noise
of an imperfectly-neutral basket, (c) a recommendation to forward-collect OKX
funding until a real null is possible. Verdict is capped at PRELIM-* (never
ADVANCE) given the window.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import List, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from sweep_feasibility import FeasibilityVerdict  # noqa: E402
from sweep.xsectional import (  # noqa: E402
    DEFAULT_ASSETS, OKX_DATA, load_panel, load_funding_panel, _total_return_pct, _sharpe,
)


@dataclass
class CarryConfig:
    funding_lookback: int = 7     # trailing-funding ranking window (days)
    rebal: int = 3                # rebalance every N days
    m: int = 3                    # long lowest-m / short highest-m by funding
    cost_rate: float = 0.0015     # per unit |Δweight|


def _funding_window(panel: pd.DataFrame, fpanel: np.ndarray):
    """Slice to the contiguous window where OKX funding actually exists."""
    has = np.abs(fpanel).sum(axis=1) > 0
    if not has.any():
        return None, None, None
    idx = np.where(has)[0]
    lo, hi = int(idx.min()), int(idx.max()) + 1
    return panel.to_numpy()[lo:hi], fpanel[lo:hi], panel.index[lo:hi]


def _backtest(closes, fpanel, fund_sig, cfg: CarryConfig, *, selection: str,
              rng: Optional[np.random.Generator] = None):
    """Returns (total daily returns, funding-only daily returns)."""
    n, k = closes.shape
    rets = closes[1:] / closes[:-1] - 1.0
    w = np.zeros(k)
    out = np.empty(n - 1)
    fout = np.empty(n - 1)
    for t in range(n - 1):
        cost = 0.0
        if t % cfg.rebal == 0 and t >= cfg.funding_lookback and np.all(np.isfinite(fund_sig[t])):
            if selection == "carry":
                order = np.argsort(fund_sig[t])         # ascending funding
            else:
                order = rng.permutation(k)
            nw = np.zeros(k)
            nw[order[:cfg.m]] = 1.0 / cfg.m             # long LOWEST funding (receive)
            nw[order[-cfg.m:]] = -1.0 / cfg.m           # short HIGHEST funding (receive)
            cost = np.abs(nw - w).sum() * cfg.cost_rate
            w = nw
        fpnl = -float(np.dot(w, fpanel[t]))             # funding collected
        fout[t] = fpnl
        out[t] = float((w * rets[t]).sum() - cost + fpnl)
    return out, fout


def run(cfg: Optional[CarryConfig] = None, *, assets: Optional[List[str]] = None,
        reps: int = 500, seed: int = 20260604, data_dir: str = OKX_DATA) -> FeasibilityVerdict:
    cfg = cfg or CarryConfig()
    assets = assets or DEFAULT_ASSETS
    name = f"funding_carry_{cfg.funding_lookback}d_top{cfg.m}"
    thesis = ("cross-sectional funding carry: long lowest-funding / short "
              "highest-funding OKX perps, harvest the funding dispersion")

    panel = load_panel(assets, data_dir=data_dir)
    if panel.empty:
        return FeasibilityVerdict(name=name, verdict="KILL", thesis=thesis,
                                  reasons=["no OKX panel"])
    fpanel_full = load_funding_panel(panel.index, list(panel.columns), data_dir=data_dir)
    closes, fpanel, dates = _funding_window(panel, fpanel_full)
    if closes is None or len(closes) < cfg.funding_lookback + 4 * cfg.rebal:
        return FeasibilityVerdict(name=name, verdict="PRELIM-NODATA", thesis=thesis,
                                  reasons=["funding window too short — forward-collect OKX funding"])

    n, k = closes.shape
    fund_sig = pd.DataFrame(fpanel).rolling(cfg.funding_lookback,
                                            min_periods=cfg.funding_lookback).mean().to_numpy()

    obs, obs_f = _backtest(closes, fpanel, fund_sig, cfg, selection="carry")
    obs_ret = _total_return_pct(obs)
    funding_harvest = _total_return_pct(obs_f)       # funding-only contribution
    price_pnl = obs_ret - funding_harvest            # residual price drift of the basket

    rng = np.random.default_rng(seed)
    null = np.array([_total_return_pct(_backtest(closes, fpanel, fund_sig, cfg,
                                                 selection="random", rng=rng)[0])
                     for _ in range(reps)])
    pct = float(np.mean(null < obs_ret) * 100.0)

    # gross funding dispersion currently available (annualized spread of per-asset means)
    daily_means = np.nanmean(fpanel, axis=0)         # mean daily funding per asset
    spread_ann = float((np.nanmax(daily_means) - np.nanmin(daily_means)) * 365 * 100)

    # Honest verdict: capped — never ADVANCE on a ~3mo window.
    window_days = (dates[-1] - dates[0]).days
    if pct > 95 and funding_harvest > abs(price_pnl):
        verdict = "PRELIM-PROMISING"
        reasons = [f"clears 3mo null ({pct:.0f}th) and funding ({funding_harvest:.1f}%) "
                   f"dominates price noise ({price_pnl:+.1f}%) — forward-collect to null-gate"]
    elif funding_harvest > abs(price_pnl) and funding_harvest > 0:
        verdict = "PRELIM-WEAK"
        reasons = [f"funding harvested ({funding_harvest:.1f}%) but doesn't clear the 3mo "
                   f"null ({pct:.0f}th) — marginal; needs longer data"]
    else:
        verdict = "PRELIM-NOEDGE"
        reasons = [f"price-leg noise ({price_pnl:+.1f}%) swamps funding harvest "
                   f"({funding_harvest:.1f}%) — basket not neutral enough on {window_days}d"]

    return FeasibilityVerdict(
        name=name, verdict=verdict,
        checks={"prelim_only": True},
        metrics={
            "window_days": int(window_days),
            "n_rebalances_approx": int((n - cfg.funding_lookback) / cfg.rebal),
            "net_return_pct": round(obs_ret, 2),
            "funding_harvest_pct": round(funding_harvest, 2),
            "price_pnl_pct": round(price_pnl, 2),
            "sharpe": round(_sharpe(obs), 3),
            "null_percentile": round(pct, 1),
            "gross_funding_spread_annual_pct": round(spread_ann, 2),
            "n_assets": int(k),
        },
        reasons=reasons, thesis=thesis,
    )


if __name__ == "__main__":
    for lb, rb in [(7, 3), (14, 5), (3, 1)]:
        v = run(cfg=CarryConfig(funding_lookback=lb, rebal=rb), reps=500)
        print(f"{v.verdict:18s} lb={lb} rb={rb} {v.metrics}")
        for r in v.reasons:
            print("   -", r)
