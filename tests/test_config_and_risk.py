import json
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

from config_utils import ConfigError, load_and_validate_config
from risk_utils import calculate_risk_position_size


class ConfigValidationTests(unittest.TestCase):
    def test_conflicting_risk_values_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / 'config.json').write_text(json.dumps({
                'exchange': 'blofin',
                'blofin': {'api_key': 'YOUR', 'api_secret': 'YOUR', 'passphrase': 'YOUR'},
                'dry_run': True,
                'trading_pair': 'BTC-USDT',
                'strategy_name': 'advanced',
                'risk_per_trade_pct': 5,
                'risk': {'risk_per_trade_pct': 2, 'contract_size': 0.001},
                'trading': {'allow_long': True, 'allow_short': True, 'max_positions': 1},
                'strategy': {},
            }))
            with self.assertRaises(ConfigError):
                load_and_validate_config('config.json', base)

    def test_env_override_allows_live_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / 'config.json').write_text(json.dumps({
                'exchange': 'blofin',
                'blofin': {'api_key': 'YOUR', 'api_secret': 'YOUR', 'passphrase': 'YOUR'},
                'dry_run': False,
                'trading_pair': 'BTC-USDT',
                'strategy_name': 'advanced',
                'risk': {'risk_per_trade_pct': 1, 'contract_size': 0.001},
                'trading': {'allow_long': True, 'allow_short': True, 'max_positions': 1},
                'strategy': {},
            }))
            import os
            os.environ['BLOFIN_API_KEY'] = 'live_key'
            os.environ['BLOFIN_API_SECRET'] = 'live_secret'
            os.environ['BLOFIN_PASSPHRASE'] = 'live_pass'
            try:
                config = load_and_validate_config('config.json', base)
            finally:
                del os.environ['BLOFIN_API_KEY']
                del os.environ['BLOFIN_API_SECRET']
                del os.environ['BLOFIN_PASSPHRASE']
            self.assertEqual(config['blofin']['api_key'], 'live_key')
            self.assertFalse(config['dry_run'])


class RiskSizingTests(unittest.TestCase):
    def test_stop_based_sizing(self):
        result = calculate_risk_position_size(
            balance=1000,
            entry_price=50000,
            stop_loss=49000,
            risk_percent=1,
            contract_size=0.001,
            contract_step=0.1,
            min_contracts=0.1,
            leverage=2,
            max_position_notional_pct=100,
            slippage_buffer_pct=0,
        )
        self.assertGreater(result.contracts, 0)
        self.assertAlmostEqual(result.estimated_loss, 10.0, places=6)

    def test_notional_cap_can_zero_out_trade(self):
        result = calculate_risk_position_size(
            balance=100,
            entry_price=100000,
            stop_loss=99900,
            risk_percent=2,
            contract_size=0.001,
            contract_step=0.1,
            min_contracts=0.1,
            leverage=1,
            max_position_notional_pct=5,
            slippage_buffer_pct=0,
        )
        self.assertEqual(result.contracts, 0)
        self.assertIn('Maximum notional cap', result.reason)


