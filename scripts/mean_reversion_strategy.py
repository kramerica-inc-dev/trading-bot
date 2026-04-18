#!/usr/bin/env python3
"""Plan D mean-reversion strategy.

Single-asset (BTC-USDT 5m) mean reversion gated by the Plan D chop
classifier (see scripts/chop_classifier.py).

Design principles:
    - Chop gate is mandatory: no trade unless P(chop) > threshold.
    - Entry requires z-score extreme AND RSI confirming the same extreme.
    - TP = reversion to SMA20 at entry time (mean target), not ATR multiple.
    - SL = z-score band extension (regime break), not ATR multiple.
    - Max hold: 48 bars (4 hours) — if reversion hasn't happened, exit flat.

Features + classifier predictions must be pre-computed and injected via
`set_precomputed(features_by_ts_ms, p_chop_by_ts_ms)` before running a
backtest. This avoids per-bar feature recomputation and keeps training/
evaluation separation explicit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from trading_strategy import Signal, TradingStrategy


@dataclass
class MeanReversionSignal(Signal):
    """Signal enriched for the backtester's regime / indicator tracking."""

    indicators: Dict[str, float] = field(default_factory=dict)
    atr: Optional[float] = None
    regime: Optional[str] = None
    risk_multiplier: float = 1.0
    max_hold_bars: Optional[int] = None


def _rsi(closes: np.ndarray, period: int = 14) -> float:
    """Wilder's RSI over the most recent bars of the closes array."""
    if len(closes) < period + 1:
        return 50.0
    diffs = np.diff(closes[-(period + 1):])
    gains = np.where(diffs > 0, diffs, 0.0)
    losses = np.where(diffs < 0, -diffs, 0.0)
    avg_gain = gains.mean()
    avg_loss = losses.mean()
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


