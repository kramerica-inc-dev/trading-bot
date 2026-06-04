"""Single-asset, long-or-flat directional candidates (sweep wave 1).

These ride the daily engine via `sweep_feasibility.Candidate`. The directional
family (trend / momentum / mean-reversion) has already died on BloFin data;
re-running it on the fresh OKX daily series closes that loophole and exercises
the harness (cost-floor / null / IC / sham). Expect most to KILL — that is the
cheap, valid result. `vol_target_bh` is included as a non-directional benchmark
(a sized buy-and-hold; expected inside the null band).

All signal_fn/exposure_fn pairs are lookahead-free (value at t uses data <= t)
and exposure_fn is pure so the sham can feed it a shuffled signal.
"""

from __future__ import annotations

import os
import sys
from typing import List

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from sweep_feasibility import Candidate  # noqa: E402

WARMUP = 1.0   # exposure during indicator warm-up (behave like BH, the M0 convention)


# ---------------------------------------------------------------------------
# Indicator helpers (vectorized, no lookahead: value at t uses close[:t+1])
# ---------------------------------------------------------------------------

def _sma_ratio(df: pd.DataFrame, n: int) -> pd.Series:
    c = df["close"].astype(float)
    return c / c.rolling(n, min_periods=n).mean() - 1.0


def _trailing_return(df: pd.DataFrame, k: int) -> pd.Series:
    c = df["close"].astype(float)
    return c / c.shift(k) - 1.0


def _rsi(df: pd.DataFrame, period: int) -> pd.Series:
    c = df["close"].astype(float)
    delta = c.diff()
    gain = delta.clip(lower=0.0).rolling(period, min_periods=period).mean()
    loss = (-delta.clip(upper=0.0)).rolling(period, min_periods=period).mean()
    rs = gain / loss.replace(0.0, np.nan)
    return 100.0 - 100.0 / (1.0 + rs)


def _channel_pos(df: pd.DataFrame, n: int) -> pd.Series:
    """Position of close within its trailing n-day [min,max] channel, in [0,1]."""
    c = df["close"].astype(float)
    lo = c.rolling(n, min_periods=n).min()
    hi = c.rolling(n, min_periods=n).max()
    return (c - lo) / (hi - lo).replace(0.0, np.nan)


def _ann_vol(df: pd.DataFrame, window: int = 30) -> pd.Series:
    c = df["close"].astype(float)
    logret = np.log(c).diff()
    return logret.rolling(window, min_periods=window).std(ddof=1) * np.sqrt(365.0)


# ---------------------------------------------------------------------------
# exposure_fn builders (PURE in `signal`)
# ---------------------------------------------------------------------------

def _threshold_long(signal: pd.Series, thr: float) -> pd.Series:
    """Long (1.0) when signal > thr, else flat. Warm-up (NaN) -> flat."""
    s = pd.Series(np.asarray(signal, dtype=float))
    return (s > thr).astype(float)


def _channel_state(signal: pd.Series, enter: float, exit_: float) -> pd.Series:
    """Donchian-style hysteresis on a [0,1] channel-position signal.

    Long once signal >= enter; stay long until signal <= exit_; then flat until
    the next breakout. Pure in `signal` (a shuffled signal yields a different,
    broken-causality exposure — exactly what the sham needs)."""
    s = np.asarray(signal, dtype=float)
    out = np.zeros(len(s))
    in_mkt = False
    for i, v in enumerate(s):
        if not np.isfinite(v):
            out[i] = 0.0
            continue
        if in_mkt:
            if v <= exit_:
                in_mkt = False
        else:
            if v >= enter:
                in_mkt = True
        out[i] = 1.0 if in_mkt else 0.0
    return pd.Series(out)


def _vol_target(signal: pd.Series, df: pd.DataFrame, sigma_target: float = 0.20,
                L_max: float = 1.0) -> pd.Series:
    """Always-long, vol-targeted exposure (non-directional). `signal` ignored
    except for length; warm-up -> WARMUP exposure."""
    av = _ann_vol(df, 30)
    expo = (sigma_target / av).clip(0.0, L_max)
    return expo.fillna(WARMUP)


# ---------------------------------------------------------------------------
# Candidate registry
# ---------------------------------------------------------------------------

def build_candidates() -> List[Candidate]:
    return [
        Candidate(
            name="trend_sma200",
            signal_fn=lambda df: _sma_ratio(df, 200),
            exposure_fn=lambda s, df: _threshold_long(s, 0.0),
            ic_horizon=5, expected_sign=1,
            thesis="long while above the 200d SMA (classic trend filter)",
        ),
        Candidate(
            name="tsmom_90",
            signal_fn=lambda df: _trailing_return(df, 90),
            exposure_fn=lambda s, df: _threshold_long(s, 0.0),
            ic_horizon=5, expected_sign=1,
            thesis="time-series momentum: long when trailing 90d return > 0",
        ),
        Candidate(
            name="rsi2_meanrev",
            # signal high == oversold == expect bounce -> expected_sign +1
            signal_fn=lambda df: 50.0 - _rsi(df, 2),
            exposure_fn=lambda s, df: _threshold_long(s, 40.0),   # RSI2 < 10
            ic_horizon=3, expected_sign=1,
            thesis="short-term mean reversion: long when RSI(2) deeply oversold",
        ),
        Candidate(
            name="donchian_50_20",
            signal_fn=lambda df: _channel_pos(df, 50),
            exposure_fn=lambda s, df: _channel_state(s, enter=0.95, exit_=0.25),
            ic_horizon=10, expected_sign=1,
            thesis="Donchian breakout: long near channel top, exit lower band",
        ),
        Candidate(
            name="vol_target_bh",
            signal_fn=lambda df: _trailing_return(df, 30),     # unused for IC
            exposure_fn=lambda s, df: _vol_target(s, df),
            directional=False,
            thesis="benchmark: vol-targeted buy-and-hold (the documented fallback)",
        ),
    ]
