"""Tests for strategy rejection counters, diagnostics, and regime classification (C12/D-patch)."""

import random
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

from advanced_strategy import MultiIndicatorConfluence


class RejectionCounterTests(unittest.TestCase):
    """Rejection counter instrumentation should track filter activity."""

    def _make_candles(self, n=200, base_price=50000.0, volatility=0.005):
        random.seed(42)
        candles = []
        price = base_price
        ts = 1700000000000
        for i in range(n):
            open_p = price
            change = random.gauss(0, volatility * price)
            close_p = max(open_p + change, 1.0)
            high_p = max(open_p, close_p) * (1 + random.uniform(0, volatility))
            low_p = min(open_p, close_p) * (1 - random.uniform(0, volatility))
            vol = random.uniform(100, 1000)
            candles.append([str(ts), str(open_p), str(high_p),
                            str(low_p), str(close_p), str(vol)])
            price = close_p
            ts += 300000
        return candles

    def test_rejection_stats_initially_empty(self):
        strategy = MultiIndicatorConfluence({'min_confidence': 0.5})
        self.assertEqual(strategy.rejection_stats, {})

    def test_rejection_stats_populated_after_analyze(self):
        """After analyzing candles, at least one rejection reason should be recorded."""
        strategy = MultiIndicatorConfluence({'min_confidence': 0.5})
        candles = self._make_candles(200)
        current_price = float(candles[-1][4])
        signal = strategy.analyze(candles, current_price)
        # If signal is hold, there should be at least one rejection
        if signal.action == 'hold':
            self.assertGreater(len(strategy.rejection_stats), 0)
            total = sum(strategy.rejection_stats.values())
            self.assertGreater(total, 0)

    def test_reset_clears_counters(self):
        strategy = MultiIndicatorConfluence({'min_confidence': 0.5})
        candles = self._make_candles(200)
        current_price = float(candles[-1][4])
        strategy.analyze(candles, current_price)
        strategy.reset_rejection_stats()
        self.assertEqual(strategy.rejection_stats, {})

    def test_rejection_stats_accumulate_across_calls(self):
        """Multiple analyze calls should accumulate rejection counts."""
        strategy = MultiIndicatorConfluence({'min_confidence': 0.5})
        candles = self._make_candles(200)
        # Run analyze twice
        current_price = float(candles[-1][4])
        strategy.analyze(candles, current_price)
        count_after_first = sum(strategy.rejection_stats.values())
        strategy.analyze(candles, current_price)
        count_after_second = sum(strategy.rejection_stats.values())
        # Second call should add more rejections (or same if signal was not hold)
        self.assertGreaterEqual(count_after_second, count_after_first)

    def test_known_rejection_keys(self):
        """Rejection keys should be from the documented set."""
        valid_keys = {
            'insufficient_data', 'atr_too_low', 'chop_regime',
            'low_regime_confidence', 'quality_gate',
            'risk_allocation_zero', 'trade_spacing',
            'trend_long_mtf_fail', 'trend_long_no_setup',
            'trend_short_mtf_fail', 'trend_short_no_setup',
            'range_htf_blocked', 'range_no_edge',
            'range_disabled',
        }
        strategy = MultiIndicatorConfluence({'min_confidence': 0.5})
        candles = self._make_candles(200)
        current_price = float(candles[-1][4])
        strategy.analyze(candles, current_price)
        for key in strategy.rejection_stats:
            self.assertIn(key, valid_keys,
                          f"Unknown rejection key: {key}")


class UnclearRegimeTests(unittest.TestCase):
    """Tests for the 'unclear' regime classification (D1)."""

    def _make_candles(self, n=200, base_price=50000.0, volatility=0.005):
        random.seed(42)
        candles = []
        price = base_price
        ts = 1700000000000
        for i in range(n):
            open_p = price
            change = random.gauss(0, volatility * price)
            close_p = max(open_p + change, 1.0)
            high_p = max(open_p, close_p) * (1 + random.uniform(0, volatility))
            low_p = min(open_p, close_p) * (1 - random.uniform(0, volatility))
            vol = random.uniform(100, 1000)
            candles.append([str(ts), str(open_p), str(high_p),
                            str(low_p), str(close_p), str(vol)])
            price = close_p
            ts += 300000
        return candles

    def test_unclear_regime_code_exists(self):
        strategy = MultiIndicatorConfluence({'min_confidence': 0.5})
        self.assertIn('unclear', strategy._REGIME_CODE)

    def test_unclear_in_regime_spacing(self):
        strategy = MultiIndicatorConfluence({'min_confidence': 0.5})
        self.assertIn('unclear', strategy.regime_spacing_bars)

    def test_unclear_risk_multiplier_zero(self):
        strategy = MultiIndicatorConfluence({'min_confidence': 0.5})
        self.assertEqual(strategy.regime_risk_multipliers.get('unclear'), 0.0)