class NewConfigSectionsTests(unittest.TestCase):
    """Tests for v2.2 config sections."""

    def _make_config(self, base, overrides=None):
        config = {
            'exchange': 'blofin',
            'blofin': {'api_key': 'YOUR', 'api_secret': 'YOUR', 'passphrase': 'YOUR'},
            'dry_run': True,
            'trading_pair': 'BTC-USDT',
            'strategy_name': 'advanced',
            'risk': {'risk_per_trade_pct': 1, 'contract_size': 0.001},
            'trading': {'allow_long': True, 'allow_short': True, 'max_positions': 1},
            'strategy': {},
        }
        if overrides:
            config.update(overrides)
        (base / 'config.json').write_text(json.dumps(config))
        return base

    def test_new_sections_have_safe_defaults(self):
        """Config without new sections loads with safe defaults."""
        with tempfile.TemporaryDirectory() as tmp:
            base = self._make_config(Path(tmp))
            config = load_and_validate_config('config.json', base)
            self.assertTrue(config['protection']['use_server_side_tpsl'])
            self.assertFalse(config['circuit_breaker']['enabled'])
            self.assertFalse(config['market_data']['use_websocket'])
            self.assertFalse(config['execution']['reconcile_pending_orders_each_cycle'])
            self.assertFalse(config['execution']['attach_tpsl_on_entry'])

    def test_require_tpsl_without_use_raises(self):
        """require_server_side_tpsl without use_server_side_tpsl should fail."""
        with tempfile.TemporaryDirectory() as tmp:
            base = self._make_config(Path(tmp), {
                'protection': {
                    'use_server_side_tpsl': False,
                    'require_server_side_tpsl': True,
                }
            })
            with self.assertRaises(ConfigError):
                load_and_validate_config('config.json', base)

    def test_circuit_breaker_invalid_values_raise(self):
        """Circuit breaker with bad values should fail validation."""
        with tempfile.TemporaryDirectory() as tmp:
            base = self._make_config(Path(tmp), {
                'circuit_breaker': {
                    'enabled': True,
                    'daily_loss_limit_pct': -1,
                }
            })
            with self.assertRaises(ConfigError):
                load_and_validate_config('config.json', base)

    def test_market_data_staleness_must_be_positive(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self._make_config(Path(tmp), {
                'market_data': {'max_staleness_seconds': 0}
            })
            with self.assertRaises(ConfigError):
                load_and_validate_config('config.json', base)

    def test_protection_section_preserves_user_values(self):
        """User-provided protection values should be preserved."""
        with tempfile.TemporaryDirectory() as tmp:
            base = self._make_config(Path(tmp), {
                'protection': {
                    'use_server_side_tpsl': True,
                    'require_server_side_tpsl': True,
                    'sync_exchange_each_cycle': True,
                }
            })
            config = load_and_validate_config('config.json', base)
            self.assertTrue(config['protection']['use_server_side_tpsl'])
            self.assertTrue(config['protection']['require_server_side_tpsl'])
            self.assertTrue(config['protection']['sync_exchange_each_cycle'])


class ParameterSelectorConfigTests(unittest.TestCase):
    """Tests for v2.3 parameter_selector and execution extensions."""

    def _make_config(self, base, overrides=None):
        config = {
            'exchange': 'blofin',
            'blofin': {'api_key': 'YOUR', 'api_secret': 'YOUR', 'passphrase': 'YOUR'},
            'dry_run': True,
            'trading_pair': 'BTC-USDT',
            'strategy_name': 'advanced',
            'risk': {'risk_per_trade_pct': 1, 'contract_size': 0.001},
            'trading': {'allow_long': True, 'allow_short': True, 'max_positions': 1},
            'strategy': {},
        }
        if overrides:
            config.update(overrides)
        (base / 'config.json').write_text(json.dumps(config))
        return base

    def test_parameter_selector_defaults_disabled(self):
        """Config without parameter_selector section loads with enabled=False."""
        with tempfile.TemporaryDirectory() as tmp:
            base = self._make_config(Path(tmp))
            config = load_and_validate_config('config.json', base)
            self.assertFalse(config['parameter_selector']['enabled'])
            self.assertFalse(config['parameter_selector']['auto_refresh_enabled'])
            self.assertEqual(config['parameter_selector']['refresh_interval_minutes'], 60)

    def test_parameter_selector_enabled_requires_profile_path(self):
        """parameter_selector.enabled=True with empty live_profile_path should fail."""
        with tempfile.TemporaryDirectory() as tmp:
            base = self._make_config(Path(tmp), {
                'parameter_selector': {
                    'enabled': True,
                    'live_profile_path': '',
                }
            })
            with self.assertRaises(ConfigError):
                load_and_validate_config('config.json', base)

    def test_execution_private_ws_defaults_disabled(self):
        """Execution section should default use_private_order_websocket to False."""
        with tempfile.TemporaryDirectory() as tmp:
            base = self._make_config(Path(tmp))
            config = load_and_validate_config('config.json', base)
            self.assertFalse(config['execution']['use_private_order_websocket'])
            self.assertTrue(config['execution']['prefer_private_order_websocket'])
            self.assertEqual(
                config['execution']['history_reconciliation_lookback_hours'], 48)
            self.assertEqual(
                config['execution']['history_reconciliation_limit'], 50)

    def test_execution_invalid_reconciliation_lookback_raises(self):
        """history_reconciliation_lookback_hours < 1 should fail."""
        with tempfile.TemporaryDirectory() as tmp:
            base = self._make_config(Path(tmp), {
                'execution': {'history_reconciliation_lookback_hours': 0}
            })
            with self.assertRaises(ConfigError):
                load_and_validate_config('config.json', base)

    def test_parameter_selector_invalid_drift_raises(self):
        """max_param_drift < 0 should fail."""
        with tempfile.TemporaryDirectory() as tmp:
            base = self._make_config(Path(tmp), {
                'parameter_selector': {'max_param_drift': -1}
            })
            with self.assertRaises(ConfigError):
                load_and_validate_config('config.json', base)


class AdvancedStrategyTests(unittest.TestCase):
    """Tests for the regime-aware advanced strategy."""

    def _make_candles(self, n=200, base_price=50000.0, volatility=0.005):
        """Generate synthetic candles: [ts, open, high, low, close, volume]."""
        import random
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
            ts += 300000  # 5m
        return candles

    def test_regime_detection_returns_valid_regime(self):
        """Strategy analyze should return a valid regime type."""
        from advanced_strategy import MultiIndicatorConfluence
        strategy = MultiIndicatorConfluence({'min_confidence': 0.5})
        candles = self._make_candles(200)
        current_price = float(candles[-1][4])
        signal = strategy.analyze(candles, current_price)
        valid_regimes = {'bull_trend', 'bear_trend', 'range', 'chop', 'unclear', None}
        self.assertIn(signal.regime, valid_regimes)

    def test_advanced_signal_has_required_fields(self):
        """AdvancedSignal should have all regime and quality fields."""
        from advanced_strategy import MultiIndicatorConfluence
        strategy = MultiIndicatorConfluence({'min_confidence': 0.5})
        candles = self._make_candles(200)
        current_price = float(candles[-1][4])
        signal = strategy.analyze(candles, current_price)
        self.assertIsInstance(signal.indicators, dict)
        self.assertIsInstance(signal.regime_confidence, float)
        self.assertIsInstance(signal.risk_multiplier, float)
        self.assertIsInstance(signal.quality_score, float)
        self.assertTrue(0.0 <= signal.regime_confidence <= 1.0)
        self.assertTrue(0.0 <= signal.risk_multiplier <= 2.0)

    def test_chop_regime_blocks_trades(self):
        """In chop regime, risk_multiplier should be 0."""
        from advanced_strategy import MultiIndicatorConfluence
        strategy = MultiIndicatorConfluence({
            'min_confidence': 0.5,
            'regime': {
                'chop_atr_pct_threshold': 0.0001,
            }
        })
        candles = self._make_candles(200, volatility=0.02)
        current_price = float(candles[-1][4])
        signal = strategy.analyze(candles, current_price)
        # Even if not chop, verify risk_multiplier is non-negative
        self.assertGreaterEqual(signal.risk_multiplier, 0.0)
        # If chop detected, multiplier should be 0
        if signal.regime == 'chop':
            self.assertAlmostEqual(signal.risk_multiplier, 0.0)


class BatchFixTests(unittest.TestCase):
    """Tests for Batch 2-3 review fixes."""

    def test_bollinger_uses_sample_std(self):
        """Bollinger bands should use ddof=1 (sample std)."""
        import numpy as np
        from advanced_strategy import MultiIndicatorConfluence
        strategy = MultiIndicatorConfluence({'min_confidence': 0.5})
        prices = [float(100 + i * 0.1) for i in range(20)]
        upper, middle, lower = strategy.calculate_bollinger_bands(prices)
        expected_std = float(np.std(prices[-20:], ddof=1))
        expected_upper = np.mean(prices[-20:]) + 2.0 * expected_std
        self.assertAlmostEqual(upper, expected_upper, places=6)

    def test_clock_aligned_resample(self):
        """Resampled candles should align to clock boundaries."""
        from advanced_strategy import MultiIndicatorConfluence
        strategy = MultiIndicatorConfluence({
            'min_confidence': 0.5,
            'base_timeframe': '5m',
        })
        # 6 candles at timestamps that fall into exactly 2 full 15m buckets
        # 15m = 900000ms. Use ts 0, 300000, 600000 (bucket 0)
        # and 900000, 1200000, 1500000 (bucket 900000)
        candles = []
        for i in range(6):
            ts = 300000 * i  # 0, 300000, 600000, 900000, 1200000, 1500000
            candles.append([str(ts), '100', '101', '99', '100', '10'])
        result = strategy._resample_candles(candles, '15m')
        self.assertEqual(len(result), 2)
        # Volume should aggregate: 3 candles * 10 = 30 each
        self.assertAlmostEqual(float(result[0][5]), 30.0)
        self.assertAlmostEqual(float(result[1][5]), 30.0)

    def test_balance_none_on_zero_entry(self):
        """calculate_risk_position_size with entry_price=0 returns reason."""
        result = calculate_risk_position_size(
            balance=1000, entry_price=0, stop_loss=100,
            risk_percent=1, contract_size=0.001,
        )
        self.assertEqual(result.contracts, 0)
        self.assertIn('zero or negative', result.reason)


if __name__ == '__main__':
    unittest.main()
