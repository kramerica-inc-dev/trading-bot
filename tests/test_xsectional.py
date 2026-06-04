"""Tests for the cross-sectional momentum candidate (sweep wave 1)."""

import os
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "backtest"))

from sweep.xsectional import (  # noqa: E402
    XSConfig, _portfolio_returns, _total_return_pct, _sharpe, load_panel,
)


def _panel_with_one_trender(n=400, k=5, seed=5) -> np.ndarray:
    """Asset 0 trends up strongly and quietly; the rest are flat noise.

    With m=1 (long the top, short the bottom) momentum reliably longs the clear
    trender → strongly positive, beating the average random dollar-neutral
    basket (which longs the trender only 1/k of the time)."""
    rng = np.random.default_rng(seed)
    cols = []
    for j in range(k):
        drift, sigma = (0.02, 0.003) if j == 0 else (0.0, 0.01)
        rets = rng.normal(drift, sigma, n)
        cols.append(100.0 * np.cumprod(1.0 + rets))
    return np.column_stack(cols)


class TestPortfolioReturns(unittest.TestCase):

    def test_shape(self):
        closes = _panel_with_one_trender()
        cfg = XSConfig(lookback=30, rebal=5, m=2)
        port = _portfolio_returns(closes, cfg, selection="momentum")
        self.assertEqual(len(port), len(closes) - 1)

    def test_momentum_beats_random_on_trender(self):
        closes = _panel_with_one_trender()
        cfg = XSConfig(lookback=20, rebal=5, m=1, cost_rate=0.0)
        mom = _total_return_pct(_portfolio_returns(closes, cfg, selection="momentum"))
        rng = np.random.default_rng(0)
        rand = np.mean([
            _total_return_pct(_portfolio_returns(closes, cfg, selection="random", rng=rng))
            for _ in range(50)
        ])
        self.assertGreater(mom, rand)

    def test_sharpe_zero_on_flat(self):
        self.assertEqual(_sharpe(np.zeros(100)), 0.0)


class TestLoadPanel(unittest.TestCase):

    def test_missing_dir_returns_empty(self):
        df = load_panel(["BTC-USDT"], data_dir="/nonexistent/path/xyz")
        self.assertTrue(df.empty)


if __name__ == "__main__":
    unittest.main()