class DiagnosticsTests(unittest.TestCase):
    """Tests for per-candle diagnostics buffer (D2)."""

    def _make_candles(self, n=200, base_price=50000.0, volatility=0.005):
        random.seed(42)
        candles = []
        price = base_price
        ts = 1700000000000
        for i in range(n):
            open_p = price
            change = random.gauss(0, volatility * price)
            close_p = max(open_p + change, 1.0)
            high_p = max(open_p, close_p) * (1 + random.uniform(0, volatility))
            low_p = min(open_p, close_p) * (1 - random.uniform(0, volatility))
            vol = random.uniform(100, 1000)
            candles.append([str(ts), str(open_p), str(high_p),
                            str(low_p), str(close_p), str(vol)])
            price = close_p
            ts += 300000
        return candles

    def test_diagnostics_disabled_by_default(self):
        strategy = MultiIndicatorConfluence({'min_confidence': 0.5})
        candles = self._make_candles(200)
        strategy.analyze(candles, float(candles[-1][4]))
        self.assertEqual(len(strategy.get_diagnostics()), 0)

    def test_diagnostics_buffer_populated(self):
        strategy = MultiIndicatorConfluence({'min_confidence': 0.5})
        strategy.enable_diagnostics()
        candles = self._make_candles(200)
        strategy.analyze(candles, float(candles[-1][4]))
        diag = strategy.get_diagnostics()
        self.assertGreater(len(diag), 0)
        self.assertIn('efficiency_ratio', diag[0])
        self.assertIn('final_regime', diag[0])
        self.assertIn('bull_conditions_passing', diag[0])
        self.assertIn('bear_conditions_passing', diag[0])

    def test_diagnostics_clear(self):
        strategy = MultiIndicatorConfluence({'min_confidence': 0.5})
        strategy.enable_diagnostics()
        candles = self._make_candles(200)
        strategy.analyze(candles, float(candles[-1][4]))
        strategy.clear_diagnostics()
        self.assertEqual(len(strategy.get_diagnostics()), 0)


class NearMissTests(unittest.TestCase):
    """Tests for near-miss trend counters (D3)."""

    def _make_candles(self, n=200, base_price=50000.0, volatility=0.005):
        random.seed(42)
        candles = []
        price = base_price
        ts = 1700000000000
        for i in range(n):
            open_p = price
            change = random.gauss(0, volatility * price)
            close_p = max(open_p + change, 1.0)
            high_p = max(open_p, close_p) * (1 + random.uniform(0, volatility))
            low_p = min(open_p, close_p) * (1 - random.uniform(0, volatility))
            vol = random.uniform(100, 1000)
            candles.append([str(ts), str(open_p), str(high_p),
                            str(low_p), str(close_p), str(vol)])
            price = close_p
            ts += 300000
        return candles

    def test_near_miss_stats_is_dict(self):
        strategy = MultiIndicatorConfluence({'min_confidence': 0.5})
        candles = self._make_candles(200)
        strategy.analyze(candles, float(candles[-1][4]))
        nm = strategy.near_miss_stats
        self.assertIsInstance(nm, dict)

    def test_near_miss_reset(self):
        strategy = MultiIndicatorConfluence({'min_confidence': 0.5})
        candles = self._make_candles(200)
        strategy.analyze(candles, float(candles[-1][4]))
        strategy.reset_near_miss_stats()
        self.assertEqual(strategy.near_miss_stats, {})


class RangeDisableTests(unittest.TestCase):
    """Tests for allow_range_trades config flag (D5)."""

    def _make_candles(self, n=200, base_price=50000.0, volatility=0.005):
        random.seed(42)
        candles = []
        price = base_price
        ts = 1700000000000
        for i in range(n):
            open_p = price
            change = random.gauss(0, volatility * price)
            close_p = max(open_p + change, 1.0)
            high_p = max(open_p, close_p) * (1 + random.uniform(0, volatility))
            low_p = min(open_p, close_p) * (1 - random.uniform(0, volatility))
            vol = random.uniform(100, 1000)
            candles.append([str(ts), str(open_p), str(high_p),
                            str(low_p), str(close_p), str(vol)])
            price = close_p
            ts += 300000
        return candles

    def test_range_enabled_by_default(self):
        strategy = MultiIndicatorConfluence({'min_confidence': 0.5})
        self.assertTrue(strategy.allow_range_trades)

    def test_range_disabled_flag(self):
        strategy = MultiIndicatorConfluence({
            'min_confidence': 0.5,
            'allow_range_trades': False,
        })
        candles = self._make_candles(200)
        signal = strategy.analyze(candles, float(candles[-1][4]))
        # If regime was range, it should be held with range_disabled rejection
        if 'range_disabled' in strategy.rejection_stats:
            self.assertEqual(signal.action, 'hold')


class BacktesterSizingAlignmentTests(unittest.TestCase):
    """Backtester should use risk_utils for SL-based sizing."""

    def test_backtester_imports_risk_utils(self):
        """Backtester module should import calculate_risk_position_size."""
        from backtest.backtester import Backtester
        import inspect
        source = inspect.getsource(Backtester._calculate_size)
        self.assertIn('calculate_risk_position_size', source)

    def test_backtester_uses_stop_loss_param(self):
        """Backtester _calculate_size should accept stop_loss parameter."""
        import inspect
        from backtest.backtester import Backtester
        sig = inspect.signature(Backtester._calculate_size)
        param_names = list(sig.parameters.keys())
        self.assertIn('stop_loss', param_names)


if __name__ == '__main__':
    unittest.main()
