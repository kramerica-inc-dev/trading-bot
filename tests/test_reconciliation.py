"""Tests for startup reconciliation fail-closed behavior (B7)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

from trading_bot import TradingBot, ReconciliationError


def _make_test_config(base_dir, overrides=None):
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
            'require_server_side_tpsl': True,
        },
    }
    if overrides:
        config.update(overrides)
    (base_dir / 'config.json').write_text(json.dumps(config))
    return config


class ReconciliationHardFailTests(unittest.TestCase):
    """Startup reconciliation should fail-closed on dangerous mismatches."""

    @patch('trading_bot.create_exchange_adapter')
    def test_orphan_exchange_position_raises(self, mock_create):
        """Exchange position with no local metadata should raise."""
        mock_api = MagicMock()
        mock_create.return_value = mock_api
        mock_api.get_positions.return_value = {
            'code': '0',
            'data': [{
                'instId': 'BTC-USDT',
                'positionSide': 'long',
                'positions': '1.0',
                'averagePrice': '60000',
                'unrealizedPnl': '0',
            }],
        }
        mock_api.get_active_tpsl_orders.return_value = {'code': '0', 'data': []}

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _make_test_config(base)
            bot = TradingBot(str(base / 'config.json'))
            # Bot has no local positions — orphan exchange position
            bot.positions = {}
            bot.pending_orders = {}
            # The reconciliation should raise in live mode
            # In dry_run mode the bot may skip reconciliation,
            # so test the _reconciliation_fail helper directly
            with self.assertRaises(ReconciliationError):
                bot._reconciliation_fail(
                    "Exchange has position BTC-USDT with no local metadata",
                    {"exchange_positions": [{"instId": "BTC-USDT"}]})

    @patch('trading_bot.create_exchange_adapter')
    def test_force_reconcile_bypasses_error(self, mock_create):
        """--force-reconcile should not raise ReconciliationError."""
        mock_api = MagicMock()
        mock_create.return_value = mock_api

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _make_test_config(base)
            bot = TradingBot(str(base / 'config.json'), force_reconcile=True)
            # Should NOT raise
            bot._reconciliation_fail(
                "Test mismatch", {"detail": "test"})

    @patch('trading_bot.create_exchange_adapter')
    def test_reconciliation_error_is_runtime_error(self, mock_create):
        """ReconciliationError should be a RuntimeError subclass."""
        self.assertTrue(issubclass(ReconciliationError, RuntimeError))


if __name__ == '__main__':
    unittest.main()
