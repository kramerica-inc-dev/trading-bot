#!/usr/bin/env python3
"""Regime classifier for Plan E (Phase 1).

Predicts whether the cross-sectional reversal basket Plan E would open
at time `t` is likely to land in the bottom quartile of forward 24h
returns. Output P(loss-tail) is used by the runner to gate execution
(see REGIME-TILT-PHASE1.md) — but only after walk-forward AUC > 0.55
on the strategy-aligned target (P3 bar in DESIGN-PRINCIPLES.md).

Universe-level features (six, all backward-looking):
    breadth_pos_72h       - fraction of universe with positive 72h log-return
    breadth_above_sma200  - fraction of universe with close > SMA-200
    xs_dispersion_72h     - std of cross-sectional 72h log-returns
    btc_vol_ratio_24_720  - BTC 24h vol / 30d vol (mirrors Agent C math)
    btc_trend_strength    - |BTC 72h log-return| / (BTC hourly sigma * sqrt(72))
    xs_rank_autocorr_72h  - Spearman corr of 72h-return ranks at t vs t-72

Target (strategy-aligned, NOT a price-move proxy):
    realized 24h return of the equal-weighted basket
    (long top-LONG_N − short bottom-SHORT_N) Plan E would open at t
    using the same signal_sign as production. Binarized at the
    train-fold 25th percentile inside the classifier.

Model: L2-regularized logistic regression. Same discipline as the
Plan D chop classifier — the experiment under test is the target
definition, not the model class.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import joblib
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler


FEATURE_NAMES: Tuple[str, ...] = (
    "breadth_pos_72h",
    "breadth_above_sma200",
    "xs_dispersion_72h",
    "btc_vol_ratio_24_720",
    "btc_trend_strength",
    "xs_rank_autocorr_72h",
)

DEFAULT_LOOKBACK_H = 72
DEFAULT_SMA_WINDOW_H = 200
DEFAULT_VOL_WINDOW_H = 24
DEFAULT_MA_WINDOW_H = 720       # 30 days of hourly bars
DEFAULT_HOLD_H = 24             # forward window for the target
DEFAULT_LONG_N = 3
DEFAULT_SHORT_N = 3
DEFAULT_SIGNAL_SIGN = -1        # REV (validated in PLAN-E-final.md)
DEFAULT_QUANTILE = 0.25         # bottom-quartile target binarization
DEFAULT_BTC_SYMBOL = "BTC-USDT"


# =========================  Feature engineering  =========================


def _safe_log_ret(closes: pd.DataFrame, h: int) -> pd.DataFrame:
    """Log return over h hours per asset; NaN where insufficient history."""
    return np.log(closes / closes.shift(h))


def compute_features(
    closes: pd.DataFrame,
    *,
    lookback_h: int = DEFAULT_LOOKBACK_H,
    sma_window_h: int = DEFAULT_SMA_WINDOW_H,
    vol_window_h: int = DEFAULT_VOL_WINDOW_H,
    ma_window_h: int = DEFAULT_MA_WINDOW_H,
    btc_symbol: str = DEFAULT_BTC_SYMBOL,
) -> pd.DataFrame:
    """Build universe-level feature DataFrame indexed to closes.

    `closes` is a wide DataFrame (timestamps × symbols). The returned
    DataFrame has the same index and columns = FEATURE_NAMES. Rows
    inside the warm-up window are NaN; caller dropna's before training.
    """
    if btc_symbol not in closes.columns:
        raise ValueError(f"BTC symbol {btc_symbol!r} not in universe")

    log_ret_lb = _safe_log_ret(closes, lookback_h)            # (T, n_assets)
    feat = pd.DataFrame(index=closes.index)

    # 1. breadth_pos_72h
    pos_count = (log_ret_lb > 0).sum(axis=1)
    valid_count = log_ret_lb.notna().sum(axis=1)
    feat["breadth_pos_72h"] = (pos_count / valid_count).where(valid_count > 0)

    # 2. breadth_above_sma200
    sma = closes.rolling(sma_window_h, min_periods=sma_window_h).mean()
    above = (closes > sma).sum(axis=1)
    valid_sma = sma.notna().sum(axis=1)
    feat["breadth_above_sma200"] = (above / valid_sma).where(valid_sma > 0)

    # 3. xs_dispersion_72h — std across universe at each t
    feat["xs_dispersion_72h"] = log_ret_lb.std(axis=1)

    # 4. btc_vol_ratio_24_720 — mirrors compute_vol_halt
    btc = closes[btc_symbol]
    btc_log_ret_1h = np.log(btc / btc.shift(1))
    recent_vol = btc_log_ret_1h.rolling(vol_window_h, min_periods=vol_window_h).std()
    ma_vol = btc_log_ret_1h.rolling(ma_window_h, min_periods=ma_window_h).std()
    feat["btc_vol_ratio_24_720"] = (recent_vol / ma_vol).where(ma_vol > 0)

    # 5. btc_trend_strength — |72h log return| / sqrt(72) sigma
    btc_log_ret_lb = np.log(btc / btc.shift(lookback_h))
    sigma_lb = ma_vol * np.sqrt(lookback_h)
    feat["btc_trend_strength"] = (btc_log_ret_lb.abs() / sigma_lb).where(sigma_lb > 0)

    # 6. xs_rank_autocorr_72h — Spearman corr of 72h-rank vector at t vs t-lookback
    ranks_now = log_ret_lb.rank(axis=1, method="average")
    ranks_prev = ranks_now.shift(lookback_h)
    feat["xs_rank_autocorr_72h"] = _row_corr(ranks_now, ranks_prev)

    return feat


def _row_corr(a: pd.DataFrame, b: pd.DataFrame) -> pd.Series:
    """Pearson correlation across columns, per row.

    Used for Spearman-on-ranks (Spearman = Pearson on ranks), so the
    inputs here are already rank-transformed.
    """
    a_c = a.sub(a.mean(axis=1), axis=0)
    b_c = b.sub(b.mean(axis=1), axis=0)
    num = (a_c * b_c).sum(axis=1)
    den = np.sqrt((a_c ** 2).sum(axis=1) * (b_c ** 2).sum(axis=1))
    return (num / den).where(den > 0)


# =========================  Target labeling  =========================


def compute_basket_forward_return(
    closes: pd.DataFrame,
    *,
    lookback_h: int = DEFAULT_LOOKBACK_H,
    hold_h: int = DEFAULT_HOLD_H,
    long_n: int = DEFAULT_LONG_N,
    short_n: int = DEFAULT_SHORT_N,
    signal_sign: int = DEFAULT_SIGNAL_SIGN,
) -> pd.Series:
    """Realized return of the Plan E basket opened at t, held hold_h hours.

    The basket at t:
      - signal[t, asset] = signal_sign * log(close[t] / close[t - lookback_h])
      - rank descending; longs = top long_n, shorts = bottom short_n
      - equal-weight inside each side, dollar-neutral across sides
      - return = mean(simple return long_legs t→t+hold_h)
                 - mean(simple return short_legs t→t+hold_h)

    No fees or slippage in the target — the classifier is predicting
    the *direction* of the basket, not the net P&L the strategy will
    book. Friction is constant per cycle in production, so it shifts
    the threshold but not the AUC.
    """
    log_ret_lb = _safe_log_ret(closes, lookback_h).values
    fwd_simple = (closes.shift(-hold_h) / closes - 1.0).values
    n_bars = len(closes)
    target = np.full(n_bars, np.nan)

    for i in range(n_bars):
        sig = log_ret_lb[i]
        if np.isnan(sig).any():
            continue
        if i + hold_h >= n_bars:
            break
        fr = fwd_simple[i]
        if np.isnan(fr).any():
            continue

        adj = signal_sign * sig
        order = np.argsort(-adj)        # descending by adjusted signal
        longs = order[:long_n]
        shorts = order[-short_n:]

        long_ret = float(fr[longs].mean())
        short_ret = float(fr[shorts].mean())
        target[i] = long_ret - short_ret

    return pd.Series(target, index=closes.index, name="basket_fwd_ret")


def binarize_to_loss_tail(
    raw: pd.Series,
    quantile: float = DEFAULT_QUANTILE,
    *,
    threshold: Optional[float] = None,
) -> Tuple[pd.Series, float]:
    """Convert raw basket forward return into a binary loss-tail label.

    label = 1  iff  raw < threshold (where threshold is the train-set
    `quantile` of raw if not supplied).

    Returns (label_series, threshold_used). Pass the returned threshold
    to the test fold so train and test share the cutoff.
    """
    if threshold is None:
        clean = raw.dropna()
        if clean.empty:
            raise ValueError("Cannot derive quantile threshold from empty series")
        threshold = float(clean.quantile(quantile))
    label = (raw < threshold).astype(float)
    label = label.where(raw.notna())
    return label.rename("label_loss_tail"), float(threshold)


# =========================  Classifier wrapper  =========================


@dataclass
class RegimeClassifierE:
    """Logistic regression on universe-level features for Plan E gating.

    Fit on a binarized loss-tail label; .predict_proba_loss returns the
    model's posterior P(label=1) — i.e., P(next 24h basket is in
    bottom quartile of train distribution).
    """

    C: float = 1.0
    feature_names: Tuple[str, ...] = field(default_factory=lambda: FEATURE_NAMES)
    scaler: Optional[StandardScaler] = None
    model: Optional[LogisticRegression] = None
    threshold: Optional[float] = None
    train_end_ts: Optional[str] = None
    train_n: int = 0
    train_pos_rate: float = 0.0
    fitted: bool = False

    def fit(
        self,
        X: pd.DataFrame,
        raw_target: pd.Series,
        *,
        quantile: float = DEFAULT_QUANTILE,
        threshold: Optional[float] = None,
        train_end_ts: Optional[str] = None,
    ) -> "RegimeClassifierE":
        feats = X[list(self.feature_names)]
        joined = feats.join(raw_target.rename("__raw__"), how="inner").dropna()
        if joined.empty:
            raise ValueError("No rows survive feature/target inner-join + dropna")

        labels, used_threshold = binarize_to_loss_tail(
            joined["__raw__"], quantile=quantile, threshold=threshold,
        )
        Xv = joined[list(self.feature_names)].values
        yv = labels.values.astype(int)

        self.scaler = StandardScaler()
        Xs = self.scaler.fit_transform(Xv)
        self.model = LogisticRegression(
            C=self.C, penalty="l2", solver="lbfgs",
            class_weight="balanced", max_iter=2000,
        )
        self.model.fit(Xs, yv)

        self.threshold = used_threshold
        self.train_end_ts = train_end_ts
        self.train_n = int(len(yv))
        self.train_pos_rate = float(yv.mean())
        self.fitted = True
        return self

    def predict_proba_loss(self, X: pd.DataFrame) -> pd.Series:
        if not self.fitted:
            raise RuntimeError("RegimeClassifierE not fitted")
        feats = X[list(self.feature_names)]
        keep = feats.dropna()
        if keep.empty:
            return pd.Series(dtype=float, index=X.index, name="p_loss")
        Xs = self.scaler.transform(keep.values)
        proba = self.model.predict_proba(Xs)[:, 1]
        return pd.Series(proba, index=keep.index, name="p_loss").reindex(X.index)

    def coef(self) -> pd.Series:
        if not self.fitted:
            raise RuntimeError("RegimeClassifierE not fitted")
        return pd.Series(self.model.coef_[0], index=list(self.feature_names))

    def to_dict(self) -> Dict[str, object]:
        return {
            "feature_names": list(self.feature_names),
            "threshold": self.threshold,
            "train_end_ts": self.train_end_ts,
            "train_n": self.train_n,
            "train_pos_rate": self.train_pos_rate,
            "C": self.C,
        }

    def save(self, path: str | Path) -> Path:
        if not self.fitted:
            raise RuntimeError("Cannot save unfitted classifier")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(
            {
                "scaler": self.scaler,
                "model": self.model,
                "metadata": self.to_dict(),
            },
            path,
        )
        sidecar = path.with_suffix(path.suffix + ".json")
        sidecar.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True))
        return path

    @classmethod
    def load(cls, path: str | Path) -> "RegimeClassifierE":
        bundle = joblib.load(Path(path))
        meta = bundle["metadata"]
        inst = cls(
            C=float(meta.get("C", 1.0)),
            feature_names=tuple(meta.get("feature_names", FEATURE_NAMES)),
        )
        inst.scaler = bundle["scaler"]
        inst.model = bundle["model"]
        inst.threshold = meta.get("threshold")
        inst.train_end_ts = meta.get("train_end_ts")
        inst.train_n = int(meta.get("train_n", 0))
        inst.train_pos_rate = float(meta.get("train_pos_rate", 0.0))
        inst.fitted = True
        return inst


# =========================  Convenience  =========================


def prepare_training_frame(
    features: pd.DataFrame,
    raw_target: pd.Series,
) -> pd.DataFrame:
    """Inner-join features + raw target on index, drop NaN. Returns one frame."""
    frame = features[list(FEATURE_NAMES)].join(
        raw_target.rename("raw_target"), how="inner",
    )
    return frame.dropna().reset_index()
