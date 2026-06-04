"""Tests for the cross-sectional momentum paper runner (network-free)."""

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from xs_runner import (  # noqa: E402
    XSRunnerConfig, XSState, Position, XSRunner, resolve_mode,
    compute_target_weights, MODE_DRY, MODE_P2, MODE_P3,
)


class TestModeGate(unittest.TestCase):
    def test_dry_run_default(self):
        self.assertEqual(resolve_mode(XSRunnerConfig()), MODE_DRY)

    def test_p2_demo(self):
        self.assertEqual(resolve_mode(XSRunnerConfig(dry_run=False, okx_demo=True)), MODE_P2)

    def test_live_refused_without_allow(self):
        with self.assertRaises(RuntimeError):
            resolve_mode(XSRunnerConfig(dry_run=False, okx_demo=False, allow_live=False))

    def test_p3_explicit(self):
        self.assertEqual(
            resolve_mode(XSRunnerConfig(dry_run=False, okx_demo=False, allow_live=True)), MODE_P3)


class TestTargetWeights(unittest.TestCase):
    def _closes(self, finals):
        # 200 flat bars then a final move so trailing-120 return == finals[i]
        out = {}
        for sym, f in finals.items():
            cl = np.ones(200)
            cl[-1] = 1.0 + f
            out[sym] = cl
        return out

    def test_dollar_neutral_equal_weight(self):
        finals = {"A": 0.5, "B": 0.4, "C": 0.3, "D": 0.0, "E": -0.3, "F": -0.4, "G": -0.5}
        w = compute_target_weights(self._closes(finals), lookback=120, m=2)
        self.assertAlmostEqual(sum(w.values()), 0.0, places=9)        # dollar-neutral
        self.assertAlmostEqual(sum(abs(v) for v in w.values()), 2.0)  # gross = 2 (2m * 1/m)
        self.assertEqual(w["A"], 0.5); self.assertEqual(w["B"], 0.5)  # top-2 long
        self.assertEqual(w["F"], -0.5); self.assertEqual(w["G"], -0.5)  # bottom-2 short
        self.assertEqual(w["D"], 0.0)

    def test_too_few_assets(self):
        self.assertEqual(compute_target_weights(self._closes({"A": 0.1, "B": -0.1}), 120, 3), {})

    def test_lookahead_free(self):
        # appending FUTURE bars after the window must not change the ranking decision
        finals = {"A": 0.5, "B": 0.1, "C": -0.5, "D": -0.1, "E": 0.3, "F": -0.3}
        w = compute_target_weights(self._closes(finals), lookback=120, m=2)
        self.assertEqual(w["A"], 0.5)   # clear top
        self.assertEqual(w["C"], -0.5)  # clear bottom


class TestRunnerMechanics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        import xs_runner
        self._orig = xs_runner.STATE_ROOT
        xs_runner.STATE_ROOT = Path(self.tmp.name)
        self.cfg = XSRunnerConfig(funding_model="flat", initial_capital=5000.0, m=2,
                                  universe=["A", "B", "C", "D", "E", "F"])
        self.runner = XSRunner(self.cfg)

    def tearDown(self):
        import xs_runner
        xs_runner.STATE_ROOT = self._orig
        self.tmp.cleanup()

    def _closes(self, finals):
        out = {}
        for sym, f in finals.items():
            cl = np.ones(200); cl[-1] = 1.0 + f
            out[sym] = cl
        return out

    def test_rebalance_is_dollar_neutral_and_charges_fee(self):
        s = self.runner.load_state()
        finals = {"A": 0.5, "B": 0.4, "C": 0.0, "D": 0.0, "E": -0.4, "F": -0.5}
        prices = {sym: 100.0 for sym in finals}
        closes = self._closes(finals)
        from datetime import datetime, timezone
        res = self.runner.rebalance(s, closes, prices, datetime.now(timezone.utc))
        self.assertEqual(res["action"], "rebalance")
        self.assertEqual(len(s.positions), 4)                 # 2m
        ln = sum(p.notional for p in s.positions.values() if p.side > 0)
        sn = sum(p.notional for p in s.positions.values() if p.side < 0)
        self.assertAlmostEqual(ln, sn, places=6)              # dollar-neutral
        self.assertGreater(s.fees_paid_total, 0.0)            # turnover fee charged
        self.assertEqual(sorted(res["longs"]), ["A", "B"])
        self.assertEqual(sorted(res["shorts"]), ["E", "F"])

    def test_mark_equity_pnl_sign(self):
        s = self.runner.load_state()
        s.positions = {"A": Position(side=1, notional=1000.0, entry_price=100.0, entered_ts="x"),
                       "B": Position(side=-1, notional=1000.0, entry_price=100.0, entered_ts="x")}
        # A +10%, B +10%: long gains 100, short loses 100 -> net 0
        eq = self.runner.mark_equity(s, {"A": 110.0, "B": 110.0})
        self.assertAlmostEqual(eq, s.cash, places=6)
        # A +10%, B -10%: both gain -> +200
        eq2 = self.runner.mark_equity(s, {"A": 110.0, "B": 90.0})
        self.assertAlmostEqual(eq2, s.cash + 200.0, places=6)

    def test_funding_long_pays_positive_rate(self):
        s = self.runner.load_state()
        s.cash = 5000.0
        s.positions = {"A": Position(side=1, notional=1000.0, entry_price=100.0, entered_ts="x")}
        # flat model: rate>0 -> long pays -> cash decreases (1 day elapsed)
        f = self.runner.apply_funding(s, {"A": 100.0}, 1.0)
        self.assertLess(f, 0.0)
        self.assertLess(s.cash, 5000.0)
        self.assertGreater(s.funding_paid_total, 0.0)

    def test_reconcile_flags_dry_run_mismatch(self):
        s = self.runner.load_state()
        s.dry_run = False                       # cfg.dry_run is True
        rec = self.runner.reconcile(s, {})
        self.assertFalse(rec["ok"])

    def test_state_roundtrip(self):
        s = self.runner.load_state()
        s.positions = {"A": Position(side=-1, notional=500.0, entry_price=42.0, entered_ts="t")}
        s.rebalances_total = 7
        self.runner.save_state(s)
        s2 = self.runner.load_state()
        self.assertEqual(s2.rebalances_total, 7)
        self.assertEqual(s2.positions["A"].side, -1)
        self.assertEqual(s2.positions["A"].entry_price, 42.0)


if __name__ == "__main__":
    unittest.main()
