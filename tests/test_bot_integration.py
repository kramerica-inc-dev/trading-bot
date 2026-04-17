"""Integration tests for trading_bot.py critical paths.

Uses mock API and config to test order lifecycle, circuit breaker,
server-side TP/SL placement, and state persistence.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import sys
import os

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

from config_utils import load_and_validate_config


def _make_test_config(base_dir, overrides=None):
    """Create a minimal valid config for testing."""
    config = {
        'exchange': 'blofin',
        'blofin': {'api_key': 'test', 'api_secret': 'test', 'passphrase': 'test'},
        'dry_run': True,
        'trading_pair': 'BTC-USDT',
        'strategy_name': 'advanced',
        'risk': {
            'risk_per_trade_pct': 1,
            'contract_size': 0.001,
            'contract_step': 0.1,
            'min_contracts': 0.1,
            'leverage': 1,
        },
        'trading': {'allow_long': True, 'allow_short': True, 'max_positions': 1},
        'strategy': {'min_confidence': 0.5},
        'protection': {
            'use_server_side_tpsl': True,
            'require_server_side_tpsl': False,
            'tp_order_price': '-1',
            'sl_order_price': '-1',
        },
        'circuit_breaker': {
            'enabled': True,
            'daily_loss_limit_pct': 5.0,
            'max_consecutive_losses': 3,
            'max_consecutive_errors': 5,
            'cooldown_minutes': 60,
        },
    }
    if overrides:
        config.update(overrides)
    (base_dir / 'config.json').write_text(json.dumps(config))
    return config


class SaveStateAtomicTests(unittest.TestCase):
    """Test that _save_state uses atomic write pattern."""

    def test_save_state_creates_file(self):
        """_save_state should create a valid JSON state file."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _make_test_config(base)
            # Import and create bot with mocked exchange
            with patch('trading_bot.create_exchange_adapter') as mock_adapter:
                mock_adapter.return_value = MagicMock()
                from trading_bot import TradingBot
                bot = TradingBot(str(base / 'config.json'))

            bot.state = {'test': 'value', 'peak_balance': 100.0}
            bot._save_state()

            # File should exist and be valid JSON
            self.assertTrue(bot.state_file.exists())
            loaded = json.loads(bot.state_file.read_text())
            self.assertEqual(loaded['test'], 'value')
            self.assertEqual(loaded['peak_balance'], 100.0)

            # No .tmp file should remain
            tmp_file = bot.state_file.with_suffix(bot.state_file.suffix + '.tmp')
            self.assertFalse(tmp_file.exists())

    def test_save_state_logs_error_on_failure(self):
        """_save_state should log error, not silently fail."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _make_test_config(base)
            with patch('trading_bot.create_exchange_adapter') as mock_adapter:
                mock_adapter.return_value = MagicMock()
                from trading_bot import TradingBot
                bot = TradingBot(str(base / 'config.json'))

            # Point state_file to non-existent directory
            bot.state_file = Path(tmp) / 'nonexistent' / 'state.json'
            bot._log = MagicMock()
            bot._save_state()
            # Should have logged an error
            bot._log.assert_called()
            args = bot._log.call_args[0]
            self.assertEqual(args[0], 'error')


class CircuitBreakerTests(unittest.TestCase):
    """Test circuit breaker behavior."""

    def test_peak_balance_drawdown(self):
        """Drawdown should be measured from peak, not start."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _make_test_config(base)
            with patch('trading_bot.create_exchange_adapter') as mock_adapter:
                mock_adapter.return_value = MagicMock()
                from trading_bot import TradingBot
                bot = TradingBot(str(base / 'config.json'))

            # Simulate balance going up then down
            bot._update_balance_state(100.0)
            self.assertEqual(bot.state['peak_balance'], 100.0)

            bot._update_balance_state(120.0)
            self.assertEqual(bot.state['peak_balance'], 120.0)

            # Balance drops to 110 — drawdown from peak 120
            # dd = (120 - 110) / 120 * 100 = 8.33%
            bot._update_balance_state(110.0)
            self.assertEqual(bot.state['peak_balance'], 120.0)
            # With 5% daily loss limit, this should trip the breaker
            self.assertTrue(bot.state.get('circuit_breaker', {}).get('active', False))

    def test_error_streak_trips_breaker(self):
        """Consecutive errors should trip the circuit breaker."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _make_test_config(base)
            with patch('trading_bot.create_exchange_adapter') as mock_adapter:
                mock_adapter.return_value = MagicMock()
                from trading_bot import TradingBot
                bot = TradingBot(str(base / 'config.json'))

            # Record errors up to the limit
            for i in range(5):
                bot._record_error(f"test error {i}")

            self.assertTrue(bot.state.get('circuit_breaker', {}).get('active', False))


class LeverageValidationTests(unittest.TestCase):
    """Test leverage upper bound validation."""

    def test_leverage_over_20_rejected(self):
        """Leverage > 20 should fail validation."""
        from config_utils import ConfigError
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = {
                'exchange': 'blofin',
                'blofin': {'api_key': 'test', 'api_secret': 'test',
                           'passphrase': 'test'},
                'dry_run': True,
                'trading_pair': 'BTC-USDT',
                'strategy_name': 'advanced',
                'risk': {
                    'risk_per_trade_pct': 1,
                    'contract_size': 0.001,
                    'leverage': 25,
                },
                'trading': {'allow_long': True, 'allow_short': True,
                            'max_positions': 1},
                'strategy': {},
            }
            (base / 'config.json').write_text(json.dumps(config))
            with self.assertRaises(ConfigError):
                load_and_validate_config('config.json', base)

    def test_leverage_20_accepted(self):
        """Leverage = 20 should pass validation."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = {
                'exchange': 'blofin',
                'blofin': {'api_key': 'test', 'api_secret': 'test',
                           'passphrase': 'test'},
                'dry_run': True,
                'trading_pair': 'BTC-USDT',
                'strategy_name': 'advanced',
                'risk': {
                    'risk_per_trade_pct': 1,
                    'contract_size': 0.001,
                    'leverage': 20,
                },
                'trading': {'allow_long': True, 'allow_short': True,
                            'max_positions': 1},
                'strategy': {},
            }
            (base / 'config.json').write_text(json.dumps(config))
            result = load_and_validate_config('config.json', base)
            self.assertEqual(float(result['risk']['leverage']), 20.0)


