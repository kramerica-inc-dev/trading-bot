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
    def test_load_config_filters_unknown(self, ):
        import json, tempfile, os
        d = {"instance_name": "x", "m": 2, "network": "testnet", "bogus": 1}
        p = Path(tempfile.mkdtemp()) / "c.json"
        p.write_text(json.dumps(d))
        cfg = R.load_config(str(p))
        self.assertEqual(cfg.m, 2)
        self.assertEqual(cfg.network, "testnet")
        self.assertFalse(hasattr(cfg, "bogus"))


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
