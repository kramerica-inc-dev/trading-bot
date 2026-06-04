"""Tests for the sweep feasibility harness (M2).

Pure component logic is tested deterministically; the full stochastic
`evaluate` is smoke-tested on synthetic data with small reps (a noise candidate
must never ADVANCE, and the verdict structure must be well-formed).
"""

import os
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "backtest"))

from sweep_feasibility import (  # noqa: E402
    Candidate, FeasibilityVerdict, PrecomputedExposureStrategy,
    decide_verdict, evaluate, _signal_ic, _tim_and_hold, _avg_in_exposure,
)
from sweep.directional import (  # noqa: E402
    _threshold_long, _channel_state, build_candidates,
)


def _synthetic_daily(n=400, seed=7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0005, 0.03, n)
    close = 20000.0 * np.cumprod(1.0 + rets)
    ts = pd.date_range("2023-01-01", periods=n, freq="D", tz="UTC")
    return pd.DataFrame({"timestamp": ts, "open": close, "high": close * 1.01,
                         "low": close * 0.99, "close": close, "volume": 1000.0})


class TestDecideVerdict(unittest.TestCase):

    def test_void_dominates(self):
        # sham not failing -> VOID regardless of the other checks
        self.assertEqual(decide_verdict(True, True, True, False), "VOID")
        self.assertEqual(decide_verdict(False, False, False, False), "VOID")

    def test_advance_requires_all(self):
        self.assertEqual(decide_verdict(True, True, True, True), "ADVANCE")

    def test_kill_on_any_fail(self):
        self.assertEqual(decide_verdict(False, True, True, True), "KILL")
        self.assertEqual(decide_verdict(True, False, True, True), "KILL")
        self.assertEqual(decide_verdict(True, True, False, True), "KILL")


class TestTimAndHold(unittest.TestCase):

    def test_all_in(self):
        tim, hold = _tim_and_hold([1.0, 1.0, 1.0, 1.0])
        self.assertAlmostEqual(tim, 1.0)
        self.assertAlmostEqual(hold, 4.0)

    def test_alternating(self):
        tim, hold = _tim_and_hold([1.0, 0.0, 1.0, 0.0])
        self.assertAlmostEqual(tim, 0.5)
        self.assertAlmostEqual(hold, 1.0)

    def test_flat(self):
        tim, hold = _tim_and_hold([0.0, 0.0])
        self.assertAlmostEqual(tim, 0.0)


class TestSignalIC(unittest.TestCase):

    def test_perfect_predictor(self):
        df = _synthetic_daily()
        h = 5
        c = df["close"]
        perfect = (c.shift(-h) / c - 1.0)        # equals the forward return
        rho, p = _signal_ic(perfect, df, h)
        self.assertGreater(rho, 0.95)
        self.assertLess(p, 1e-6)

    def test_noise_has_low_ic(self):
        df = _synthetic_daily()
        rng = np.random.default_rng(1)
        noise = pd.Series(rng.normal(0, 1, len(df)))
        rho, p = _signal_ic(noise, df, 5)
        self.assertLess(abs(rho), 0.2)


class TestExposureMappings(unittest.TestCase):

    def test_threshold_long(self):
        s = pd.Series([-1.0, 0.0, 0.5, np.nan])
        e = _threshold_long(s, 0.0)
        self.assertEqual(list(e), [0.0, 0.0, 1.0, 0.0])

    def test_channel_state_hysteresis(self):
        # enters at >=0.95, stays until <=0.25
        s = pd.Series([0.5, 0.96, 0.6, 0.3, 0.2, 0.9, 0.96])
        e = _channel_state(s, enter=0.95, exit_=0.25)
        self.assertEqual(list(e), [0.0, 1.0, 1.0, 1.0, 0.0, 0.0, 1.0])

    def test_precomputed_strategy(self):
        strat = PrecomputedExposureStrategy([0.0, 0.5, 1.0])
        self.assertEqual(strat.target_exposure(pd.DataFrame({"a": [1]})), 0.0)
        self.assertEqual(strat.target_exposure(pd.DataFrame({"a": [1, 2, 3]})), 1.0)


class TestEvaluateSmoke(unittest.TestCase):

    def test_noise_candidate_never_advances(self):
        df = _synthetic_daily(n=500)
        rng = np.random.default_rng(3)
        noise_sig = pd.Series(rng.normal(0, 1, len(df)))
        cand = Candidate(
            name="noise",
            signal_fn=lambda d: noise_sig,
            exposure_fn=lambda s, d: _threshold_long(s, 0.0),
            ic_horizon=5, expected_sign=1, thesis="pure noise",
        )
        v = evaluate(cand, df, reps=40, n_sham=2, seed=11)
        self.assertIsInstance(v, FeasibilityVerdict)
        self.assertIn(v.verdict, ("KILL", "VOID"))   # noise must never ADVANCE
        for key in ("net_roi_pct", "null_percentile", "time_in_market"):
            self.assertIn(key, v.metrics)

    def test_registry_builds(self):
        cands = build_candidates()
        self.assertTrue(any(c.name == "trend_sma200" for c in cands))
        self.assertTrue(any(not c.directional for c in cands))  # the BH benchmark


if __name__ == "__main__":
    unittest.main()