class MeanReversionStrategy(TradingStrategy):
    """Z-score mean reversion, gated by the chop classifier."""

    def __init__(self, config: Dict):
        super().__init__(config)
        self.name = "Plan D Mean Reversion"

        # Classifier gate
        self.min_chop_prob = float(config.get("min_chop_prob", 0.60))

        # Z-score entry / exit
        self.z_entry = float(config.get("z_entry", 2.0))
        self.z_stop = float(config.get("z_stop", 3.5))
        self.sma_length = int(config.get("sma_length", 20))

        # RSI confirmation
        self.rsi_period = int(config.get("rsi_period", 14))
        self.rsi_overbought = float(config.get("rsi_overbought", 70.0))
        self.rsi_oversold = float(config.get("rsi_oversold", 30.0))

        # Risk / hold
        self.max_hold_bars = int(config.get("max_hold_bars", 48))

        # Direction toggles (separate flags; both on by default)
        self.allow_long = bool(config.get("allow_long", True))
        self.allow_short = bool(config.get("allow_short", True))

        # Injected precomputed data (timestamp_ms -> dict/float)
        self._features: Dict[int, Dict[str, float]] = {}
        self._p_chop: Dict[int, float] = {}

        # Lightweight diagnostics
        self._reject_reasons: Dict[str, int] = {}

    # ---- Data injection ---------------------------------------------------

    def set_precomputed(
        self,
        features_by_ts_ms: Dict[int, Dict[str, float]],
        p_chop_by_ts_ms: Dict[int, float],
    ) -> None:
        self._features = features_by_ts_ms
        self._p_chop = p_chop_by_ts_ms

    # ---- Helpers ----------------------------------------------------------

    def _reject(self, reason: str, **extras) -> MeanReversionSignal:
        self._reject_reasons[reason] = self._reject_reasons.get(reason, 0) + 1
        return MeanReversionSignal(
            action="hold",
            confidence=0.0,
            reason=reason,
            regime=extras.get("regime", "unknown"),
            atr=extras.get("atr"),
            max_hold_bars=self.max_hold_bars,
        )

    # ---- Core -------------------------------------------------------------

    def analyze(self, candles: List, current_price: float) -> MeanReversionSignal:
        if not candles:
            return self._reject("no_candles")

        current = candles[-1]
        ts_ms = int(current[0])

        feats = self._features.get(ts_ms)
        p_chop = self._p_chop.get(ts_ms)

        if feats is None or p_chop is None or not np.isfinite(p_chop):
            return self._reject("no_precomputed", regime="unknown")

        atr14 = feats.get("atr14")
        sma20 = feats.get("sma20")
        std20 = feats.get("std20")
        if not all(np.isfinite([x]) for x in (sma20, std20, atr14)):
            return self._reject("nan_feature")
        if std20 <= 0:
            return self._reject("zero_std")

        regime_label = "chop" if p_chop >= self.min_chop_prob else "not_chop"

        # Chop gate
        if p_chop < self.min_chop_prob:
            return self._reject("below_chop_prob", regime=regime_label, atr=atr14)

        close = float(current_price)
        z = (close - sma20) / std20

        # Need z-score extreme
        if abs(z) < self.z_entry:
            return self._reject("z_not_extreme", regime=regime_label, atr=atr14)

        # RSI confirmation
        closes = np.asarray([float(c[4]) for c in candles], dtype=float)
        rsi = _rsi(closes, self.rsi_period)

        reason_extra = f"z={z:+.2f} p_chop={p_chop:.2f} rsi={rsi:.1f}"

        if z >= self.z_entry:
            # Overextended up — consider short
            if not self.allow_short:
                return self._reject("short_disabled", regime=regime_label, atr=atr14)
            if rsi < self.rsi_overbought:
                return self._reject("rsi_not_overbought",
                                    regime=regime_label, atr=atr14)
            target = sma20
            stop = close + (self.z_stop - z) * std20
            if stop <= close:
                # z already past z_stop: reject rather than open without stop
                return self._reject("z_past_stop_short",
                                    regime=regime_label, atr=atr14)
            confidence = min(
                1.0,
                0.55 + 0.10 * min(abs(z) - self.z_entry, 1.5)
                     + 0.20 * (p_chop - self.min_chop_prob),
            )
            return MeanReversionSignal(
                action="sell",
                confidence=confidence,
                reason=f"mean_rev_short {reason_extra}",
                stop_loss=float(stop),
                take_profit=float(target),
                atr=float(atr14),
                regime="chop_mean_rev",
                risk_multiplier=1.0,
                max_hold_bars=self.max_hold_bars,
                indicators={"z": z, "p_chop": p_chop, "rsi": rsi,
                            "sma20": sma20, "std20": std20},
            )

        # z <= -self.z_entry — overextended down, consider long
        if not self.allow_long:
            return self._reject("long_disabled", regime=regime_label, atr=atr14)
        if rsi > self.rsi_oversold:
            return self._reject("rsi_not_oversold", regime=regime_label, atr=atr14)
        target = sma20
        stop = close - (self.z_stop + z) * std20  # z is negative here
        if stop >= close:
            return self._reject("z_past_stop_long", regime=regime_label, atr=atr14)
        confidence = min(
            1.0,
            0.55 + 0.10 * min(abs(z) - self.z_entry, 1.5)
                 + 0.20 * (p_chop - self.min_chop_prob),
        )
        return MeanReversionSignal(
            action="buy",
            confidence=confidence,
            reason=f"mean_rev_long {reason_extra}",
            stop_loss=float(stop),
            take_profit=float(target),
            atr=float(atr14),
            regime="chop_mean_rev",
            risk_multiplier=1.0,
            max_hold_bars=self.max_hold_bars,
            indicators={"z": z, "p_chop": p_chop, "rsi": rsi,
                        "sma20": sma20, "std20": std20},
        )

    # ---- Introspection ----------------------------------------------------

    def reject_summary(self) -> Dict[str, int]:
        return dict(self._reject_reasons)
