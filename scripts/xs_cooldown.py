#!/usr/bin/env python3
"""Market-stabilisation test for the XS breaker's COOLDOWN state (2026-10-08).

The circuit breaker cuts the book off on a sharp drop and parks the runner in
`cooldown`; it auto-resumes once the MARKET (not our own equity — a flat book
can't recover) has calmed down. This module is the pure, network-free measure
of "calmed down", computed on hourly closes of the runner's own universe:

  S1 vol_ratio   stdev of hourly equal-weight-index log returns over the last
                 `window_h` hours ÷ the median of the same stat over the
                 preceding `baseline_days` non-overlapping windows
  S2 disp_ratio  mean cross-sectional stdev of hourly returns over the window ÷
                 its baseline median (a momentum crash is a DISPERSION shock —
                 this book is market-neutral)
  S3 move_ratio  |index log move over the window| ÷ median |window move| over
                 the baseline

Everything is relative to the universe's own recent history, so the same rule
works on mainnet and on testnet (no BTC in the universe, regime tag "unknown")
and needs no per-coin absolute threshold. Missing/insufficient data → NOT
stable (fail-safe: never resume blind).

Safety mechanics only: this is consulted solely while the book is already flat
in `cooldown` and decides only WHETHER to restart, never how much. It is not a
strategy overlay and has no influence in the normal state. Thresholds are
first-principles proposals, not fit on data (see hl-lanes/LANE-hl-xsectional.md).
"""

from __future__ import annotations

from typing import Dict, Optional

import numpy as np


def market_stability(hourly: Dict[str, np.ndarray], *, window_h: int = 24,
                     baseline_days: int = 30, min_coins: int = 4,
                     max_vol_ratio: float = 1.5, max_disp_ratio: float = 1.5,
                     max_move_ratio: float = 3.0) -> dict:
    """Evaluate S1-S3 on hourly closes {coin: closes oldest→newest}.

    Returns {ok, reason, vol_ratio, disp_ratio, move_ratio, n_coins, thresholds}.
    `ok` is True only when every ratio is within its threshold."""
    thresholds = {"vol": max_vol_ratio, "disp": max_disp_ratio, "move": max_move_ratio}
    out = {"ok": False, "reason": "", "vol_ratio": None, "disp_ratio": None,
           "move_ratio": None, "n_coins": 0, "thresholds": thresholds}
    w = max(2, int(window_h))
    need = w * (int(baseline_days) + 1) + 1          # closes for baseline + current window
    series = []
    for c, arr in (hourly or {}).items():
        a = np.asarray(arr, dtype=float)
        if len(a) >= need and np.all(np.isfinite(a[-need:])) and np.all(a[-need:] > 0):
            series.append(a[-need:])
    out["n_coins"] = len(series)
    if len(series) < max(2, int(min_coins)):
        out["reason"] = f"insufficient market data ({len(series)} coins with {need}h history)"
        return out
    px = np.vstack(series)                           # coins × time
    r = np.diff(np.log(px), axis=1)                  # coins × (need-1) hourly log returns
    idx = r.mean(axis=0)                             # equal-weight index return per hour
    disp = r.std(axis=0)                             # cross-sectional dispersion per hour
    n_win = len(idx) // w
    idx = idx[-n_win * w:].reshape(n_win, w)         # oldest → newest windows
    disp = disp[-n_win * w:].reshape(n_win, w)
    vol_w = idx.std(axis=1, ddof=1)
    disp_w = disp.mean(axis=1)
    move_w = np.abs(idx.sum(axis=1))
    base_vol = float(np.median(vol_w[:-1]))
    base_disp = float(np.median(disp_w[:-1]))
    base_move = float(np.median(move_w[:-1]))
    if min(base_vol, base_disp, base_move) <= 1e-12:
        out["reason"] = "degenerate baseline (flat prices)"
        return out
    vr = float(vol_w[-1] / base_vol)
    dr = float(disp_w[-1] / base_disp)
    mr = float(move_w[-1] / base_move)
    out.update(vol_ratio=round(vr, 3), disp_ratio=round(dr, 3), move_ratio=round(mr, 3))
    fails = []
    if vr > max_vol_ratio:
        fails.append(f"vol {vr:.2f}>{max_vol_ratio}")
    if dr > max_disp_ratio:
        fails.append(f"disp {dr:.2f}>{max_disp_ratio}")
    if mr > max_move_ratio:
        fails.append(f"move {mr:.2f}>{max_move_ratio}")
    out["ok"] = not fails
    out["reason"] = "stable" if not fails else "unsettled: " + ", ".join(fails)
    return out


def resumes_in_window(resume_ts: list, now_ts: float, window_days: float) -> list:
    """The auto-resume timestamps (epoch seconds) still inside the rolling window."""
    lo = now_ts - float(window_days) * 86400.0
    return [t for t in (resume_ts or []) if t >= lo]


def hard_floor_equity(hwm: Optional[float], floor_pct: float) -> Optional[float]:
    """Equity at/below which the runner goes TERMINAL. None when disabled/unknown."""
    if hwm is None or hwm <= 0 or floor_pct is None or floor_pct <= 0:
        return None
    return hwm * (1.0 - float(floor_pct))
