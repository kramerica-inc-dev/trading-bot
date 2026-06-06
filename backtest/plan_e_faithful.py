#!/usr/bin/env python3
"""Faithful 1H cross-check of Plan E — ports the LIVE runner logic verbatim.

This is the CONFIRMATORY companion to backtest/sweep/xs_reversal.py. It reuses the
EXACT live decision functions from scripts/plan_e_runner.py:
  * compute_signal : sign=-1 * log(latest / latest_minus_72h)  (72h REVERSAL)
  * rank_signals   : sort symbols by signal DESCENDING
  * select_positions : k_exit=6 band-keep hysteresis (long head / short tail)
onto the 1H OKX panel (backtest/data/<ASSET>_1H.csv), rebalancing DAILY at 00:00 UTC
(rebalance_interval_hours=24, anchor=00:00), with the same dollar-neutral equal-weight
construction and the SAME random-basket null as the daily study.

IMPORTANT — STALE-FILE TRAP: backtest/plan_e_cross_sectional.py is STALE. It uses a
24h lookback (not 72h), MOMENTUM direction (np.argsort(-signal) with NO sign flip ==
long leaders / short laggers, the OPPOSITE of the live -1 reversal), a 4h rebalance,
and NO k_exit hysteresis. Do NOT use it to characterize Plan E. This module ports the
ACTUAL live runner instead.

This 1H check is CONFIRMATORY ONLY (it covers ~1y of 1H data, far less than the 3.5y
daily gate). It can AGREE or DISAGREE with the daily approximation but NEVER overrides
the daily 3.5y null gate. We import the live functions directly so the port can't drift.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

# Import the LIVE decision logic verbatim (no re-implementation, no drift).
from plan_e_runner import (  # noqa: E402
    compute_signal, rank_signals, select_positions, Position,
)

DEFAULT_ASSETS = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT",
                  "DOGE-USDT", "ADA-USDT", "AVAX-USDT", "DOT-USDT", "LINK-USDT"]
DATA_1H = os.path.join(PROJECT_ROOT, "backtest", "data")

# Live Plan E params (cf. PlanEConfig defaults / plan_e_runner.py)
LOOKBACK_H = 72
REBAL_H = 24
ANCHOR_HOUR = 0
LONG_N = 3
SHORT_N = 3
K_EXIT = 6
SIGN = -1                 # REVERSAL (the validated live sign)
FEE_RATE = 0.0006
SLIPPAGE_RATE = 0.0005
COST_PER_SIDE = FEE_RATE + SLIPPAGE_RATE


def load_panel_1h(assets: List[str], *, data_dir: str = DATA_1H) -> pd.DataFrame:
    """Aligned 1H close panel (inner-join on common timestamps)."""
    series: Dict[str, pd.Series] = {}
    for a in assets:
        p = os.path.join(data_dir, f"{a}_1H.csv")
        if not os.path.exists(p):
            continue
        d = pd.read_csv(p)
        d["timestamp"] = pd.to_datetime(d["timestamp"], utc=True)
        d = d.sort_values("timestamp")
        series[a] = d.set_index("timestamp")["close"].astype(float)
    if not series:
        return pd.DataFrame()
    return pd.DataFrame(series).dropna()


def _weights_for(longs: List[str], shorts: List[str], cols: List[str]) -> np.ndarray:
    """Dollar-neutral equal-weight book gross-renormalized to 2.0 (m longs + m shorts)."""
    idx = {c: i for i, c in enumerate(cols)}
    w = np.zeros(len(cols))
    if longs:
        for s in longs:
            w[idx[s]] = 1.0 / len(longs)
    if shorts:
        for s in shorts:
            w[idx[s]] = -1.0 / len(shorts)
    gross = float(np.abs(w).sum())
    if gross > 1e-12:
        w *= 2.0 / gross
    return w


@dataclass
class FaithfulResult:
    daily_returns: np.ndarray         # per-bar (1H) portfolio returns over the run
    rebal_bars: List[int]            # bar indices where a rebalance fired
    net_pct: float
    sharpe_ann: float
    max_dd_pct: float
    n_rebal: int


def _max_dd_pct(port: np.ndarray) -> float:
    if len(port) == 0:
        return 0.0
    eq = np.cumprod(1.0 + port)
    peak = np.maximum.accumulate(eq)
    return float(((eq - peak) / peak).min() * 100.0)


def simulate_faithful(panel: pd.DataFrame, *, selection: str = "live",
                      rng: Optional[np.random.Generator] = None) -> FaithfulResult:
    """Bar-by-bar 1H sim. At each 00:00 UTC bar (>= warmup) it builds the live
    signal/ranking/selection, sets a fresh equal-weight dollar-neutral book and
    carries it (weights drift with realized 1H returns) until the next 00:00 fire.
    Cost = COST_PER_SIDE on |dweight| traded at each rebalance.

    selection: 'live'  -> exact live compute_signal/rank/select_positions (k_exit)
               'random'-> random dollar-neutral baskets on the same fire bars,
                          run through the SAME select_positions hysteresis (in symbol
                          space) so the null carries matched turnover dynamics.
    """
    cols = list(panel.columns)
    closes = panel.to_numpy()
    ts = panel.index
    n, k = closes.shape
    rets = closes[1:] / closes[:-1] - 1.0          # (n-1, k); row t == bar t->t+1

    w = np.zeros(k)
    out = np.empty(n - 1)
    rebal_bars: List[int] = []
    # carried "positions" in the live Position form so select_positions sees state
    current: Dict[str, Position] = {}

    for t in range(n - 1):
        cost = 0.0
        bar_ts = ts[t]
        fire = (bar_ts.hour == ANCHOR_HOUR) and (t >= LOOKBACK_H)
        if fire:
            # build a closes-history dict ending at bar t (live runner sees series
            # up to the latest bar; compute_signal uses iloc[-1] and iloc[-1-72]).
            hist = {c: panel[c].iloc[: t + 1] for c in cols}
            if selection == "live":
                signals = compute_signal(hist, LOOKBACK_H, SIGN)
                ranked = rank_signals(signals)
                new_longs, new_shorts = select_positions(
                    ranked, current, LONG_N, SHORT_N, K_EXIT)
            elif selection == "random":
                # random ranking, then SAME hysteresis selection (matched turnover)
                ranked = list(rng.permutation(cols))
                new_longs, new_shorts = select_positions(
                    ranked, current, LONG_N, SHORT_N, K_EXIT)
            else:
                raise ValueError(selection)

            nw = _weights_for(new_longs, new_shorts, cols)
            cost = float(np.abs(nw - w).sum()) * COST_PER_SIDE
            w = nw
            rebal_bars.append(t)
            # refresh carried state in live Position form for next select_positions
            px = closes[t]
            idx = {c: i for i, c in enumerate(cols)}
            current = {}
            for s in new_longs:
                current[s] = Position(symbol=s, side="long", entry_price=float(px[idx[s]]),
                                      notional=1.0, entered_ts=str(bar_ts))
            for s in new_shorts:
                current[s] = Position(symbol=s, side="short", entry_price=float(px[idx[s]]),
                                      notional=1.0, entered_ts=str(bar_ts))

        port = float((w * rets[t]).sum() - cost)
        out[t] = port
        denom = 1.0 + port
        w = w * (1.0 + rets[t]) / (denom if abs(denom) > 1e-9 else 1e-9)

    net = float((np.cumprod(1.0 + out)[-1] - 1.0) * 100.0) if len(out) else 0.0
    sd = float(np.std(out, ddof=1))
    sharpe = float(np.mean(out) / sd * np.sqrt(24 * 365)) if sd > 0 else 0.0
    return FaithfulResult(daily_returns=out, rebal_bars=rebal_bars, net_pct=round(net, 2),
                          sharpe_ann=round(sharpe, 3), max_dd_pct=round(_max_dd_pct(out), 2),
                          n_rebal=len(rebal_bars))


def run(*, reps: int = 400, seed: int = 20260606) -> Dict:
    panel = load_panel_1h(DEFAULT_ASSETS)
    if panel.empty or panel.shape[1] < 2 * LONG_N:
        return {"error": "insufficient 1H panel"}
    res = simulate_faithful(panel, selection="live")
    rng = np.random.default_rng(seed)
    null = np.array([simulate_faithful(panel, selection="random", rng=rng).net_pct
                     for _ in range(reps)])
    pct = float(np.mean(null < res.net_pct) * 100.0)
    return {
        "panel": {"n_assets": int(panel.shape[1]), "n_bars": int(len(panel)),
                  "start": str(panel.index[0]), "end": str(panel.index[-1]),
                  "approx_days": round(len(panel) / 24.0, 1)},
        "spec": {"lookback_h": LOOKBACK_H, "rebal_h": REBAL_H, "anchor_hour": ANCHOR_HOUR,
                 "long_n": LONG_N, "short_n": SHORT_N, "k_exit": K_EXIT, "sign": SIGN,
                 "cost_per_side": COST_PER_SIDE, "reps": reps, "seed": seed},
        "observed": {"net_pct": res.net_pct, "sharpe_ann": res.sharpe_ann,
                     "max_dd_pct": res.max_dd_pct, "n_rebal": res.n_rebal},
        "null": {"percentile": round(pct, 1),
                 "p95_net_pct": round(float(np.percentile(null, 95)), 2),
                 "median_net_pct": round(float(np.median(null)), 2)},
        "stale_file_warning": ("backtest/plan_e_cross_sectional.py is STALE: 24h "
                               "lookback + momentum direction (argsort(-signal), no sign "
                               "flip) + 4h rebal + no hysteresis — NOT the live Plan E."),
    }


if __name__ == "__main__":
    import json
    r = run(reps=400)
    print(json.dumps(r, indent=2, default=str))
