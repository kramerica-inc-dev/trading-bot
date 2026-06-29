"""Tests for the DIAGNOSTIC regime tag: causal/deterministic labeling, the
favorability mapping, the insufficient-data path, and — critically — a
STRUCTURAL assertion that the tag never touches the trading path (observability
only; regime-gating is the closed dead lane per DECISIONS.md 2026-06-10).
"""

import ast
import os
import sys
import unittest

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import regime_tag as R  # noqa: E402


def _mk(rets, n=40, w=30):
    """Build a closes dict where each coin's trailing-w return equals rets[coin]
    (only the two endpoints arr[-1], arr[-1-w] matter to the labeler)."""
    closes = {}
    for coin, r in rets.items():
        a = np.full(n, 100.0)
        a[-1 - w] = 100.0
        a[-1] = 100.0 * (1.0 + r)
        closes[coin] = a
    return closes


class TestLabeler(unittest.TestCase):
    def test_bull_high_dispersion_is_strong(self):
        closes = _mk({"BTC": 0.10, "A": 0.6, "B": 0.4, "C": -0.4, "D": -0.6, "E": 0.0})
        out = R.compute_regime(closes)
        self.assertEqual(out["trend"], "bull_trend")
        self.assertEqual(out["dispersion"], "high_disp")
        self.assertEqual(out["label"], "bull_trend|high_disp")
        self.assertEqual(out["favorability"], "strong")
        self.assertTrue(out["in_distribution"])

    def test_chop_high_dispersion_is_adverse(self):
        closes = _mk({"BTC": 0.0, "A": 0.6, "B": 0.4, "C": -0.4, "D": -0.6, "E": 0.0})
        out = R.compute_regime(closes)
        self.assertEqual(out["label"], "chop|high_disp")
        self.assertEqual(out["favorability"], "adverse")
        self.assertFalse(out["in_distribution"])

    def test_bear_low_dispersion_is_neutral(self):
        closes = _mk({"BTC": -0.10, "A": -0.09, "B": -0.11, "C": -0.10, "D": -0.08, "E": -0.12})
        out = R.compute_regime(closes)
        self.assertEqual(out["trend"], "bear_trend")
        self.assertEqual(out["dispersion"], "low_disp")
        self.assertEqual(out["favorability"], "neutral")

    def test_insufficient_data_is_unknown(self):
        out = R.compute_regime({"BTC": np.array([100.0, 101.0])})   # < trend_window
        self.assertEqual(out["label"], "unknown")
        self.assertFalse(out["in_distribution"])

    def test_deterministic(self):
        closes = _mk({"BTC": 0.10, "A": 0.6, "B": -0.4, "C": 0.1, "D": -0.2, "E": 0.3})
        self.assertEqual(R.compute_regime(closes), R.compute_regime(closes))

    def test_favorability_map_grounded(self):
        # the powerhouse and the danger bucket must be labeled as the data showed
        self.assertEqual(R.REGIME_FAVORABILITY["bull_trend|high_disp"], "strong")
        self.assertEqual(R.REGIME_FAVORABILITY["chop|high_disp"], "adverse")


class TestObservabilityOnly(unittest.TestCase):
    """The regime tag must be READ-ONLY w.r.t. trading: it may appear only in the
    health path, never in any method that computes targets / exposure / orders."""

    TRADING_METHODS = {"_targets", "_apply_exposure_caps", "_execute_live", "_execute_sim",
                       "_maybe_delever", "_resize_order", "flatten_all", "_should_rebalance",
                       "reconcile", "_apply_circuit_breaker"}

    def test_regime_absent_from_trading_path(self):
        src = open(os.path.join(ROOT, "scripts", "hl_xs_runner.py")).read()
        tree = ast.parse(src)
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in self.TRADING_METHODS:
                seg = ast.get_source_segment(src, node) or ""
                if "regime" in seg.lower():
                    offenders.append(node.name)
        self.assertEqual(offenders, [], f"regime tag leaked into trading path: {offenders}")

    def test_regime_only_read_in_health(self):
        # _last_regime may be assigned in _update_regime_tag and read in _health_payload only
        src = open(os.path.join(ROOT, "scripts", "hl_xs_runner.py")).read()
        tree = ast.parse(src)
        readers = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and "_last_regime" in (ast.get_source_segment(src, node) or ""):
                readers.append(node.name)
        self.assertEqual(set(readers), {"__init__", "_update_regime_tag", "_health_payload"},
                         f"_last_regime touched outside health path: {readers}")


if __name__ == "__main__":
    unittest.main()
