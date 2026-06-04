"""Tests for the Hyperliquid adapter's pure logic (network-free).

The signing + data paths are proven by the testnet self-test
(`python -m scripts.hl_adapter --selftest`); these cover the safety gate and
response parsing without touching the network.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

try:
    from hl_adapter import (  # noqa: E402
        resolve_hl_mode, _mask, HLAdapter,
        MODE_TESTNET, MODE_MAINNET_DRY, MODE_MAINNET_LIVE,
    )
    HAVE_SDK = True
except ImportError:
    HAVE_SDK = False


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestModeGate(unittest.TestCase):
    def test_testnet(self):
        self.assertEqual(resolve_hl_mode("testnet", False), MODE_TESTNET)
        self.assertEqual(resolve_hl_mode("testnet", True), MODE_TESTNET)

    def test_mainnet_dry_default(self):
        self.assertEqual(resolve_hl_mode("mainnet", False), MODE_MAINNET_DRY)

    def test_mainnet_live_requires_allow(self):
        self.assertEqual(resolve_hl_mode("mainnet", True), MODE_MAINNET_LIVE)

    def test_bad_network(self):
        with self.assertRaises(ValueError):
            resolve_hl_mode("demo", False)


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestMask(unittest.TestCase):
    def test_masks_address(self):
        m = _mask("0x1234567890abcdef1234")
        self.assertTrue(m.startswith("0x1234"))
        self.assertTrue(m.endswith("1234"))
        self.assertIn("…", m)

    def test_short_passthrough(self):
        self.assertEqual(_mask(None), "None")


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestParseOrder(unittest.TestCase):
    def test_filled(self):
        raw = {"status": "ok", "response": {"data": {"statuses": [
            {"filled": {"totalSz": "0.001", "avgPx": "66000.0", "oid": 1}}]}}}
        r = HLAdapter._parse_order(raw)
        self.assertTrue(r.ok)
        self.assertAlmostEqual(r.filled_sz, 0.001)
        self.assertAlmostEqual(r.avg_px, 66000.0)

    def test_status_err(self):
        raw = {"status": "err", "response": "insufficient margin"}
        r = HLAdapter._parse_order(raw)
        self.assertFalse(r.ok)
        self.assertIn("insufficient", r.error)

    def test_resting_no_fill(self):
        raw = {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 7}}]}}}
        r = HLAdapter._parse_order(raw)
        self.assertTrue(r.ok)          # accepted, just not filled yet
        self.assertEqual(r.filled_sz, 0.0)

    def test_order_level_error(self):
        raw = {"status": "ok", "response": {"data": {"statuses": [
            {"error": "Order has invalid size"}]}}}
        r = HLAdapter._parse_order(raw)
        self.assertFalse(r.ok)
        self.assertIn("invalid size", r.error)


if __name__ == "__main__":
    unittest.main()
