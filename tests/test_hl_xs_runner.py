"""Tests for the Hyperliquid momentum runner's pure logic (network-free).

The full loop (data + execution) is proven by the MAINNET_DRY integration run
and the testnet _execute_live proof; these cover target-building and the
simulated rebalance accounting without constructing the (networked) adapter.
"""

import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

try:
    import hl_xs_runner as R
    from xs_runner import XSState
    HAVE_SDK = True
except ImportError:
    HAVE_SDK = False


def _closes(finals):
    out = {}
    for sym, f in finals.items():
        cl = np.ones(200); cl[-1] = 1.0 + f
        out[sym] = cl
    return out


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestConfig(unittest.TestCase):
    def _write(self, d):
        import json, tempfile
        p = Path(tempfile.mkdtemp()) / "c.json"
        p.write_text(json.dumps(d))
        return str(p)

    def test_load_config_filters_unknown(self):
        cfg = R.load_config(self._write({"instance_name": "x", "m": 2, "network": "testnet", "bogus": 1}))
        self.assertEqual(cfg.m, 2)
        self.assertEqual(cfg.network, "testnet")
        self.assertFalse(hasattr(cfg, "bogus"))

    def test_load_config_rejects_string_allow_live(self):
        # the critical guard: a JSON-string boolean must NOT be accepted
        with self.assertRaises(ValueError):
            R.load_config(self._write({"network": "mainnet", "allow_live": "false"}))

    def test_load_config_rejects_bad_network(self):
        with self.assertRaises(ValueError):
            R.load_config(self._write({"network": "demo"}))


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestVerifyBook(unittest.TestCase):
    def _stub(self, book):
        cfg = R.HLXSConfig(m=2)
        adapter = types.SimpleNamespace(book_notional=lambda: book, MIN_ORDER_USD=10.0)
        return types.SimpleNamespace(cfg=cfg, adapter=adapter)

    def test_balanced_book_ok(self):
        stub = self._stub({"A": 1000.0, "B": 1000.0, "C": -1000.0, "D": -1000.0})
        ok, missing, _ = R.HLXSRunner._verify_book(stub, {"A": 1000.0, "B": 1000.0, "C": -1000.0, "D": -1000.0})
        self.assertTrue(ok)
        self.assertEqual(missing, {})

    def test_one_legged_book_fails(self):
        stub = self._stub({"A": 1000.0, "B": 1000.0})   # shorts never filled
        ok, missing, _ = R.HLXSRunner._verify_book(stub, {"A": 1000.0, "B": 1000.0, "C": -1000.0, "D": -1000.0})
        self.assertFalse(ok)
        self.assertEqual(sorted(missing), ["C", "D"])

    def test_size_skewed_book_fails(self):
        # count-balanced 2L/2S but shorts tiny -> not dollar-neutral
        stub = self._stub({"A": 1000.0, "B": 1000.0, "C": -100.0, "D": -100.0})
        ok, _, _ = R.HLXSRunner._verify_book(stub, {"A": 1000.0, "B": 1000.0, "C": -1000.0, "D": -1000.0})
        self.assertFalse(ok)


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestTargets(unittest.TestCase):
    def _stub(self, **over):
        cfg = R.HLXSConfig(lookback_days=120, m=2, gross_exposure=1.0,
                           cost_rate=0.0005, **over)
        return types.SimpleNamespace(cfg=cfg, mode="MAINNET_DRY")

    def test_targets_dollar_neutral(self):
        stub = self._stub()
        finals = {"A": 0.5, "B": 0.4, "C": 0.0, "D": -0.4, "E": -0.5}
        t = R.HLXSRunner._targets(stub, _closes(finals), 5000.0)
        self.assertAlmostEqual(sum(t.values()), 0.0, places=6)          # dollar-neutral
        # gross = 2m * (1/m) * gross_book = 2 * equity * gross_exposure (1x per side)
        self.assertAlmostEqual(sum(abs(v) for v in t.values()), 10000.0, places=3)
        self.assertEqual(sorted(k for k, v in t.items() if v > 0), ["A", "B"])
        self.assertEqual(sorted(k for k, v in t.items() if v < 0), ["D", "E"])


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestSimExecution(unittest.TestCase):
    def _stub(self):
        cfg = R.HLXSConfig(m=2, cost_rate=0.0005)
        return types.SimpleNamespace(cfg=cfg, mode="MAINNET_DRY")

    def test_sim_rebalance_dollar_neutral_and_fee(self):
        stub = self._stub()
        s = XSState(cash=5000.0, equity=5000.0, peak_equity=5000.0)
        targets = {"A": 1250.0, "B": 1250.0, "C": -1250.0, "D": -1250.0}
        mids = {k: 100.0 for k in "ABCD"}
        res = R.HLXSRunner._execute_sim(stub, s, targets, mids, datetime.now(timezone.utc))
        self.assertEqual(res["execution"], "sim")
        self.assertEqual(len(s.positions), 4)
        ln = sum(p.notional for p in s.positions.values() if p.side > 0)
        sn = sum(p.notional for p in s.positions.values() if p.side < 0)
        self.assertAlmostEqual(ln, sn, places=6)            # dollar-neutral
        self.assertGreater(s.fees_paid_total, 0.0)          # turnover fee
        self.assertEqual(sorted(res["longs"]), ["A", "B"])
        self.assertEqual(sorted(res["shorts"]), ["C", "D"])

    def test_sim_keeps_same_side_positions(self):
        from xs_runner import Position
        stub = self._stub()
        s = XSState(cash=5000.0, equity=5000.0, peak_equity=5000.0)
        s.positions = {"A": Position(side=1, notional=1250.0, entry_price=100.0, entered_ts="x")}
        # A stays long in the new target -> should NOT be re-traded (no churn)
        targets = {"A": 1250.0, "B": 1250.0, "C": -1250.0, "D": -1250.0}
        mids = {k: 100.0 for k in "ABCD"}
        R.HLXSRunner._execute_sim(stub, s, targets, mids, datetime.now(timezone.utc))
        self.assertEqual(s.positions["A"].entry_price, 100.0)   # untouched


if __name__ == "__main__":
    unittest.main()