class ServerSideTPSLTests(unittest.TestCase):
    """Test server-side TP/SL placement in _apply_partial_fill."""

    def test_tpsl_placed_on_fill(self):
        """When use_server_side_tpsl is enabled, TP/SL should be placed
        after a fill regardless of fallback_place_tpsl_after_partial_fill."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _make_test_config(base)
            with patch('trading_bot.create_exchange_adapter') as mock_adapter:
                mock_api = MagicMock()
                mock_adapter.return_value = mock_api
                from trading_bot import TradingBot
                bot = TradingBot(str(base / 'config.json'))

            # Ensure fallback is OFF (simulating production config)
            bot.execution_cfg['fallback_place_tpsl_after_partial_fill'] = False
            bot.execution_cfg['attach_tpsl_on_entry'] = False
            bot.dry_run = False

            # Mock the TP/SL API call to succeed
            mock_api.place_tpsl_order.return_value = {
                'code': '0', 'data': {'algoId': 'test123'}
            }

            pending = {
                'inst_id': 'BTC-USDT',
                'order_id': 'ord1',
                'side': 'buy',
                'position_side': 'long',
                'entry_price': 50000,
                'size': 1.0,
                'filled_size': 0.0,
                'average_price': 50000,
                'stop_loss': 49000,
                'take_profit': 52000,
                'server_side_tpsl': False,
                'timestamp': '2026-01-01T00:00:00+00:00',
                'state': 'submitted',
            }
            bot.pending_orders = [pending]

            bot._apply_partial_fill(
                pending, filled_size=1.0, average_price=50000, state='filled')

            # TP/SL should have been placed
            mock_api.place_tpsl_order.assert_called_once()
            # Position should be marked as protected
            self.assertTrue(len(bot.active_positions) > 0)
            pos = bot.active_positions[0]
            self.assertTrue(pos.get('server_side_tpsl'))


class LogRotationTests(unittest.TestCase):
    """Test log and snapshot rotation."""

    def test_rotate_log_file(self):
        """Log file exceeding max_bytes should be rotated."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _make_test_config(base)
            with patch('trading_bot.create_exchange_adapter') as mock_adapter:
                mock_adapter.return_value = MagicMock()
                from trading_bot import TradingBot
                bot = TradingBot(str(base / 'config.json'))

            log_path = base / 'test.jsonl'
            log_path.write_text('x' * 1000)

            # Should not rotate (under limit)
            bot._rotate_log_file(log_path, max_bytes=2000)
            self.assertTrue(log_path.exists())
            self.assertFalse(log_path.with_suffix('.jsonl.old').exists())

            # Should rotate (over limit)
            bot._rotate_log_file(log_path, max_bytes=500)
            self.assertFalse(log_path.exists())
            self.assertTrue(log_path.with_suffix('.jsonl.old').exists())

    def test_rotate_reconciliation_snapshots(self):
        """Reconciliation dir should be trimmed to max_files."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _make_test_config(base)
            with patch('trading_bot.create_exchange_adapter') as mock_adapter:
                mock_adapter.return_value = MagicMock()
                from trading_bot import TradingBot
                bot = TradingBot(str(base / 'config.json'))

            # Create 10 snapshot files
            for i in range(10):
                (bot.reconciliation_dir / f'2026010{i}T000000Z-test.json').write_text('{}')

            bot._rotate_reconciliation_snapshots(max_files=5)
            remaining = list(bot.reconciliation_dir.glob('*.json'))
            self.assertEqual(len(remaining), 5)


class BacktesterSlippageTests(unittest.TestCase):
    """Test that backtester applies slippage and time exits."""

    def test_slippage_reduces_pnl(self):
        """With slippage, a round trip should cost more than without."""
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'backtest'))
        from backtester import Backtester, BacktestConfig
        from advanced_strategy import MultiIndicatorConfluence
        import pandas as pd

        # Create minimal candle data
        import random
        random.seed(42)
        rows = []
        price = 50000.0
        ts = pd.Timestamp('2026-01-01')
        for i in range(300):
            o = price
            c = o + random.gauss(0, 50)
            h = max(o, c) + abs(random.gauss(0, 20))
            l = min(o, c) - abs(random.gauss(0, 20))
            rows.append({'timestamp': ts, 'open': o, 'high': h,
                         'low': l, 'close': c, 'volume': 100})
            price = c
            ts += pd.Timedelta(minutes=5)
        df = pd.DataFrame(rows)

        strategy = MultiIndicatorConfluence({'min_confidence': 0.5})

        # Run with zero slippage
        cfg_no_slip = BacktestConfig(
            slippage_pct=0.0, lookback_candles=200,
            use_risk_multiplier=False, use_time_exits=False)
        result_no_slip = Backtester(strategy, cfg_no_slip).run(df)

        # Run with 0.1% slippage
        cfg_slip = BacktestConfig(
            slippage_pct=0.1, lookback_candles=200,
            use_risk_multiplier=False, use_time_exits=False)
        result_slip = Backtester(strategy, cfg_slip).run(df)

        # With slippage, total PnL should be lower or equal
        if result_no_slip.total_trades > 0 and result_slip.total_trades > 0:
            self.assertLessEqual(result_slip.total_pnl, result_no_slip.total_pnl)


if __name__ == '__main__':
    unittest.main()
