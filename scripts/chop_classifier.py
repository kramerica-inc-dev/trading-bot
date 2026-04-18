#!/usr/bin/env python3
"""Chop classifier for Plan D mean-reversion strategy.

Produces a per-bar probability that the next 48 bars will exhibit
mean-reverting behavior, defined as:

    price touches SMA20 within 48 bars AND does NOT break ±3×ATR14
    relative to the entry close during that window.

Features (all backward-looking, no lookahead):
    atr_pct       - ATR14 as fraction of close
    bb_width      - 4*std20/sma20 (Bollinger width fraction)
    adx14         - Wilder's ADX, 14-period
    hurst         - Hurst exponent via variance ratio (lag 10) over 100 bars
    autocorr_1    - 1-bar return autocorrelation over last 100 bars

Model: L2-regularized logistic regression (sklearn). Chosen over
gradient boosting deliberately - mirrors the discipline lesson from the
baseline strategy's feature freeze (DECISIONS.md 2026-04-18).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler


FEATURE_NAMES = ["atr_pct", "bb_width", "adx14", "hurst", "autocorr_1"]


# ---------- Feature engineering ----------


def _wilder_ema(series: pd.Series, length: int) -> pd.Series:
    """Wilder's smoothing (equivalent to EMA with alpha = 1/length)."""
    return series.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()


def compute_atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close_prev = df["close"].shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - close_prev).abs(),
        (low - close_prev).abs(),
    ], axis=1).max(axis=1)
    return _wilder_ema(tr, length)


def compute_adx(df: pd.DataFrame, length: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    up = high.diff()
    dn = -low.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    plus_dm = pd.Series(plus_dm, index=df.index)
    minus_dm = pd.Series(minus_dm, index=df.index)

    atr = compute_atr(df, length)
    plus_di = 100.0 * _wilder_ema(plus_dm, length) / atr.replace(0, np.nan)
    minus_di = 100.0 * _wilder_ema(minus_dm, length) / atr.replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return _wilder_ema(dx, length)


def compute_hurst_vr(log_returns: pd.Series, window: int = 100, lag: int = 10) -> pd.Series:
    """Rolling Hurst via variance-ratio at fixed lag.

    H = 0.5 + log(VR(k)) / (2 log k), where
        VR(k) = Var(k-period returns) / (k * Var(1-period returns))

    VR > 1 => trending (H > 0.5), VR < 1 => mean-reverting (H < 0.5).
    """
    var1 = log_returns.rolling(window).var()
    k_returns = log_returns.rolling(lag).sum()
    vark = k_returns.rolling(window).var()
    vr = vark / (lag * var1)
    hurst = 0.5 + np.log(vr.clip(lower=1e-12)) / (2.0 * np.log(lag))
    return hurst


def compute_autocorr_1(log_returns: pd.Series, window: int = 100) -> pd.Series:
    """Rolling 1-lag autocorrelation of returns.

    Negative values indicate mean-reverting behavior.
    """
    return log_returns.rolling(window).apply(
        lambda x: pd.Series(x).autocorr(lag=1) if len(x) == window else np.nan,
        raw=False,
    )


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """Build the feature DataFrame indexed to the input.

    Input df needs columns: open, high, low, close, volume (+ timestamp).
    Output df has columns matching FEATURE_NAMES plus the original columns.
    Rows with NaN features (warm-up period) are kept; caller is expected
    to dropna before training.
    """
    out = df.copy().reset_index(drop=True)

    close = out["close"]
    sma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    atr14 = compute_atr(out, 14)
    log_ret = np.log(close / close.shift(1))

    out["sma20"] = sma20
    out["std20"] = std20
    out["atr14"] = atr14

    out["atr_pct"] = atr14 / close
    out["bb_width"] = (4.0 * std20) / sma20
    out["adx14"] = compute_adx(out, 14)
    out["hurst"] = compute_hurst_vr(log_ret, window=100, lag=10)
    out["autocorr_1"] = compute_autocorr_1(log_ret, window=100)

    return out


# ---------- Target labeling ----------


def compute_target(
    df: pd.DataFrame,
    forward_bars: int = 48,
    atr_band_mult: float = 3.0,
) -> pd.Series:
    """Per-bar binary target: next `forward_bars` are mean-reverting.

    target[i] = 1 iff within bars i+1..i+forward_bars:
        (a) some close crosses sma20[i] (sign change of close-sma20[i]), AND
        (b) no high > close[i] + atr_band_mult*atr14[i], AND
        (c) no low  < close[i] - atr_band_mult*atr14[i]

    Otherwise 0. Rows with insufficient forward data are NaN.
    """
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    sma20 = df["sma20"].values
    atr14 = df["atr14"].values

    n = len(df)
    target = np.full(n, np.nan)

    for i in range(n):
        if i + forward_bars >= n:
            break
        ref_mean = sma20[i]
        ref_close = close[i]
        ref_atr = atr14[i]
        if not (np.isfinite(ref_mean) and np.isfinite(ref_atr) and ref_atr > 0):
            continue

        upper = ref_close + atr_band_mult * ref_atr
        lower = ref_close - atr_band_mult * ref_atr
        sign_at_i = np.sign(ref_close - ref_mean)
        if sign_at_i == 0:
            target[i] = 1  # already at mean
            continue

        reverted = False
        broke = False
        for j in range(i + 1, i + 1 + forward_bars):
            if high[j] > upper or low[j] < lower:
                broke = True
                break
            sign_j = np.sign(close[j] - ref_mean)
            # Crossed the mean if sign flipped or close == mean
            if sign_j == 0 or sign_j != sign_at_i:
                reverted = True
                break
        target[i] = 1 if (reverted and not broke) else 0

    return pd.Series(target, index=df.index, name="target_chop")


# ---------- Classifier wrapper ----------


@dataclass
class ChopClassifier:
    """Thin wrapper around logistic regression + scaler.

    Use .fit(X_train, y_train) then .predict_proba(X) to get P(chop).
    """

    C: float = 1.0
    scaler: StandardScaler = None
    model: LogisticRegression = None
    feature_names: Tuple[str, ...] = tuple(FEATURE_NAMES)
    fitted: bool = False

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "ChopClassifier":
        self.scaler = StandardScaler()
        Xs = self.scaler.fit_transform(X.values)
        self.model = LogisticRegression(
            C=self.C, penalty="l2", solver="lbfgs", max_iter=1000
        )
        self.model.fit(Xs, y.values.astype(int))
        self.fitted = True
        return self

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        if not self.fitted:
            raise RuntimeError("ChopClassifier not fitted")
        Xs = self.scaler.transform(X.values)
        return self.model.predict_proba(Xs)[:, 1]

    def coef(self) -> pd.Series:
        if not self.fitted:
            raise RuntimeError("ChopClassifier not fitted")
        return pd.Series(self.model.coef_[0], index=list(self.feature_names))


def prepare_training_frame(
    df_with_features: pd.DataFrame,
    target: pd.Series,
) -> pd.DataFrame:
    """Return a single DataFrame with features + target, dropna applied."""
    frame = df_with_features[list(FEATURE_NAMES)].copy()
    frame["target"] = target
    return frame.dropna().reset_index(drop=True)
