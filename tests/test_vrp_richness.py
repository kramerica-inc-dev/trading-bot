"""Tests for the VRP DVOL-richness filter — focus on the look-ahead-free property."""

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backtest"))

from sweep.vrp_richness import (  # noqa: E402
    causal_trailing_mult, subset_null_percentile, matched_tail_ann, _sharpe,
)


def _m(dvol):
    n = len(dvol)
    dates = pd.date_range("2022-01-01", periods=n, freq="D", tz="UTC")
    return pd.DataFrame({"date": dates, "dvol": dvol, "close": np.full(n, 100.0)})


class TestCausalNoLookahead(unittest.TestCase):

    def test_mult_depends_only_on_past(self):
        rng = np.random.default_rng(0)
        dvol = 50 + rng.normal(0, 10, 400)
        m = _m(dvol)
        entry_idx = np.arange(60, 400, 30)
        base = causal_trailing_mult(m, entry_idx, lookback=120, pctl=50.0)
        # mutate DVOL strictly AFTER the 3rd entry; multipliers up to & incl. it must not change
        k = 3
        m2 = m.copy()
        m2.loc[m2.index > entry_idx[k], "dvol"] += 999.0
        after = causal_trailing_mult(m2, entry_idx, lookback=120, pctl=50.0)
        np.testing.assert_array_equal(base[: k + 1], after[: k + 1])

    def test_high_dvol_entry_trades_low_stands_aside(self):
        # trailing DVOL flat at 50; an entry at 70 trades, an entry at 30 stands aside
        dvol = np.full(200, 50.0)
        dvol[150] = 70.0
        dvol[180] = 30.0
        m = _m(dvol)
        mult = causal_trailing_mult(m, np.array([150, 180]), lookback=120, pctl=50.0, size_low=0.0)
        self.assertEqual(mult[0], 1.0)   # 70 >= trailing median 50
        self.assertEqual(mult[1], 0.0)   # 30 < trailing median 50

    def test_warmup_defaults_full_size(self):
        dvol = np.full(100, 50.0)
        m = _m(dvol)
        mult = causal_trailing_mult(m, np.array([10]), lookback=120, pctl=50.0, min_hist=60)
        self.assertEqual(mult[0], 1.0)   # only 10 days of history < min_hist -> full size


class TestSubsetNull(unittest.TestCase):

    def test_random_mask_is_not_significant_on_average(self):
        # A single random subset's percentile is itself ~uniform, so average
        # over many random masks: the mean percentile must sit near 50.
        rng = np.random.default_rng(1)
        pnl = rng.normal(5, 20, 43)
        pcts = []
        for _ in range(40):
            mask = np.zeros(43, bool); mask[rng.choice(43, 20, replace=False)] = True
            pct, _ = subset_null_percentile(pnl, mask, reps=1000, seed=int(rng.integers(1e9)))
            pcts.append(pct)
        self.assertTrue(35 < np.mean(pcts) < 65)   # random selection has no skill

    def test_top_subset_is_significant(self):
        pnl = np.arange(43, dtype=float)          # monotone
        mask = pnl >= 23                            # the top ~half
        pct, _ = subset_null_percentile(pnl, mask, reps=2000)
        self.assertGreater(pct, 95.0)


class TestMatchedTail(unittest.TestCase):

    def test_sizing_hits_budget(self):
        rng = np.random.default_rng(2)
        pnl = rng.normal(300, 1500, 43)
        out = matched_tail_ann(pnl, cvar_budget_pct=10.0, capital=100_000.0)
        self.assertIsNotNone(out["ann_pct"])
        self.assertLess(out["worst_month_pct"], 0.0)


if __name__ == "__main__":
    unittest.main()
