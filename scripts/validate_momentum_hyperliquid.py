#!/usr/bin/env python3
"""Does the cross-sectional momentum lead survive on HYPERLIQUID data?

The lead (lb=120/rebal=5/m=3) cleared the random-basket null on OKX daily data.
But OKX EU retail can't execute perps (acctLv=1). Hyperliquid CAN execute (NL
retail, no KYC/geoblock — see docs/VENUE-ACCESS-RESEARCH.md). So the gating
question before any wallet/capital: does the SAME edge clear the SAME null on
Hyperliquid's own prices + funding? This re-runs the validation on both panels
side by side.

    python -m scripts.validate_momentum_hyperliquid
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backtest"))
sys.path.insert(0, str(ROOT))

from sweep.xsectional import (  # noqa: E402
    XSConfig, run as xsec_run, load_panel, load_funding_panel,
    _portfolio_returns, _total_return_pct, _sharpe,
)

HL_ASSETS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "DOT", "LINK"]
OKX_ASSETS = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT",
              "DOGE-USDT", "ADA-USDT", "AVAX-USDT", "DOT-USDT", "LINK-USDT"]
HL_DIR = str(ROOT / "backtest" / "data" / "hyperliquid")
OKX_DIR = str(ROOT / "backtest" / "data" / "okx")
LEAD = dict(lookback=120, rebal=5, m=3)


def _funding_drag(panel, assets, data_dir, cfg):
    """Realistic annualised funding drag on the lead's actual momentum weights."""
    closes = panel.to_numpy()
    fpanel = load_funding_panel(panel.index, assets, data_dir=data_dir)
    if not np.any(fpanel):
        return None, 0
    gross = _portfolio_returns(closes, cfg, selection="momentum")
    net = _portfolio_returns(closes, cfg, selection="momentum", funding_panel=fpanel)
    drag = gross - net
    active = drag[np.abs(drag) > 0]
    funded_days = int(np.count_nonzero(fpanel.any(axis=1)))
    mean_daily = float(np.mean(active)) if len(active) else 0.0
    return mean_daily, funded_days


def main() -> int:
    print("Cross-sectional momentum LEAD (lb=120/rebal=5/m=3) — Hyperliquid vs OKX\n")
    for label, assets, data_dir, bar in (
        ("HYPERLIQUID", HL_ASSETS, HL_DIR, "1d"),
        ("OKX        ", OKX_ASSETS, OKX_DIR, "1Dutc"),
    ):
        panel = load_panel(assets, data_dir=data_dir, bar=bar)
        if panel.empty:
            print(f"{label}: no panel at {data_dir}")
            continue
        cfg = XSConfig(bar=bar, **LEAD)
        v = xsec_run(cfg=cfg, assets=assets, reps=1000, data_dir=data_dir)
        m = v.metrics
        print(f"== {label} ==  ({m['n_assets']} assets, {m['n_days']} days, "
              f"{panel.index.min().date()}..{panel.index.max().date()})")
        print(f"   VERDICT: {v.verdict}")
        print(f"   net return   : {m['net_return_pct']:+.1f}%   (gross {m['gross_return_pct']:+.1f}%, "
              f"cost_share {m['cost_share']:.2f})")
        print(f"   null %ile    : {m['null_percentile']:.1f}  (p95 null = {m['null_p95_return_pct']:+.1f}%)")
        print(f"   Sharpe       : {m['sharpe']:+.2f}")
        print(f"   x-sec IC     : {m['xs_ic_mean']} (p={m['xs_ic_p']})")
        print(f"   sham %iles   : {m['sham_percentiles']}")
        md, fd = _funding_drag(panel, assets, data_dir, cfg)
        if md is not None:
            print(f"   funding drag : {md*365*100:+.2f}%/yr ({md*1e4:+.2f} bps/day, over {fd} funded days)")
        else:
            print("   funding drag : no funding data")
        # funding sensitivity (flat headwind on the lead)
        closes = panel.to_numpy()
        rng = np.random.default_rng(20260604)
        for d in (0.0, 0.0011, 0.0020, 0.0060):
            net = _total_return_pct(_portfolio_returns(closes, cfg, selection="momentum",
                                                       flat_drag_daily=d))
            null = np.array([_total_return_pct(_portfolio_returns(closes, cfg, selection="random",
                                                                  rng=rng, flat_drag_daily=d))
                             for _ in range(300)])
            pct = float(np.mean(null < net) * 100)
            print(f"     flat drag {d*1e4:4.1f} bps/day -> net {net:+7.1f}%  null {pct:5.1f}th")
        print()
    print("Read: if HYPERLIQUID clears the null (>95th) with positive IC like OKX did,")
    print("the edge survives on the executable venue. If not, it was OKX-data-specific.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
