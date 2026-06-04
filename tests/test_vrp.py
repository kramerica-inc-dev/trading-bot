"""Tests for the VRP feasibility module (data-independent: pure functions)."""

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "backtest"))

from sweep.vrp import _forward_rv, _monthly_pnls, VRPConfig  # noqa: E402


class TestForwardRV(unittest.TestCase):

    def test_constant_prices_zero_vol(self):
        close = np.ones(60) * 100.0
        self.assertAlmostEqual(_forward_rv(close, 0, 30), 0.0, places=6)

    def test_volatile_prices_positive_vol(self):
        rng = np.random.default_rng(0)
        close = 100.0 * np.cumprod(1 + rng.normal(0, 0.03, 60))
        self.assertGreater(_forward_rv(close, 0, 30), 0.1)


class TestMonthlyPnls(unittest.TestCase):

    def _df(self, dvol, close):
        n = len(close)
        dates = pd.date_range("2023-01-01", periods=n, freq="D", tz="UTC")
        return pd.DataFrame({"date": dates, "dvol": dvol, "close": close})

    def test_high_iv_calm_market_earns_premium(self):
        # IV pinned high (60%), market calm (low realized) -> short vol profits
        rng = np.random.default_rng(1)
        close = 100.0 * np.cumprod(1 + rng.normal(0, 0.005, 400))  # ~9% ann vol
        m = self._df(60.0, close)
        pnls = _monthly_pnls(m, VRPConfig(cost_volpts=0.0))
        self.assertGreater(len(pnls), 5)
        self.assertGreater(np.mean(pnls), 0.0)   # IV(60) >> RV(~9) -> positive

    def test_iv_equals_rv_no_premium(self):
        # IV set to ~realized -> premium ~0 (minus nothing, cost 0)
        rng = np.random.default_rng(2)
        rets = rng.normal(0, 0.03, 400)            # ~57% ann vol
        close = 100.0 * np.cumprod(1 + rets)
        ann = np.std(rets, ddof=1) * np.sqrt(365) * 100
        m = self._df(ann, close)
        pnls = _monthly_pnls(m, VRPConfig(cost_volpts=0.0))
        self.assertLess(abs(np.mean(pnls)), 15.0)   # roughly centered, no big edge


if __name__ == "__main__":
    unittest.main()
