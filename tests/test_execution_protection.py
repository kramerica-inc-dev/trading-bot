"""Tests for mandatory server-side TP/SL and partial fill protection (B8/B9)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

from trading_bot import TradingBot


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
    }
    if overrides:
        config.update(overrides)
    (base_dir / 'config.json').write_text(json.dumps(config))
    return config


class MandatoryTPSLTests(unittest.TestCase):
    """Server-side TP/SL must be enforced when require_server_side_tpsl=True."""

    @patch('trading_bot.create_exchange_adapter')
    def test_protection_config_defaults_to_required(self, mock_create):
        """Default config should have require_server_side_tpsl=True."""
        mock_api = MagicMock()
        mock_create.return_value = mock_api

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _make_test_config(base)
            bot = TradingBot(str(base / 'config.json'))
            self.assertTrue(bot.protection.get('use_server_side_tpsl'))
            self.assertTrue(bot.protection.get('require_server_side_tpsl'))

    @patch('trading_bot.create_exchange_adapter')
    def test_protection_can_be_disabled(self, mock_create):
        """Explicit False should disable requirement."""
        mock_api = MagicMock()
        mock_create.return_value = mock_api

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _make_test_config(base, {
                'protection': {
                    'use_server_side_tpsl': False,
                    'require_server_side_tpsl': False,
                }
            })
            bot = TradingBot(str(base / 'config.json'))
            self.assertFalse(bot.protection.get('require_server_side_tpsl'))


class PartialFillSizeVerificationTests(unittest.TestCase):
    """Partial fill handling should verify position size matches filled size."""

    @patch('trading_bot.create_exchange_adapter')
    def test_bot_has_apply_partial_fill_method(self, mock_create):
        """Bot should have _apply_partial_fill method."""
        mock_api = MagicMock()
        mock_create.return_value = mock_api

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _make_test_config(base)
            bot = TradingBot(str(base / 'config.json'))
            self.assertTrue(hasattr(bot, '_apply_partial_fill'))
            self.assertTrue(callable(getattr(bot, '_apply_partial_fill')))


if __name__ == '__main__':
    unittest.main()
