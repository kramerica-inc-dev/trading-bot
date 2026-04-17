#!/usr/bin/env python3
"""Advanced trading strategy with multi-timeframe regime filtering and regime-aware entries."""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from trading_strategy import Signal, TradingStrategy


@dataclass
class AdvancedSignal(Signal):
    """Enhanced signal with regime and indicator details."""

    indicators: Dict[str, float] = field(default_factory=dict)
    atr: Optional[float] = None
    regime: Optional[str] = None
    regime_metrics: Dict[str, float] = field(default_factory=dict)
    active_strategy: Optional[str] = None
    regime_scores: Dict[str, float] = field(default_factory=dict)
    regime_confidence: float = 0.0
    risk_multiplier: float = 1.0
    quality_score: float = 0.0
    max_hold_bars: Optional[int] = None


class MultiIndicatorConfluence(TradingStrategy):
    """Regime-aware strategy with multi-timeframe context.

    v6 goals:
    - use higher timeframe (1H/4H) context to avoid counter-trend entries
    - use 15m confirmation for 5m entries
    - keep range trades only when higher timeframes are not strongly trending
    - lower trade count further while improving regime robustness
    """

    _REGIME_CODE = {"bull_trend": 1.0, "bear_trend": -1.0, "range": 0.0, "chop": 2.0, "unclear": 0.5}
    _STATE_CODE = {"bull": 1.0, "bear": -1.0, "neutral": 0.0, "mixed": 0.5, "unknown": 0.0}

    def __init__(self, config: Dict):
        super().__init__(config)
        self.name = "Regime-Aware Multi-Strategy v6"

        # Core indicator parameters
        self.rsi_period = int(config.get("rsi_period", 14))
        self.rsi_oversold = float(config.get("rsi_oversold", 30))
        self.rsi_overbought = float(config.get("rsi_overbought", 70))
        self.macd_fast = int(config.get("macd_fast", 12))
        self.macd_slow = int(config.get("macd_slow", 26))
        self.macd_signal = int(config.get("macd_signal", 9))
        self.bb_period = int(config.get("bb_period", 20))
        self.bb_std = float(config.get("bb_std", 2.0))
        self.volume_period = int(config.get("volume_period", 20))
        self.volume_threshold = float(config.get("volume_threshold", 1.2))
        self.atr_period = int(config.get("atr_period", 14))

        self.stop_loss_atr_mult = float(config.get("stop_loss_atr_mult", 2.0))
        self.take_profit_atr_mult = float(config.get("take_profit_atr_mult", 3.0))
        self.min_confidence = float(config.get("min_confidence", 0.65))
        self.min_votes = int(config.get("min_votes", 2))

        regime_cfg = config.get("regime", {})
        self.regime_fast_ema = int(regime_cfg.get("fast_ema", 21))
        self.regime_slow_ema = int(regime_cfg.get("slow_ema", 55))
        self.regime_anchor_ema = int(regime_cfg.get("anchor_ema", 89))
        self.regime_window = int(regime_cfg.get("window", 30))
        self.trend_strength_threshold = float(regime_cfg.get("trend_strength_threshold", 0.0018))
        self.efficiency_trend_threshold = float(regime_cfg.get("efficiency_trend_threshold", 0.32))
        self.efficiency_range_threshold = float(regime_cfg.get("efficiency_range_threshold", 0.18))
        self.chop_atr_pct_threshold = float(regime_cfg.get("chop_atr_pct_threshold", 1.8))
        self.range_atr_pct_ceiling = float(regime_cfg.get("range_atr_pct_ceiling", 1.2))
        self.range_bias_threshold = float(regime_cfg.get("range_bias_threshold", 0.0010))
        self.anchor_slope_period = int(regime_cfg.get("anchor_slope_period", 12))
        self.anchor_slope_threshold = float(regime_cfg.get("anchor_slope_threshold", 0.0015))
        self.trend_persistence_bars = int(regime_cfg.get("trend_persistence_bars", 6))
        self.trend_min_score = float(regime_cfg.get("trend_min_score", 5.0))
        self.trade_spacing_bars = int(regime_cfg.get("trade_spacing_bars", 10))
        self.regime_spacing_bars = dict(regime_cfg.get("regime_spacing_bars", {
            "bull_trend": 12,
            "bear_trend": 12,
            "range": 10,
            "chop": 999999,
            "unclear": 999999,
        }))

        trend_cfg = config.get("trend_following", {})
        self.pullback_tolerance_pct = float(trend_cfg.get("pullback_tolerance_pct", 0.35))
        self.breakout_lookback = int(trend_cfg.get("breakout_lookback", 20))
        self.breakout_buffer_pct = float(trend_cfg.get("breakout_buffer_pct", 0.15))
        self.trend_min_rsi = float(trend_cfg.get("trend_min_rsi", 50))
        self.trend_max_rsi = float(trend_cfg.get("trend_max_rsi", 68))
        self.trend_stop_atr_mult = float(trend_cfg.get("stop_loss_atr_mult", 1.8))
        self.trend_take_profit_atr_mult = float(trend_cfg.get("take_profit_atr_mult", 3.2))
        self.trend_min_confidence = float(trend_cfg.get("min_confidence", max(0.58, self.min_confidence)))
        self.pullback_zone_atr_mult = float(trend_cfg.get("pullback_zone_atr_mult", 0.45))
        self.max_breakout_extension_atr = float(trend_cfg.get("max_breakout_extension_atr", 0.45))
        self.require_volume_on_breakout = bool(trend_cfg.get("require_volume_on_breakout", True))

        mean_rev_cfg = config.get("mean_reversion", {})
        self.range_entry_band = float(mean_rev_cfg.get("entry_band_threshold", 0.12))
        self.range_exit_band = float(mean_rev_cfg.get("exit_band_threshold", 0.88))
        self.range_stop_atr_mult = float(mean_rev_cfg.get("stop_loss_atr_mult", 1.2))
        self.range_take_profit_atr_mult = float(mean_rev_cfg.get("take_profit_atr_mult", 1.6))
        self.range_min_confidence = float(mean_rev_cfg.get("min_confidence", max(0.55, self.min_confidence)))
        self.range_midzone_avoidance = float(mean_rev_cfg.get("midzone_avoidance", 0.18))
        self.range_rsi_long_max = float(mean_rev_cfg.get("rsi_long_max", 34))
        self.range_rsi_short_min = float(mean_rev_cfg.get("rsi_short_min", 68))
        self.range_allow_shorts = bool(mean_rev_cfg.get("allow_shorts", False))
        self.allow_range_trades = bool(config.get("allow_range_trades", True))

        mtf_cfg = config.get("multi_timeframe", {})
        self.base_timeframe = str(config.get("base_timeframe", mtf_cfg.get("base_timeframe", "5m")))
        self.use_mtf = bool(mtf_cfg.get("enabled", True))
        self.entry_confirmation_timeframe = str(mtf_cfg.get("entry_confirmation_timeframe", "15m"))
        self.regime_trend_timeframe = str(mtf_cfg.get("regime_trend_timeframe", "1h"))
        self.regime_anchor_timeframe = str(mtf_cfg.get("regime_anchor_timeframe", "4h"))
        self.require_15m_confirmation = bool(mtf_cfg.get("require_15m_confirmation", True))
        self.require_1h_alignment = bool(mtf_cfg.get("require_1h_alignment", True))
        self.require_4h_alignment = bool(mtf_cfg.get("require_4h_alignment", True))
        self.allow_range_when_htf_trending = bool(mtf_cfg.get("allow_range_when_htf_trending", False))
        self.promote_range_to_trend = bool(mtf_cfg.get("promote_range_to_trend", True))
        self.confirmation_rsi_long_min = float(mtf_cfg.get("confirmation_rsi_long_min", 52))
        self.confirmation_rsi_short_max = float(mtf_cfg.get("confirmation_rsi_short_max", 48))
        self.range_confirmation_band = float(mtf_cfg.get("range_confirmation_band", 6.0))
        self.higher_tf_pullback_bias = float(mtf_cfg.get("higher_tf_pullback_bias", 0.10))
        self.htf_conflict_blocks_trade = bool(mtf_cfg.get("htf_conflict_blocks_trade", True))
        self.mtf_profiles = mtf_cfg.get("profiles", {
            "15m": {"fast_ema": 8, "slow_ema": 21, "anchor_ema": 34, "window": 18, "slope_period": 5, "trend_threshold": 0.0010, "efficiency_threshold": 0.24},
            "1h": {"fast_ema": 8, "slow_ema": 21, "anchor_ema": 34, "window": 16, "slope_period": 4, "trend_threshold": 0.0012, "efficiency_threshold": 0.20},
            "4h": {"fast_ema": 5, "slow_ema": 8, "anchor_ema": 13, "window": 10, "slope_period": 3, "trend_threshold": 0.0010, "efficiency_threshold": 0.16},
        })

        self.regime_live_profiles = dict(config.get('regime_live_profiles', {}) or {})
        self.live_profile_metadata = dict(config.get('live_profile_metadata', {}) or {})

        # Per-timeframe parameter profiles (step 5). Disabled by default.
        # Populated by load_timeframe_profiles() or via config key.
        self.timeframe_profiles: Dict[str, Dict] = dict(
            config.get('timeframe_profiles', {}) or {})
        self._active_calibrated_timeframe: Optional[str] = None

        risk_cfg = config.get('risk_allocation', {})
        self.base_risk_multiplier = float(risk_cfg.get('base_multiplier', 1.0))
        self.regime_risk_multipliers = {
            'bull_trend': float(risk_cfg.get('bull_trend_multiplier', 1.0)),
            'bear_trend': float(risk_cfg.get('bear_trend_multiplier', 0.8)),
            'range': float(risk_cfg.get('range_multiplier', 0.55)),
            'chop': float(risk_cfg.get('chop_multiplier', 0.0)),
            'unclear': float(risk_cfg.get('unclear_multiplier', 0.0)),
        }
        self.confidence_floor = float(risk_cfg.get('confidence_floor', 0.55))
        self.confidence_ceiling = float(risk_cfg.get('confidence_ceiling', 0.85))
        self.min_risk_multiplier = float(risk_cfg.get('min_multiplier', 0.0))
        self.max_risk_multiplier = float(risk_cfg.get('max_multiplier', 1.25))

        quality_cfg = config.get('trade_quality', {})
        self.trade_quality_enabled = bool(quality_cfg.get('enabled', True))
        self.min_trade_quality_score = float(quality_cfg.get('min_score', 0.55))
        self.min_rr_ratio = float(quality_cfg.get('min_rr_ratio', 1.2))
        self.min_volume_score = float(quality_cfg.get('min_volume_score', -0.1))
        self.max_atr_pct = float(quality_cfg.get('max_atr_pct', max(self.chop_atr_pct_threshold * 1.1, 2.0)))
        self.min_regime_confidence_for_entry = float(quality_cfg.get('min_regime_confidence', 0.40))
        self.max_trend_extension_atr = float(quality_cfg.get('max_trend_extension_atr', 1.0))
        self.range_midzone_block = bool(quality_cfg.get('block_midzone_range_entries', True))

        time_exit_cfg = config.get('time_exit', {})
        self.time_exit_enabled = bool(time_exit_cfg.get('enabled', True))
        self.max_hold_bars_by_regime = {
            'bull_trend': int(time_exit_cfg.get('bull_trend_bars', 48)),
            'bear_trend': int(time_exit_cfg.get('bear_trend_bars', 48)),
            'range': int(time_exit_cfg.get('range_bars', 18)),
            'chop': int(time_exit_cfg.get('chop_bars', 4)),
        }
        self.stale_trade_bars_by_regime = {
            'bull_trend': int(time_exit_cfg.get('bull_trend_stale_bars', 16)),
            'bear_trend': int(time_exit_cfg.get('bear_trend_stale_bars', 16)),
            'range': int(time_exit_cfg.get('range_stale_bars', 8)),
            'chop': int(time_exit_cfg.get('chop_stale_bars', 2)),
        }
        self.stale_progress_atr = float(time_exit_cfg.get('stale_progress_atr', 0.18))
        self._base_profile = {
            'min_confidence': self.min_confidence,
            'min_votes': self.min_votes,
            'allow_shorts': self.range_allow_shorts,
            'regime': {
                'trend_strength_threshold': self.trend_strength_threshold,
                'efficiency_trend_threshold': self.efficiency_trend_threshold,
                'range_atr_pct_ceiling': self.range_atr_pct_ceiling,
                'trend_min_score': self.trend_min_score,
            },
            'trend_following': {
                'min_confidence': self.trend_min_confidence,
                'trend_max_rsi': self.trend_max_rsi,
                'pullback_zone_atr_mult': self.pullback_zone_atr_mult,
                'max_breakout_extension_atr': self.max_breakout_extension_atr,
            },
            'mean_reversion': {
                'min_confidence': self.range_min_confidence,
                'entry_band_threshold': self.range_entry_band,
                'exit_band_threshold': self.range_exit_band,
                'allow_shorts': self.range_allow_shorts,
                'rsi_long_max': self.range_rsi_long_max,
            },
            'multi_timeframe': {
                'require_15m_confirmation': self.require_15m_confirmation,
                'require_1h_alignment': self.require_1h_alignment,
                'require_4h_alignment': self.require_4h_alignment,
            },
            'risk_allocation': {
                'base_multiplier': self.base_risk_multiplier,
                'bull_trend_multiplier': self.regime_risk_multipliers['bull_trend'],
                'bear_trend_multiplier': self.regime_risk_multipliers['bear_trend'],
                'range_multiplier': self.regime_risk_multipliers['range'],
                'chop_multiplier': self.regime_risk_multipliers['chop'],
                'confidence_floor': self.confidence_floor,
                'confidence_ceiling': self.confidence_ceiling,
                'min_multiplier': self.min_risk_multiplier,
                'max_multiplier': self.max_risk_multiplier,
            },
            'trade_quality': {
                'enabled': self.trade_quality_enabled,
                'min_score': self.min_trade_quality_score,
                'min_rr_ratio': self.min_rr_ratio,
                'min_regime_confidence': self.min_regime_confidence_for_entry,
            },
            'time_exit': {
                'enabled': self.time_exit_enabled,
                'stale_progress_atr': self.stale_progress_atr,
            },
        }
        self._bar_index = 0
        self._last_signal_bar = -10**9
        self._last_signal_bar_by_regime: Dict[str, int] = {}
        self._rejection_counts: Dict[str, int] = {}
        self._near_miss_counts: Dict[str, int] = {}
        self._diagnostics_enabled: bool = bool(config.get('diagnostics', {}).get('enabled', False))
        self._diagnostics_buffer: List[Dict] = []
        self._injected_htf_candles: Dict[str, List] = {}  # Backtest HTF override

    # ------------------------------ rejection tracking ----------------------

    def _record_rejection(self, reason_key: str) -> None:
        self._rejection_counts[reason_key] = (
            self._rejection_counts.get(reason_key, 0) + 1)

    @property
    def rejection_stats(self) -> Dict[str, int]:
        return dict(self._rejection_counts)

    def reset_rejection_stats(self) -> None:
        self._rejection_counts.clear()

    # ------------------------------ near-miss tracking ----------------------

    def _record_near_miss(self, key: str) -> None:
        self._near_miss_counts[key] = self._near_miss_counts.get(key, 0) + 1

    @property
    def near_miss_stats(self) -> Dict[str, int]:
        return dict(self._near_miss_counts)

    def reset_near_miss_stats(self) -> None:
        self._near_miss_counts.clear()

    # ------------------------------ diagnostics -----------------------------

    def enable_diagnostics(self) -> None:
        self._diagnostics_enabled = True

    def disable_diagnostics(self) -> None:
        self._diagnostics_enabled = False

    def get_diagnostics(self) -> List[Dict]:
        return list(self._diagnostics_buffer)

    def clear_diagnostics(self) -> None:
        self._diagnostics_buffer.clear()

    def export_diagnostics_csv(self, path: str) -> int:
        if not self._diagnostics_buffer:
            return 0
        import csv
        keys = list(self._diagnostics_buffer[0].keys())
        with open(path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(self._diagnostics_buffer)
        return len(self._diagnostics_buffer)

    # ------------------------------ indicators ------------------------------

    def calculate_rsi(self, prices: List[float]) -> float:
        if len(prices) < self.rsi_period + 1:
            return 50.0
        deltas = np.diff(prices)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = float(np.mean(gains[: self.rsi_period]))
        avg_loss = float(np.mean(losses[: self.rsi_period]))
        for i in range(self.rsi_period, len(gains)):
            avg_gain = (avg_gain * (self.rsi_period - 1) + gains[i]) / self.rsi_period
            avg_loss = (avg_loss * (self.rsi_period - 1) + losses[i]) / self.rsi_period
        if avg_gain == 0 and avg_loss == 0:
            return 50.0
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def calculate_macd(self, prices: List[float]) -> Tuple[float, float, float]:
        if len(prices) < self.macd_slow + self.macd_signal:
            return 0.0, 0.0, 0.0
        prices_array = np.array(prices, dtype=float)
        ema_fast_series = self._ema_series(prices_array, self.macd_fast)
        ema_slow_series = self._ema_series(prices_array, self.macd_slow)
        macd_series = ema_fast_series - ema_slow_series
        valid_macd = macd_series[self.macd_slow - 1 :]
        signal_line = self._ema(valid_macd, self.macd_signal)
        macd_line = float(macd_series[-1])
        histogram = macd_line - signal_line
        return macd_line, signal_line, histogram

    def calculate_bollinger_bands(self, prices: List[float]) -> Tuple[float, float, float]:
        if len(prices) < self.bb_period:
            price = prices[-1] if prices else 0.0
            return price, price, price
        recent_prices = np.array(prices[-self.bb_period :], dtype=float)
        middle = float(np.mean(recent_prices))
        std = float(np.std(recent_prices, ddof=1))
        return middle + (self.bb_std * std), middle, middle - (self.bb_std * std)

    def calculate_atr(self, candles: List) -> float:
        if len(candles) < self.atr_period + 1:
            return 0.0
        true_ranges = []
        start = len(candles) - self.atr_period
        for i in range(start, len(candles)):
            high = float(candles[i][2])
            low = float(candles[i][3])
            prev_close = float(candles[i - 1][4])
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            true_ranges.append(tr)
        return float(np.mean(true_ranges)) if true_ranges else 0.0

    def calculate_volume_signal(self, candles: List) -> float:
        if len(candles) < self.volume_period:
            return 0.0
        volumes = np.array([float(candle[5]) for candle in candles[-self.volume_period :]], dtype=float)
        avg_volume = float(np.mean(volumes))
        current_volume = float(candles[-1][5])
        volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1.0
        if volume_ratio >= self.volume_threshold:
            return float(min((volume_ratio - 1.0) / 0.75, 1.0))
        return 0.0

    def _ema(self, prices: np.ndarray, period: int) -> float:
        if len(prices) < period:
            return float(np.mean(prices))
        alpha = 2 / (period + 1)
        ema = float(np.mean(prices[:period]))
        for price in prices[period:]:
            ema = alpha * float(price) + (1 - alpha) * ema
        return ema

    def _ema_series(self, prices: np.ndarray, period: int) -> np.ndarray:
        result = np.zeros(len(prices))
        if len(prices) < period:
            result[:] = np.mean(prices)
            return result
        alpha = 2 / (period + 1)
        seed = np.mean(prices[:period])
        result[:period] = seed
        result[period - 1] = seed
        for i in range(period, len(prices)):
            result[i] = alpha * prices[i] + (1 - alpha) * result[i - 1]
        return result

    def _efficiency_ratio(self, prices: np.ndarray, period: int) -> float:
        if len(prices) < period + 1:
            return 0.0
        window = prices[-(period + 1) :]
        direction = abs(window[-1] - window[0])
        volatility = np.sum(np.abs(np.diff(window)))
        if volatility <= 0:
            return 0.0
        return float(direction / volatility)

    def _slope_pct(self, values: np.ndarray, period: int) -> float:
        if len(values) < period + 1:
            return 0.0
        start = float(values[-(period + 1)])
        end = float(values[-1])
        if start == 0:
            return 0.0
        return (end - start) / start

    # ------------------------------ HTF injection (backtest) ------------------

    def set_htf_candles(self, htf_candles: Dict[str, List]) -> None:
        """Inject pre-built HTF candles for the current bar.

        When set, build_multi_timeframe_context() uses these instead of
        resampling from the base-timeframe window.  Call with an empty
        dict to revert to resampling.

        Args:
            htf_candles: mapping of timeframe (e.g. '15m') to a list of
                         candle arrays, already filtered to only include
                         closed bars.
        """
        self._injected_htf_candles = dict(htf_candles)

    def clear_htf_candles(self) -> None:
        """Clear injected HTF candles — reverts to resampling."""
        self._injected_htf_candles = {}

    # ------------------------------ MTF helpers ------------------------------

    @staticmethod
    def _timeframe_to_minutes(timeframe: str) -> int:
        tf = timeframe.strip().lower()
        if tf.endswith('m'):
            return int(tf[:-1])
        if tf.endswith('h'):
            return int(tf[:-1]) * 60
        if tf.endswith('d'):
            return int(tf[:-1]) * 1440
        raise ValueError(f'Unsupported timeframe: {timeframe}')

    def _resample_candles(self, candles: List, target_timeframe: str) -> List:
        base_minutes = self._timeframe_to_minutes(self.base_timeframe)
        target_minutes = self._timeframe_to_minutes(target_timeframe)
        if target_minutes <= base_minutes or not candles:
            return candles
        factor = max(1, target_minutes // base_minutes)
        if factor <= 1:
            return candles
        target_ms = target_minutes * 60 * 1000
        buckets: dict = {}
        for c in candles:
            ts = int(c[0])
            bucket_key = ts - (ts % target_ms)
            buckets.setdefault(bucket_key, []).append(c)
        aggregated = []
        for key in sorted(buckets.keys()):
            group = buckets[key]
            aggregated.append([
                group[-1][0],
                float(group[0][1]),
                max(float(c[2]) for c in group),
                min(float(c[3]) for c in group),
                float(group[-1][4]),
                sum(float(c[5]) for c in group),
            ])
        return aggregated

    def _trend_state_from_candles(self, candles: List, profile: Dict) -> Tuple[str, Dict[str, float]]:
        if len(candles) < max(profile.get('anchor_ema', 13), profile.get('window', 10)) + max(profile.get('slope_period', 3), 2):
            return 'neutral', {'ready': 0.0}
        closes = np.array([float(c[4]) for c in candles], dtype=float)
        price = float(closes[-1])
        ema_fast = self._ema(closes, int(profile.get('fast_ema', 8)))
        ema_slow = self._ema(closes, int(profile.get('slow_ema', 21)))
        ema_anchor = self._ema(closes, int(profile.get('anchor_ema', 34)))
        fast_series = self._ema_series(closes, int(profile.get('fast_ema', 8)))
        slow_series = self._ema_series(closes, int(profile.get('slow_ema', 21)))
        anchor_series = self._ema_series(closes, int(profile.get('anchor_ema', 34)))
        trend_bias = ((ema_fast - ema_slow) / price) if price else 0.0
        anchor_bias = ((price - ema_anchor) / price) if price else 0.0
        anchor_slope = self._slope_pct(anchor_series, int(profile.get('slope_period', 4)))
        er = self._efficiency_ratio(closes, int(profile.get('window', 16)))
        persistent_above = bool(np.all(closes[-3:] > fast_series[-3:]))
        persistent_below = bool(np.all(closes[-3:] < fast_series[-3:]))
        trend_threshold = float(profile.get('trend_threshold', self.trend_strength_threshold))
        er_threshold = float(profile.get('efficiency_threshold', self.efficiency_trend_threshold * 0.8))

        state = 'neutral'
        if price > ema_fast > ema_slow > ema_anchor and persistent_above and trend_bias >= trend_threshold and anchor_bias > 0 and anchor_slope > 0 and er >= er_threshold:
            state = 'bull'
        elif price < ema_fast < ema_slow < ema_anchor and persistent_below and trend_bias <= -trend_threshold and anchor_bias < 0 and anchor_slope < 0 and er >= er_threshold:
            state = 'bear'
        metrics = {
            'ready': 1.0,
            'price': price,
            'ema_fast': ema_fast,
            'ema_slow': ema_slow,
            'ema_anchor': ema_anchor,
            'trend_bias': trend_bias,
            'anchor_bias': anchor_bias,
            'anchor_slope': anchor_slope,
            'efficiency_ratio': er,
            'state_code': self._STATE_CODE.get(state, 0.0),
        }
        return state, metrics

    def build_multi_timeframe_context(self, candles: List) -> Dict[str, Dict]:
        if not self.use_mtf:
            return {}
        context: Dict[str, Dict] = {}
        for tf in [self.entry_confirmation_timeframe, self.regime_trend_timeframe, self.regime_anchor_timeframe]:
            if tf in context:
                continue
            # Use injected HTF candles if available (backtest mode)
            if tf in self._injected_htf_candles and self._injected_htf_candles[tf]:
                resampled = self._injected_htf_candles[tf]
            else:
                resampled = self._resample_candles(candles, tf)
            profile = dict(self.mtf_profiles.get(tf, {}))
            if not profile:
                if tf == self.entry_confirmation_timeframe:
                    profile = dict(self.mtf_profiles.get('15m', {}))
                elif tf == self.regime_trend_timeframe:
                    profile = dict(self.mtf_profiles.get('1h', {}))
                else:
                    profile = dict(self.mtf_profiles.get('4h', {}))
            state, metrics = self._trend_state_from_candles(resampled, profile)
            closes = [float(c[4]) for c in resampled]
            macd_line, macd_signal, macd_hist = self.calculate_macd(closes) if len(closes) >= self.macd_slow + self.macd_signal else (0.0, 0.0, 0.0)
            rsi = self.calculate_rsi(closes) if len(closes) >= self.rsi_period + 1 else 50.0
            metrics.update({
                'bars': len(resampled),
                'rsi': rsi,
                'macd_hist': macd_hist,
                'macd_line': macd_line,
                'macd_signal': macd_signal,
                'state': state,
            })
            context[tf] = {
                'candles': resampled,
                'state': state,
                'metrics': metrics,
            }
        return context

    # ------------------------------ regime logic ------------------------------

    def detect_market_regime(self, candles: List) -> Tuple[str, Dict[str, float]]:
        prices = np.array([float(c[4]) for c in candles], dtype=float)
        if len(prices) < max(self.regime_anchor_ema, self.regime_window) + self.anchor_slope_period:
            return "range", {"regime_code": self._REGIME_CODE['range']}

        price = float(prices[-1])
        ema_fast = self._ema(prices, self.regime_fast_ema)
        ema_slow = self._ema(prices, self.regime_slow_ema)
        ema_anchor = self._ema(prices, self.regime_anchor_ema)
        fast_series = self._ema_series(prices, self.regime_fast_ema)
        slow_series = self._ema_series(prices, self.regime_slow_ema)
        anchor_series = self._ema_series(prices, self.regime_anchor_ema)
        trend_bias = ((ema_fast - ema_slow) / price) if price else 0.0
        anchor_bias = ((price - ema_anchor) / price) if price else 0.0
        anchor_slope = self._slope_pct(anchor_series, self.anchor_slope_period)
        er = self._efficiency_ratio(prices, self.regime_window)
        atr = self.calculate_atr(candles)
        atr_pct = ((atr / price) * 100) if price else 0.0
        persistent_above = bool(np.all(prices[-self.trend_persistence_bars :] > fast_series[-self.trend_persistence_bars :]))
        persistent_below = bool(np.all(prices[-self.trend_persistence_bars :] < fast_series[-self.trend_persistence_bars :]))

        metrics = {
            "price": price,
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "ema_anchor": ema_anchor,
            "trend_bias": trend_bias,
            "anchor_bias": anchor_bias,
            "anchor_slope": anchor_slope,
            "efficiency_ratio": er,
            "atr_pct": atr_pct,
            "persistent_above": 1.0 if persistent_above else 0.0,
            "persistent_below": 1.0 if persistent_below else 0.0,
        }

        bull_score = sum([
            1.0 if price > ema_fast > ema_slow > ema_anchor else 0.0,
            1.0 if persistent_above else 0.0,
            1.0 if trend_bias >= self.trend_strength_threshold else 0.0,
            1.0 if anchor_bias > 0 else 0.0,
            1.0 if anchor_slope >= self.anchor_slope_threshold else 0.0,
            1.0 if er >= self.efficiency_trend_threshold else 0.0,
            1.0 if atr_pct < self.chop_atr_pct_threshold else 0.0,
        ])
        bear_score = sum([
            1.0 if price < ema_fast < ema_slow < ema_anchor else 0.0,
            1.0 if persistent_below else 0.0,
            1.0 if trend_bias <= -self.trend_strength_threshold else 0.0,
            1.0 if anchor_bias < 0 else 0.0,
            1.0 if anchor_slope <= -self.anchor_slope_threshold else 0.0,
            1.0 if er >= self.efficiency_trend_threshold else 0.0,
            1.0 if atr_pct < self.chop_atr_pct_threshold else 0.0,
        ])

        strong_bull = bull_score >= self.trend_min_score
        strong_bear = bear_score >= self.trend_min_score
        range_like = (
            abs(trend_bias) <= self.range_bias_threshold * 1.5
            and abs(anchor_bias) <= self.range_bias_threshold * 2.0
            and er <= self.efficiency_range_threshold
            and atr_pct <= self.range_atr_pct_ceiling
        )
        choppy = (
            atr_pct >= self.chop_atr_pct_threshold
            or (er < self.efficiency_trend_threshold and abs(anchor_slope) < self.anchor_slope_threshold / 2 and atr_pct > self.range_atr_pct_ceiling)
        )

        # Near-miss tracking reuses the scores (no duplicate condition evaluation)
        bull_conditions_passing = bull_score
        bear_conditions_passing = bear_score
        if not strong_bull and bull_score >= self.trend_min_score - 2:
            self._record_near_miss('bull_near_miss')
        if not strong_bear and bear_score >= self.trend_min_score - 2:
            self._record_near_miss('bear_near_miss')
        metrics['bull_conditions_passing'] = float(bull_conditions_passing)
        metrics['bear_conditions_passing'] = float(bear_conditions_passing)

        if strong_bull:
            regime = "bull_trend"
            metrics["regime_confidence"] = bull_score / 7.0
        elif strong_bear:
            regime = "bear_trend"
            metrics["regime_confidence"] = bear_score / 7.0
        elif range_like:
            regime = "range"
        elif choppy:
            regime = "chop"
        else:
            regime = "unclear"

        metrics["base_regime_code"] = self._REGIME_CODE[regime]
        return regime, metrics

    def _finalize_regime_with_mtf(self, base_regime: str, base_metrics: Dict[str, float], mtf_context: Dict[str, Dict]) -> Tuple[str, Dict[str, float]]:
        metrics = dict(base_metrics)
        entry_ctx = mtf_context.get(self.entry_confirmation_timeframe, {})
        trend_ctx = mtf_context.get(self.regime_trend_timeframe, {})
        anchor_ctx = mtf_context.get(self.regime_anchor_timeframe, {})
        entry_state = entry_ctx.get('state', 'neutral')
        trend_state = trend_ctx.get('state', 'neutral')
        anchor_state = anchor_ctx.get('state', 'neutral')
        trend_bias = self._STATE_CODE.get(trend_state, 0.0)
        anchor_bias = self._STATE_CODE.get(anchor_state, 0.0)
        htf_score = trend_bias + anchor_bias

        metrics.update({
            'entry_tf_state_code': self._STATE_CODE.get(entry_state, 0.0),
            'trend_tf_state_code': trend_bias,
            'anchor_tf_state_code': anchor_bias,
            'entry_tf_rsi': float(entry_ctx.get('metrics', {}).get('rsi', 50.0)),
            'trend_tf_rsi': float(trend_ctx.get('metrics', {}).get('rsi', 50.0)),
            'anchor_tf_rsi': float(anchor_ctx.get('metrics', {}).get('rsi', 50.0)),
            'entry_tf_macd_hist': float(entry_ctx.get('metrics', {}).get('macd_hist', 0.0)),
            'trend_tf_macd_hist': float(trend_ctx.get('metrics', {}).get('macd_hist', 0.0)),
            'anchor_tf_macd_hist': float(anchor_ctx.get('metrics', {}).get('macd_hist', 0.0)),
            'htf_alignment_score': htf_score,
        })

        final_regime = base_regime
        if not self.use_mtf:
            final_regime = base_regime
        elif base_regime == 'bull_trend':
            if (self.require_1h_alignment and trend_state == 'bear') or (self.require_4h_alignment and anchor_state == 'bear'):
                final_regime = 'chop' if self.htf_conflict_blocks_trade else 'unclear'
            elif trend_state == 'bull' or anchor_state == 'bull':
                final_regime = 'bull_trend'
            else:
                final_regime = 'unclear'
        elif base_regime == 'bear_trend':
            if (self.require_1h_alignment and trend_state == 'bull') or (self.require_4h_alignment and anchor_state == 'bull'):
                final_regime = 'chop' if self.htf_conflict_blocks_trade else 'unclear'
            elif trend_state == 'bear' or anchor_state == 'bear':
                final_regime = 'bear_trend'
            else:
                final_regime = 'unclear'
        elif base_regime == 'range':
            if self.promote_range_to_trend and htf_score >= 2.0 and entry_state != 'bear':
                final_regime = 'bull_trend'
            elif self.promote_range_to_trend and htf_score <= -2.0 and entry_state != 'bull':
                final_regime = 'bear_trend'
            elif abs(htf_score) <= 1.0:
                final_regime = 'range'
            else:
                final_regime = 'chop'
        else:
            if htf_score >= 2.0 and base_metrics.get('atr_pct', 0.0) < self.chop_atr_pct_threshold:
                final_regime = 'bull_trend'
            elif htf_score <= -2.0 and base_metrics.get('atr_pct', 0.0) < self.chop_atr_pct_threshold:
                final_regime = 'bear_trend'
            else:
                final_regime = 'chop'

        metrics['regime_code'] = self._REGIME_CODE[final_regime]
        return final_regime, metrics

    # ------------------------------ signal helpers ------------------------------

    def _make_hold(self, reason: str, indicators: Dict[str, float], atr: float, regime: str, regime_metrics: Dict[str, float], active_strategy: str) -> AdvancedSignal:
        return AdvancedSignal(
            action='hold',
            confidence=0.0,
            reason=reason,
            indicators=indicators,
            atr=atr,
            regime=regime,
            regime_metrics=regime_metrics,
            active_strategy=active_strategy,
        )

    def _entry_spacing_allows(self, regime: str) -> bool:
        if (self._bar_index - self._last_signal_bar) < self.trade_spacing_bars:
            return False
        regime_gap = int(self.regime_spacing_bars.get(regime, self.trade_spacing_bars))
        last_regime_bar = self._last_signal_bar_by_regime.get(regime, -10**9)
        return (self._bar_index - last_regime_bar) >= regime_gap

    def _record_signal(self, regime: str) -> None:
        self._last_signal_bar = self._bar_index
        self._last_signal_bar_by_regime[regime] = self._bar_index

    def _mtf_entry_confirms(self, desired_side: str, mtf_context: Dict[str, Dict]) -> Tuple[bool, Dict[str, float]]:
        entry_ctx = mtf_context.get(self.entry_confirmation_timeframe, {})
        trend_ctx = mtf_context.get(self.regime_trend_timeframe, {})
        anchor_ctx = mtf_context.get(self.regime_anchor_timeframe, {})
        entry_metrics = entry_ctx.get('metrics', {})
        entry_state = entry_ctx.get('state', 'neutral')
        trend_state = trend_ctx.get('state', 'neutral')
        anchor_state = anchor_ctx.get('state', 'neutral')
        entry_rsi = float(entry_metrics.get('rsi', 50.0))
        entry_macd = float(entry_metrics.get('macd_hist', 0.0))
        checks: Dict[str, float] = {
            'entry_tf_state': self._STATE_CODE.get(entry_state, 0.0),
            'trend_tf_state': self._STATE_CODE.get(trend_state, 0.0),
            'anchor_tf_state': self._STATE_CODE.get(anchor_state, 0.0),
            'entry_tf_rsi': entry_rsi,
            'entry_tf_macd_hist': entry_macd,
        }
        if desired_side == 'buy':
            ok = (
                (not self.require_15m_confirmation or (entry_state != 'bear' and entry_rsi >= self.confirmation_rsi_long_min and entry_macd >= -self.higher_tf_pullback_bias))
                and (not self.require_1h_alignment or trend_state != 'bear')
                and (not self.require_4h_alignment or anchor_state != 'bear')
            )
        else:
            ok = (
                (not self.require_15m_confirmation or (entry_state != 'bull' and entry_rsi <= self.confirmation_rsi_short_max and entry_macd <= self.higher_tf_pullback_bias))
                and (not self.require_1h_alignment or trend_state != 'bull')
                and (not self.require_4h_alignment or anchor_state != 'bull')
            )
        return ok, checks

    def _trend_long_signal(self, current_price: float, candles: List, prices: List[float], indicators: Dict[str, float], atr: float, regime: str, regime_metrics: Dict[str, float], mtf_context: Dict[str, Dict]) -> AdvancedSignal:
        ema_fast = regime_metrics.get('ema_fast', current_price)
        ema_slow = regime_metrics.get('ema_slow', current_price)
        ema_anchor = regime_metrics.get('ema_anchor', current_price)
        anchor_slope = regime_metrics.get('anchor_slope', 0.0)
        rsi = indicators['rsi_value']
        macd_hist = indicators['macd_hist']
        volume_score = indicators['volume']
        recent_high = max(prices[-self.breakout_lookback:]) if len(prices) >= self.breakout_lookback else max(prices)
        breakout_buffer = self.breakout_buffer_pct / 100.0
        dist_to_fast = abs(current_price - ema_fast)
        in_pullback_zone = current_price >= ema_slow and dist_to_fast <= atr * self.pullback_zone_atr_mult
        breakout_ready = (
            current_price >= recent_high * (1 - breakout_buffer)
            and (current_price - recent_high) <= atr * self.max_breakout_extension_atr
            and (volume_score > 0 or not self.require_volume_on_breakout)
        )
        trend_filter = current_price > ema_fast > ema_slow > ema_anchor and anchor_slope > 0 and macd_hist > 0
        rsi_filter = self.trend_min_rsi <= rsi <= self.trend_max_rsi
        close_strength = float(candles[-1][4]) >= float(candles[-1][1])
        mtf_ok, mtf_checks = self._mtf_entry_confirms('buy', mtf_context)
        indicators.update({
            'trend_filter': 1.0 if trend_filter else -1.0,
            'pullback_setup': 1.0 if in_pullback_zone else 0.0,
            'breakout_setup': 1.0 if breakout_ready else 0.0,
            'anchor_slope': anchor_slope,
            'close_strength': 1.0 if close_strength else 0.0,
            'mtf_confirmed': 1.0 if mtf_ok else -1.0,
            **{f'mtf_{k}': float(v) if isinstance(v, (int, float)) else 0.0 for k, v in mtf_checks.items()},
        })
        confidence = min(
            (0.30 if trend_filter else 0.0)
            + (0.15 if rsi_filter else 0.0)
            + (0.10 if close_strength else 0.0)
            + (0.15 if macd_hist > 0 else 0.0)
            + (0.10 if in_pullback_zone or breakout_ready else 0.0)
            + (0.20 if mtf_ok else 0.0),
            1.0,
        )
        if trend_filter and rsi_filter and close_strength and (in_pullback_zone or breakout_ready) and mtf_ok and confidence >= self.trend_min_confidence:
            stop = min(ema_slow, current_price - (atr * self.trend_stop_atr_mult))
            take = current_price + (atr * self.trend_take_profit_atr_mult)
            setup = 'pullback' if in_pullback_zone else 'breakout'
            return AdvancedSignal(
                action='buy',
                confidence=confidence,
                reason=f"Bull trend {setup} with MTF confirmation: RSI {rsi:.1f}, MACD {macd_hist:.2f}",
                stop_loss=stop,
                take_profit=take,
                indicators=indicators,
                atr=atr,
                regime=regime,
                regime_metrics=regime_metrics,
                active_strategy='trend_long',
            )
        why = 'Bull trend but failed MTF alignment' if not mtf_ok else 'Bull trend but no qualified long setup'
        self._record_rejection('trend_long_mtf_fail' if not mtf_ok else 'trend_long_no_setup')
        return self._make_hold(why, indicators, atr, regime, regime_metrics, 'trend_long')

    def _trend_short_signal(self, current_price: float, candles: List, prices: List[float], indicators: Dict[str, float], atr: float, regime: str, regime_metrics: Dict[str, float], mtf_context: Dict[str, Dict]) -> AdvancedSignal:
        ema_fast = regime_metrics.get('ema_fast', current_price)
        ema_slow = regime_metrics.get('ema_slow', current_price)
        ema_anchor = regime_metrics.get('ema_anchor', current_price)
        anchor_slope = regime_metrics.get('anchor_slope', 0.0)
        rsi = indicators['rsi_value']
        macd_hist = indicators['macd_hist']
        volume_score = indicators['volume']
        recent_low = min(prices[-self.breakout_lookback:]) if len(prices) >= self.breakout_lookback else min(prices)
        breakout_buffer = self.breakout_buffer_pct / 100.0
        dist_to_fast = abs(current_price - ema_fast)
        in_rally_zone = current_price <= ema_slow and dist_to_fast <= atr * self.pullback_zone_atr_mult
        breakdown_ready = (
            current_price <= recent_low * (1 + breakout_buffer)
            and (recent_low - current_price) <= atr * self.max_breakout_extension_atr
            and (volume_score > 0 or not self.require_volume_on_breakout)
        )
        trend_filter = current_price < ema_fast < ema_slow < ema_anchor and anchor_slope < 0 and macd_hist < 0
        rsi_filter = (100 - self.trend_max_rsi) <= (100 - rsi) <= (100 - self.trend_min_rsi)
        close_weak = float(candles[-1][4]) <= float(candles[-1][1])
        mtf_ok, mtf_checks = self._mtf_entry_confirms('sell', mtf_context)
        indicators.update({
            'trend_filter': -1.0 if trend_filter else 1.0,
            'rally_setup': -1.0 if in_rally_zone else 0.0,
            'breakdown_setup': -1.0 if breakdown_ready else 0.0,
            'anchor_slope': anchor_slope,
            'close_weak': -1.0 if close_weak else 0.0,
            'mtf_confirmed': -1.0 if mtf_ok else 1.0,
            **{f'mtf_{k}': float(v) if isinstance(v, (int, float)) else 0.0 for k, v in mtf_checks.items()},
        })
        confidence = min(
            (0.30 if trend_filter else 0.0)
            + (0.15 if rsi_filter else 0.0)
            + (0.10 if close_weak else 0.0)
            + (0.15 if macd_hist < 0 else 0.0)
            + (0.10 if in_rally_zone or breakdown_ready else 0.0)
            + (0.20 if mtf_ok else 0.0),
            1.0,
        )
        if trend_filter and rsi_filter and close_weak and (in_rally_zone or breakdown_ready) and mtf_ok and confidence >= self.trend_min_confidence:
            stop = max(ema_slow, current_price + (atr * self.trend_stop_atr_mult))
            take = current_price - (atr * self.trend_take_profit_atr_mult)
            setup = 'rally short' if in_rally_zone else 'breakdown'
            return AdvancedSignal(
                action='sell',
                confidence=confidence,
                reason=f"Bear trend {setup} with MTF confirmation: RSI {rsi:.1f}, MACD {macd_hist:.2f}",
                stop_loss=stop,
                take_profit=take,
                indicators=indicators,
                atr=atr,
                regime=regime,
                regime_metrics=regime_metrics,
                active_strategy='trend_short',
            )
        why = 'Bear trend but failed MTF alignment' if not mtf_ok else 'Bear trend but no qualified short setup'
        self._record_rejection('trend_short_mtf_fail' if not mtf_ok else 'trend_short_no_setup')
        return self._make_hold(why, indicators, atr, regime, regime_metrics, 'trend_short')

    def _range_signal(self, current_price: float, candles: List, prices: List[float], indicators: Dict[str, float], atr: float, regime: str, regime_metrics: Dict[str, float], mtf_context: Dict[str, Dict]) -> AdvancedSignal:
        trend_ctx = mtf_context.get(self.regime_trend_timeframe, {})
        anchor_ctx = mtf_context.get(self.regime_anchor_timeframe, {})
        trend_state = trend_ctx.get('state', 'neutral')
        anchor_state = anchor_ctx.get('state', 'neutral')
        if not self.allow_range_when_htf_trending and ((trend_state == 'bull' and anchor_state == 'bull') or (trend_state == 'bear' and anchor_state == 'bear')):
            indicators.update({'range_htf_blocked': 1.0})
            self._record_rejection('range_htf_blocked')
            return self._make_hold('Range setup blocked by aligned higher-timeframe trend', indicators, atr, regime, regime_metrics, 'mean_reversion')

        rsi = indicators['rsi_value']
        volume_score = indicators['volume']
        bb_upper, bb_middle, bb_lower = self.calculate_bollinger_bands(prices)
        band_width = bb_upper - bb_lower
        bb_pos = (current_price - bb_lower) / band_width if band_width > 0 else 0.5
        macd_hist = indicators['macd_hist']
        prev_close = float(candles[-2][4]) if len(candles) >= 2 else current_price
        in_midzone = (0.5 - self.range_midzone_avoidance) <= bb_pos <= (0.5 + self.range_midzone_avoidance)
        entry_ctx = mtf_context.get(self.entry_confirmation_timeframe, {})
        entry_rsi = float(entry_ctx.get('metrics', {}).get('rsi', 50.0))
        entry_state = entry_ctx.get('state', 'neutral')

        directional_indicators = {
            'rsi': 1.0 if rsi <= self.range_rsi_long_max else -1.0 if rsi >= self.range_rsi_short_min else 0.0,
            'bollinger': 1.0 if bb_pos <= self.range_entry_band else -1.0 if bb_pos >= self.range_exit_band else 0.0,
            'macd': 0.6 if (bb_pos <= self.range_entry_band and macd_hist >= 0) else -0.6 if (bb_pos >= self.range_exit_band and macd_hist <= 0) else 0.0,
            'reversal_bar': 0.5 if (bb_pos <= self.range_entry_band and current_price > prev_close) else -0.5 if (bb_pos >= self.range_exit_band and current_price < prev_close) else 0.0,
            'volume': 0.3 if volume_score > 0 else 0.0,
            'entry_tf_neutrality': 0.4 if abs(entry_rsi - 50.0) <= self.range_confirmation_band and entry_state in {'neutral', 'range', 'mixed', 'unknown'} else 0.0,
        }
        indicators.update(directional_indicators)
        indicators.update({'bb_pos': bb_pos, 'in_midzone': 1.0 if in_midzone else 0.0})

        bullish_votes = sum(1 for v in directional_indicators.values() if v > 0.25)
        bearish_votes = sum(1 for v in directional_indicators.values() if v < -0.25)
        total_score = sum(directional_indicators.values())

        if not in_midzone and bullish_votes >= max(self.min_votes + 1, 4) and total_score > 1.7 and bb_pos <= self.range_entry_band:
            confidence = min(max(0.0, abs(total_score) / 3.1), 1.0)
            if confidence >= self.range_min_confidence:
                return AdvancedSignal(
                    action='buy',
                    confidence=confidence,
                    reason=f"Range mean reversion long with MTF neutrality: RSI {rsi:.1f}, BB pos {bb_pos:.2f}",
                    stop_loss=current_price - (atr * self.range_stop_atr_mult),
                    take_profit=min(bb_middle, current_price + (atr * self.range_take_profit_atr_mult)),
                    indicators=indicators,
                    atr=atr,
                    regime=regime,
                    regime_metrics=regime_metrics,
                    active_strategy='mean_reversion',
                )
        if self.range_allow_shorts and not in_midzone and bearish_votes >= max(self.min_votes + 1, 4) and total_score < -1.7 and bb_pos >= self.range_exit_band:
            confidence = min(max(0.0, abs(total_score) / 3.1), 1.0)
            if confidence >= self.range_min_confidence:
                return AdvancedSignal(
                    action='sell',
                    confidence=confidence,
                    reason=f"Range mean reversion short with MTF neutrality: RSI {rsi:.1f}, BB pos {bb_pos:.2f}",
                    stop_loss=current_price + (atr * self.range_stop_atr_mult),
                    take_profit=max(bb_middle, current_price - (atr * self.range_take_profit_atr_mult)),
                    indicators=indicators,
                    atr=atr,
                    regime=regime,
                    regime_metrics=regime_metrics,
                    active_strategy='mean_reversion',
                )
        self._record_rejection('range_no_edge')
        return self._make_hold('Range regime but no edge at band extremes', indicators, atr, regime, regime_metrics, 'mean_reversion')


    @staticmethod
    def _clamp01(value: float) -> float:
        return max(0.0, min(float(value), 1.0))

    def _compute_regime_scores(self, regime_metrics: Dict[str, float], mtf_context: Dict[str, Dict]) -> Tuple[Dict[str, float], float]:
        trend_bias = float(regime_metrics.get('trend_bias', 0.0))
        anchor_slope = float(regime_metrics.get('anchor_slope', 0.0))
        efficiency = float(regime_metrics.get('efficiency_ratio', 0.0))
        atr_pct = float(regime_metrics.get('atr_pct', 0.0))
        htf_align = float(regime_metrics.get('htf_alignment_score', 0.0))
        entry_state = str(mtf_context.get(self.entry_confirmation_timeframe, {}).get('state', 'neutral'))
        trend_state = str(mtf_context.get(self.regime_trend_timeframe, {}).get('state', 'neutral'))
        anchor_state = str(mtf_context.get(self.regime_anchor_timeframe, {}).get('state', 'neutral'))
        agreement_bonus = 0.12 if trend_state == anchor_state and trend_state in {'bull', 'bear'} else 0.0
        disagreement_penalty = 0.12 if trend_state != anchor_state and trend_state in {'bull', 'bear'} and anchor_state in {'bull', 'bear'} else 0.0

        bull = self._clamp01(
            (trend_bias / max(self.trend_strength_threshold * 2.0, 1e-6))
            + (anchor_slope / max(self.anchor_slope_threshold * 2.0, 1e-6))
            + (efficiency / max(self.efficiency_trend_threshold * 1.5, 1e-6))
            + max(htf_align, 0.0) * 0.22
            + (0.15 if entry_state == 'bull' else 0.0)
            + (0.15 if trend_state == 'bull' else 0.0)
            + (0.10 if anchor_state == 'bull' else 0.0)
            + agreement_bonus
            - (0.22 if atr_pct >= self.chop_atr_pct_threshold else 0.0)
            - disagreement_penalty
        )
        bear = self._clamp01(
            (-trend_bias / max(self.trend_strength_threshold * 2.0, 1e-6))
            + (-anchor_slope / max(self.anchor_slope_threshold * 2.0, 1e-6))
            + (efficiency / max(self.efficiency_trend_threshold * 1.5, 1e-6))
            + max(-htf_align, 0.0) * 0.22
            + (0.15 if entry_state == 'bear' else 0.0)
            + (0.15 if trend_state == 'bear' else 0.0)
            + (0.10 if anchor_state == 'bear' else 0.0)
            + agreement_bonus
            - (0.22 if atr_pct >= self.chop_atr_pct_threshold else 0.0)
            - disagreement_penalty
        )
        range_score = self._clamp01(
            ((self.efficiency_range_threshold * 1.25 - efficiency) / max(self.efficiency_range_threshold * 1.25, 1e-6))
            + ((self.range_atr_pct_ceiling * 1.15 - atr_pct) / max(self.range_atr_pct_ceiling * 1.15, 1e-6))
            + ((self.range_bias_threshold * 2.5 - abs(trend_bias)) / max(self.range_bias_threshold * 2.5, 1e-6))
            + (0.10 if entry_state in {'neutral', 'mixed'} else 0.0)
            + (0.08 if trend_state == 'neutral' else 0.0)
        )
        chop = self._clamp01(
            (atr_pct / max(self.chop_atr_pct_threshold, 1e-6))
            + ((self.efficiency_trend_threshold - efficiency) / max(self.efficiency_trend_threshold, 1e-6)) * 0.5
            + (0.12 if trend_state != anchor_state and trend_state in {'bull', 'bear'} and anchor_state in {'bull', 'bear'} else 0.0)
        )
        scores = {
            'bull_trend': bull,
            'bear_trend': bear,
            'range': range_score,
            'chop': chop,
        }
        ordered = sorted(scores.values(), reverse=True)
        confidence = self._clamp01((ordered[0] - ordered[1]) + ordered[0] * 0.35) if len(ordered) >= 2 else self._clamp01(ordered[0])
        return scores, confidence

    def _resolve_risk_multiplier(self, regime: str, signal_confidence: float, regime_confidence: float) -> float:
        if regime in ('chop', 'unclear'):
            return 0.0
        regime_mult = float(self.regime_risk_multipliers.get(regime, 1.0))
        conf_hi = max(self.confidence_ceiling, self.confidence_floor + 1e-6)
        conf_factor = (signal_confidence - self.confidence_floor) / (conf_hi - self.confidence_floor)
        conf_factor = self._clamp01(conf_factor)
        regime_factor = max(0.35, min(regime_confidence, 1.0))
        multiplier = self.base_risk_multiplier * regime_mult * (0.55 + 0.45 * conf_factor) * regime_factor
        return max(self.min_risk_multiplier, min(multiplier, self.max_risk_multiplier))

    def _evaluate_trade_quality(self, signal: AdvancedSignal, current_price: float, indicators: Dict[str, float], regime_confidence: float) -> Tuple[float, Dict[str, float]]:
        stop = signal.stop_loss
        take = signal.take_profit
        rr_ratio = 0.0
        if stop is not None and take is not None:
            risk = abs(current_price - float(stop))
            reward = abs(float(take) - current_price)
            rr_ratio = (reward / risk) if risk > 0 else 0.0
        atr_pct = float(indicators.get('atr_pct', 0.0))
        volume = float(indicators.get('volume', 0.0))
        bb_pos = float(indicators.get('bb_pos', 0.5)) if 'bb_pos' in indicators else 0.5
        trend_extension = 0.0
        if signal.regime in {'bull_trend', 'bear_trend'} and signal.atr:
            ema_fast = float(signal.regime_metrics.get('ema_fast', current_price)) if signal.regime_metrics else current_price
            trend_extension = abs(current_price - ema_fast) / max(float(signal.atr), 1e-9)
        score = 0.0
        score += 0.30 * self._clamp01(signal.confidence)
        score += 0.30 * self._clamp01(regime_confidence)
        score += 0.20 * self._clamp01(rr_ratio / max(self.min_rr_ratio, 1e-6))
        score += 0.10 * self._clamp01((volume - self.min_volume_score + 0.5) / 1.5)
        score += 0.10 * self._clamp01((self.max_atr_pct - atr_pct) / max(self.max_atr_pct, 1e-6))
        if signal.regime == 'range' and self.range_midzone_block and (0.5 - self.range_midzone_avoidance) <= bb_pos <= (0.5 + self.range_midzone_avoidance):
            score -= 0.20
        if signal.regime in {'bull_trend', 'bear_trend'} and trend_extension > self.max_trend_extension_atr:
            score -= 0.15
        score = self._clamp01(score)
        details = {
            'rr_ratio': rr_ratio,
            'atr_pct': atr_pct,
            'volume_score': volume,
            'trend_extension_atr': trend_extension,
            'bb_pos': bb_pos,
        }
        return score, details

    def _enrich_signal(self, signal: AdvancedSignal, regime_scores: Dict[str, float], regime_confidence: float, quality_score: float) -> AdvancedSignal:
        signal.regime_scores = dict(regime_scores)
        signal.regime_confidence = regime_confidence
        signal.quality_score = quality_score
        signal.risk_multiplier = self._resolve_risk_multiplier(signal.regime or 'range', signal.confidence, regime_confidence)
        if self.time_exit_enabled:
            signal.max_hold_bars = int(self.max_hold_bars_by_regime.get(signal.regime or 'range', 0))
        return signal

    # ------------------------------ timeframe profile -----------------------

    def set_active_timeframe(self, timeframe: Optional[str]) -> None:
        """Set the currently active timeframe so analyze() applies the right profile.

        Called by the bot each cycle after the timeframe resolver has
        settled on a TF. Has no effect if timeframe_profiles is empty
        or the provided TF has no calibrated profile — in that case the
        base strategy parameters remain in use.
        """
        if timeframe is None:
            self._active_calibrated_timeframe = None
            return
        tf = str(timeframe).strip()
        if tf in self.timeframe_profiles:
            self._active_calibrated_timeframe = tf
        else:
            self._active_calibrated_timeframe = None

    def load_timeframe_profiles(self, path: str) -> int:
        """Load profiles from the JSON file produced by calibrate_per_timeframe.py.

        Expected file shape:
            {
              "profiles": {"5m": {...}, "15m": {...}, "1h": {...}},
              ...
            }

        Returns the number of profiles loaded.
        """
        import json as _json
        from pathlib import Path as _Path
        p = _Path(path)
        if not p.exists():
            return 0
        try:
            payload = _json.loads(p.read_text())
        except Exception:
            return 0
        profiles = payload.get('profiles') if isinstance(payload, dict) else None
        if not isinstance(profiles, dict):
            return 0
        cleaned: Dict[str, Dict] = {}
        for tf, params in profiles.items():
            if not isinstance(params, dict):
                continue
            safe = {k: v for k, v in params.items()
                    if isinstance(v, (int, float, bool))}
            if safe:
                cleaned[tf] = safe
        self.timeframe_profiles = cleaned
        return len(cleaned)

    def _apply_timeframe_profile(self) -> None:
        """Deprecated: kept for backward compatibility with tests that call
        this directly. In normal bot flow, _apply_live_profile() calls
        _timeframe_patched_base_profile() instead so that the layering
        regime > TF > base works correctly.
        """
        tf = self._active_calibrated_timeframe
        if not tf:
            return
        profile = self.timeframe_profiles.get(tf) or {}
        if not profile:
            return
        if 'rsi_period' in profile:
            self.rsi_period = int(profile['rsi_period'])
        if 'trend_strength_threshold' in profile:
            self.trend_strength_threshold = float(
                profile['trend_strength_threshold'])
        if 'efficiency_trend_threshold' in profile:
            self.efficiency_trend_threshold = float(
                profile['efficiency_trend_threshold'])
        if 'min_confidence' in profile:
            self.min_confidence = float(profile['min_confidence'])
        if 'anchor_slope_threshold' in profile:
            self.anchor_slope_threshold = float(
                profile['anchor_slope_threshold'])

    def _timeframe_patched_base_profile(self) -> Dict:
        """Return a copy of self._base_profile with TF-calibrated
        parameters folded in. This is what _apply_live_profile uses as
        its starting point, so that regime overrides are layered on top
        of (TF > base), not (base > TF).
        """
        tf = self._active_calibrated_timeframe
        base = dict(self._base_profile)
        if not tf:
            return base
        profile = self.timeframe_profiles.get(tf) or {}
        if not profile:
            return base
        # Apply the same whitelist as _apply_timeframe_profile, but
        # into the profile-dict structure used by _apply_live_profile.
        if 'rsi_period' in profile:
            base['rsi_period'] = int(profile['rsi_period'])
            # _base_profile doesn't carry rsi_period at top level, but
            # we still apply it to self so it takes effect for this cycle.
            self.rsi_period = int(profile['rsi_period'])
        if 'min_confidence' in profile:
            base['min_confidence'] = float(profile['min_confidence'])
        if 'trend_strength_threshold' in profile:
            base.setdefault('regime', {})
            base['regime'] = dict(base.get('regime', {}))
            base['regime']['trend_strength_threshold'] = float(
                profile['trend_strength_threshold'])
        if 'efficiency_trend_threshold' in profile:
            base['regime'] = dict(base.get('regime', {}))
            base['regime']['efficiency_trend_threshold'] = float(
                profile['efficiency_trend_threshold'])
        if 'anchor_slope_threshold' in profile:
            # anchor_slope_threshold lives on self, not in _base_profile.
            # Apply it directly, regime overrides won't touch it.
            self.anchor_slope_threshold = float(
                profile['anchor_slope_threshold'])
        return base

    def _apply_live_profile(self, regime: str) -> None:
        # Start from a base profile that already has the TF-calibrated
        # parameters folded in. Then overlay the regime-level overrides.
        # Precedence: regime > timeframe > base.
        profile = self._timeframe_patched_base_profile()
        override = self.regime_live_profiles.get(regime) or {}
        for field in ('min_confidence', 'min_votes', 'allow_shorts'):
            if field in override:
                profile[field] = override[field]
        for section in ('regime', 'trend_following', 'mean_reversion', 'multi_timeframe', 'risk_allocation', 'trade_quality', 'time_exit'):
            merged = dict(profile.get(section, {}))
            merged.update(override.get(section, {}) or {})
            profile[section] = merged
        self.min_confidence = float(profile['min_confidence'])
        self.min_votes = int(profile['min_votes'])
        self.trend_strength_threshold = float(profile['regime']['trend_strength_threshold'])
        self.efficiency_trend_threshold = float(profile['regime']['efficiency_trend_threshold'])
        self.range_atr_pct_ceiling = float(profile['regime']['range_atr_pct_ceiling'])
        self.trend_min_score = float(profile['regime'].get('trend_min_score', self.trend_min_score))
        self.trend_min_confidence = float(profile['trend_following']['min_confidence'])
        self.trend_max_rsi = float(profile['trend_following']['trend_max_rsi'])
        self.pullback_zone_atr_mult = float(profile['trend_following']['pullback_zone_atr_mult'])
        self.max_breakout_extension_atr = float(profile['trend_following']['max_breakout_extension_atr'])
        self.range_min_confidence = float(profile['mean_reversion']['min_confidence'])
        self.range_entry_band = float(profile['mean_reversion']['entry_band_threshold'])
        self.range_exit_band = float(profile['mean_reversion']['exit_band_threshold'])
        self.range_allow_shorts = bool(profile['mean_reversion']['allow_shorts'])
        self.range_rsi_long_max = float(profile['mean_reversion']['rsi_long_max'])
        self.require_15m_confirmation = bool(profile['multi_timeframe']['require_15m_confirmation'])
        self.require_1h_alignment = bool(profile['multi_timeframe']['require_1h_alignment'])
        self.require_4h_alignment = bool(profile['multi_timeframe']['require_4h_alignment'])
        risk_profile = profile.get('risk_allocation', {})
        if risk_profile:
            self.base_risk_multiplier = float(risk_profile.get('base_multiplier', self.base_risk_multiplier))
            self.regime_risk_multipliers['bull_trend'] = float(risk_profile.get('bull_trend_multiplier', self.regime_risk_multipliers['bull_trend']))
            self.regime_risk_multipliers['bear_trend'] = float(risk_profile.get('bear_trend_multiplier', self.regime_risk_multipliers['bear_trend']))
            self.regime_risk_multipliers['range'] = float(risk_profile.get('range_multiplier', self.regime_risk_multipliers['range']))
            self.regime_risk_multipliers['chop'] = float(risk_profile.get('chop_multiplier', self.regime_risk_multipliers['chop']))
            self.regime_risk_multipliers['unclear'] = float(risk_profile.get('unclear_multiplier', self.regime_risk_multipliers.get('unclear', 0.0)))
            self.confidence_floor = float(risk_profile.get('confidence_floor', self.confidence_floor))
            self.confidence_ceiling = float(risk_profile.get('confidence_ceiling', self.confidence_ceiling))
        quality_profile = profile.get('trade_quality', {})
        if quality_profile:
            self.trade_quality_enabled = bool(quality_profile.get('enabled', self.trade_quality_enabled))
            self.min_trade_quality_score = float(quality_profile.get('min_score', self.min_trade_quality_score))
            self.min_rr_ratio = float(quality_profile.get('min_rr_ratio', self.min_rr_ratio))
            self.min_regime_confidence_for_entry = float(quality_profile.get('min_regime_confidence', self.min_regime_confidence_for_entry))
        time_profile = profile.get('time_exit', {})
        if time_profile:
            self.time_exit_enabled = bool(time_profile.get('enabled', self.time_exit_enabled))
            self.stale_progress_atr = float(time_profile.get('stale_progress_atr', self.stale_progress_atr))
    
    # ------------------------------ public API ------------------------------

    def analyze(self, candles: List, current_price: float) -> AdvancedSignal:
        self._bar_index += 1
        close_prices = [float(candle[4]) for candle in candles]
        min_history = max(
            self.bb_period,
            self.macd_slow + self.macd_signal,
            self.regime_anchor_ema + self.anchor_slope_period,
            self.regime_window + 2,
        )
        if len(close_prices) < min_history:
            self._record_rejection('insufficient_data')
            return AdvancedSignal('hold', 0.0, 'Not enough data')

        rsi = self.calculate_rsi(close_prices)
        macd_line, macd_signal, macd_hist = self.calculate_macd(close_prices)
        atr = self.calculate_atr(candles)
        if atr <= 0 or (current_price > 0 and atr < current_price * 1e-8):
            self._record_rejection('atr_too_low')
            return AdvancedSignal('hold', 0.0, 'ATR too low for safe entry',
                                  atr=atr, regime=None, regime_metrics={},
                                  active_strategy='no_trade')
        volume_score = self.calculate_volume_signal(candles)
        mtf_context = self.build_multi_timeframe_context(candles)
        base_regime, base_metrics = self.detect_market_regime(candles)
        regime, regime_metrics = self._finalize_regime_with_mtf(base_regime, base_metrics, mtf_context)
        regime_scores, regime_confidence = self._compute_regime_scores(regime_metrics, mtf_context)

        indicators: Dict[str, float] = {
            'rsi_value': rsi,
            'macd_line': macd_line,
            'macd_signal': macd_signal,
            'macd_hist': macd_hist,
            'volume': volume_score,
            'atr_pct': (atr / current_price * 100) if current_price else 0.0,
            'trend_bias': regime_metrics.get('trend_bias', 0.0),
            'efficiency_ratio': regime_metrics.get('efficiency_ratio', 0.0),
            'base_regime_code': self._REGIME_CODE.get(base_regime, 0.0),
            'htf_alignment_score': regime_metrics.get('htf_alignment_score', 0.0),
            'entry_tf_state_code': regime_metrics.get('entry_tf_state_code', 0.0),
            'trend_tf_state_code': regime_metrics.get('trend_tf_state_code', 0.0),
            'anchor_tf_state_code': regime_metrics.get('anchor_tf_state_code', 0.0),
            'regime_confidence': regime_confidence,
            'regime_score_bull': regime_scores.get('bull_trend', 0.0),
            'regime_score_bear': regime_scores.get('bear_trend', 0.0),
            'regime_score_range': regime_scores.get('range', 0.0),
            'regime_score_chop': regime_scores.get('chop', 0.0),
        }

        if self._diagnostics_enabled:
            self._diagnostics_buffer.append({
                'bar_index': self._bar_index,
                'price': current_price,
                'base_regime': base_regime,
                'final_regime': regime,
                'bull_score': regime_scores.get('bull_trend', 0),
                'bear_score': regime_scores.get('bear_trend', 0),
                'range_score': regime_scores.get('range', 0),
                'chop_score': regime_scores.get('chop', 0),
                'regime_confidence': regime_confidence,
                'efficiency_ratio': regime_metrics.get('efficiency_ratio', 0),
                'trend_bias': regime_metrics.get('trend_bias', 0),
                'anchor_slope': regime_metrics.get('anchor_slope', 0),
                'atr_pct': regime_metrics.get('atr_pct', 0),
                'persistent_above': regime_metrics.get('persistent_above', 0),
                'persistent_below': regime_metrics.get('persistent_below', 0),
                'htf_alignment': regime_metrics.get('htf_alignment_score', 0),
                'bull_conditions_passing': regime_metrics.get('bull_conditions_passing', 0),
                'bear_conditions_passing': regime_metrics.get('bear_conditions_passing', 0),
            })

        self._apply_live_profile(regime)

        if regime == 'bull_trend':
            signal = self._trend_long_signal(current_price, candles, close_prices, indicators, atr, regime, regime_metrics, mtf_context)
        elif regime == 'bear_trend':
            signal = self._trend_short_signal(current_price, candles, close_prices, indicators, atr, regime, regime_metrics, mtf_context)
        elif regime == 'range':
            if not self.allow_range_trades:
                self._record_rejection('range_disabled')
                signal = self._make_hold('Range trades disabled by config', indicators, atr, regime, regime_metrics, 'no_trade')
            else:
                signal = self._range_signal(current_price, candles, close_prices, indicators, atr, regime, regime_metrics, mtf_context)
        else:
            self._record_rejection('chop_regime')
            signal = self._make_hold(f'{regime}: no clear regime — filtered out', indicators, atr, regime, regime_metrics, 'no_trade')

        if signal.action != 'hold':
            quality_score, quality_details = self._evaluate_trade_quality(signal, current_price, indicators, regime_confidence)
            indicators.update({f'quality_{k}': float(v) for k, v in quality_details.items() if isinstance(v, (int, float))})
            signal = self._enrich_signal(signal, regime_scores, regime_confidence, quality_score)
            if regime_confidence < self.min_regime_confidence_for_entry:
                self._record_rejection('low_regime_confidence')
                return self._make_hold(
                    f'{regime} signal filtered by low regime confidence ({regime_confidence:.2f})',
                    indicators,
                    atr,
                    regime,
                    regime_metrics,
                    getattr(signal, 'active_strategy', 'low_regime_confidence'),
                )
            if self.trade_quality_enabled and quality_score < self.min_trade_quality_score:
                self._record_rejection('quality_gate')
                return self._make_hold(
                    f'{regime} signal filtered by quality gate ({quality_score:.2f})',
                    indicators,
                    atr,
                    regime,
                    regime_metrics,
                    getattr(signal, 'active_strategy', 'quality_filter'),
                )
            if signal.risk_multiplier <= 0.0:
                self._record_rejection('risk_allocation_zero')
                return self._make_hold(
                    f'{regime} signal filtered because dynamic risk allocation is zero',
                    indicators,
                    atr,
                    regime,
                    regime_metrics,
                    getattr(signal, 'active_strategy', 'risk_filter'),
                )
            if not self._entry_spacing_allows(regime):
                self._record_rejection('trade_spacing')
                return self._make_hold(
                    f'{regime} signal filtered by trade spacing/cooldown',
                    indicators,
                    atr,
                    regime,
                    regime_metrics,
                    getattr(signal, 'active_strategy', 'cooldown'),
                )
            self._record_signal(regime)
            return signal
        signal.regime_scores = dict(regime_scores)
        signal.regime_confidence = regime_confidence
        signal.quality_score = 0.0
        signal.risk_multiplier = 0.0 if regime == 'chop' else self._resolve_risk_multiplier(regime, signal.confidence, regime_confidence)
        signal.max_hold_bars = int(self.max_hold_bars_by_regime.get(regime, 0)) if self.time_exit_enabled else None
        return signal

    def calculate_position_size(self, balance: float, price: float, risk_percent: float, atr: float = None) -> float:
        if atr is None or atr == 0:
            return super().calculate_position_size(balance, price, risk_percent)
        volatility_pct = max((atr / price) * 100, 0.01)
        base_volatility = 2.0
        adjusted_risk = risk_percent * (base_volatility / volatility_pct)
        adjusted_risk = max(risk_percent * 0.4, min(adjusted_risk, risk_percent * 1.25))
        risk_amount = balance * (adjusted_risk / 100)
        contract_value = 0.001
        contracts = (risk_amount / price) / contract_value
        return max(0.1, round(contracts, 1))
