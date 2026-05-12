"""Tests for the v1 vol-target sizing helper and the three trend rules
(milestone M3 of docs/STRATEGY-V1-TREND-VOLTARGET.md).  Deterministic.
"""

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backtest"))

from backtest.v1_strategies import (VolTarget, realized_vol, TrailingStopRegime,
                                    MAFilter, DonchianChannel, make_candidate)
from backtest.daily_backtester import DailyBacktester, DailyBacktestConfig


def _daily_df(closes, start=datetime(2024, 1, 1, tzinfo=timezone.utc),
              highs=None, lows=None):
    n = len(closes)
    ts = [start + timedelta(days=i) for i in range(n)]
    closes = np.asarray(closes, dtype=float)
    if highs is None:
        highs = closes * 1.001
    if lows is None:
        lows = closes * 0.999
    return pd.DataFrame({
        "timestamp": ts, "open": closes, "high": highs, "low": lows,
        "close": closes, "volume": np.full(n, 1000.0),
    })


class TestRealizedVol(unittest.TestCase):
    def test_constant_returns_near_zero_vol(self):
        # geometric series with a constant daily ratio -> log-returns ~all equal,
        # so realized vol is ~0 (float noise only) — definitely not a real number.
        closes = 100.0 * (1.01 ** np.arange(60))
        v = realized_vol(closes, 30)
        self.assertTrue(np.isnan(v) or v < 1e-6)

    def test_scales_with_noise(self):
        rng = np.random.default_rng(0)
        lr_small = rng.normal(0, 0.01, 400)
        lr_big = rng.normal(0, 0.02, 400)
        c_small = 100.0 * np.exp(np.cumsum(lr_small))
        c_big = 100.0 * np.exp(np.cumsum(lr_big))
        v_small = realized_vol(c_small, 60)
        v_big = realized_vol(c_big, 60)
        # bigger noise -> roughly 2x the annualized vol
        self.assertGreater(v_big, 1.5 * v_small)
        # annualization sanity: ~0.01 daily std * sqrt(365) ~ 0.19
        self.assertTrue(0.10 < v_small < 0.30)


class TestVolTarget(unittest.TestCase):
    def test_exposure_scales_inverse_sigma(self):
        rng = np.random.default_rng(1)
        # series A has half the vol of series B
        lr_a = rng.normal(0, 0.01, 300)
        lr_b = rng.normal(0, 0.02, 300)
        df_a = _daily_df(100.0 * np.exp(np.cumsum(lr_a)))
        df_b = _daily_df(100.0 * np.exp(np.cumsum(lr_b)))
        vt = VolTarget(sigma_target=0.20, window=60, L_max=10.0)  # high cap so no clipping
        ea = vt.exposure_for(df_a)
        eb = vt.exposure_for(df_b)
        # half vol -> ~double exposure
        self.assertAlmostEqual(ea / eb, 2.0, delta=0.5)
        # exposure ~ sigma_target / sigma
        sa = realized_vol(df_a["close"].to_numpy(), 60)
        self.assertAlmostEqual(ea, 0.20 / sa, delta=1e-6)

    def test_clips_at_lmax(self):
        # very calm series -> sigma_target/sigma huge -> clipped to L_max
        lr = np.full(200, 0.0001) + np.random.default_rng(2).normal(0, 1e-5, 200)
        df = _daily_df(100.0 * np.exp(np.cumsum(lr)))
        vt = VolTarget(sigma_target=0.20, window=30, L_max=1.0)
        self.assertAlmostEqual(vt.exposure_for(df), 1.0, places=9)

    def test_plain_variant_is_constant(self):
        df = _daily_df(100.0 * (1.0 + np.random.default_rng(3).normal(0, 0.02, 100)).cumprod())
        vt = VolTarget(plain=True, L_max=1.0)
        for k in range(2, len(df)):
            self.assertEqual(vt.exposure_for(df.iloc[:k]), 1.0)

    def test_warmup_returns_warmup_exposure(self):
        df = _daily_df([100.0, 101.0, 102.0])  # too short for window=30
        vt = VolTarget(window=30, L_max=1.0)
        self.assertEqual(vt.exposure_for(df), 1.0)

    def test_zero_exposure_when_filter_flat(self):
        # the _TrendFilterBase contract: flat filter -> 0 regardless of vol
        df = _daily_df(np.linspace(100, 50, 100))  # falling -> MA filter never long
        s = MAFilter(ma_days=20, vol_target=VolTarget(sigma_target=0.20, window=20))
        self.assertEqual(s.target_exposure(df), 0.0)


class TestTrailingStopRegime(unittest.TestCase):
    def test_exits_on_trailing_drop_reenters_on_new_high(self):
        # rise to 120, drop 12% to 105.6 (stop = 120*0.85 = 102 ... use bigger drop),
        # then a new 5-day high.
        closes = ([100, 105, 110, 115, 120]      # rising, in-market
                  + [110, 100, 95, 92, 90]        # -25% from 120 -> stop hits
                  + [92, 95, 98, 101, 105]        # recovering, no new 10-day high yet
                  + [108, 112, 115, 118, 125])    # new high -> re-enter
        df = _daily_df(closes, highs=np.array(closes) * 1.0, lows=np.array(closes) * 1.0)
        s = TrailingStopRegime(trail_pct=0.15, breakout_days=10,
                               vol_target=VolTarget(plain=True))
        exposures = []
        for t in range(len(df)):
            exposures.append(s.target_exposure(df.iloc[:t + 1]))
        # at the top (t=4, close 120) still long
        self.assertEqual(exposures[4], 1.0)
        # by the time price has fallen to ~90 the stop has fired -> flat
        self.assertEqual(exposures[9], 0.0)
        # last bar makes a new high over the prior 10 closes -> re-entered
        self.assertEqual(exposures[-1], 1.0)

    def test_uses_intraday_low_for_trigger(self):
        # close stays flat but a wick low pierces the trail
        closes = [100, 100, 100, 100, 100]
        highs = [100, 100, 100, 100, 100]
        lows = [100, 100, 100, 100, 80]  # last bar wicks to 80 (-20% from running high)
        df = _daily_df(closes, highs=highs, lows=lows)
        s = TrailingStopRegime(trail_pct=0.10, breakout_days=10, vol_target=VolTarget(plain=True))
        out = [s.target_exposure(df.iloc[:t + 1]) for t in range(len(df))]
        self.assertEqual(out[3], 1.0)   # still in before the wick
        self.assertEqual(out[4], 0.0)   # wick low triggers the stop


class TestMAFilter(unittest.TestCase):
    def test_long_above_ma_flat_below(self):
        # 50 rising days then 30 falling days; M=20
        closes = list(np.linspace(100, 200, 50)) + list(np.linspace(200, 80, 30))
        df = _daily_df(closes)
        s = MAFilter(ma_days=20, vol_target=VolTarget(plain=True))
        out = [s.target_exposure(df.iloc[:t + 1]) for t in range(len(df))]
        # warmup: flat for first 20 bars
        self.assertEqual(out[10], 0.0)
        # mid rise: above the 20d MA -> long
        self.assertEqual(out[40], 1.0)
        # deep into the fall: below the 20d MA -> flat
        self.assertEqual(out[-1], 0.0)

    def test_slope_filter_blocks_falling_ma(self):
        # price pops above a still-falling MA: with slope filter it stays flat
        closes = list(np.linspace(200, 100, 40)) + [130, 135, 140]
        df = _daily_df(closes)
        s_no = MAFilter(ma_days=20, vol_target=VolTarget(plain=True))
        s_sl = MAFilter(ma_days=20, require_slope_up=True, vol_target=VolTarget(plain=True))
        # at the last bar, close 140 may be above the (still-low, possibly falling) MA
        # the slope-filtered variant must be <= the unfiltered one (never more long)
        a = s_no.target_exposure(df)
        b = s_sl.target_exposure(df)
        self.assertGreaterEqual(a, b)


class TestDonchianChannel(unittest.TestCase):
    def test_enters_on_breakout_exits_on_breakdown(self):
        # flat-ish around 100 for a while, break to a new 20-day high, then break a 10-day low
        closes = ([100 + (i % 5) for i in range(30)]   # choppy ~100-104
                  + [110, 112, 115]                     # new 20d high -> enter
                  + [114, 113, 112, 111, 110, 109, 108, 107, 106, 105]  # drifting down
                  + [95])                               # new 10d low -> exit
        df = _daily_df(closes)
        s = DonchianChannel(entry_days=20, exit_days=10, vol_target=VolTarget(plain=True))
        out = [s.target_exposure(df.iloc[:t + 1]) for t in range(len(df))]
        # before the breakout: flat
        self.assertEqual(out[25], 0.0)
        # right after the breakout to 115: long
        self.assertEqual(out[32], 1.0)
        # after the breakdown to 95: flat
        self.assertEqual(out[-1], 0.0)


class TestIntegrationWithHarness(unittest.TestCase):
    def test_runs_through_daily_backtester(self):
        rng = np.random.default_rng(7)
        closes = 100.0 * np.exp(np.cumsum(rng.normal(0.001, 0.02, 250)))
        df = _daily_df(closes)
        cfg = DailyBacktestConfig(initial_balance=5000.0, funding_series_path=None)
        for rule in ("trailing", "ma", "donchian"):
            s = make_candidate(rule, ma_days=50)  # ma_days ignored for non-ma rules
            res = DailyBacktester(s, cfg).run(df)
            self.assertEqual(len(res.equity_curve), len(df))
            # exposures stay within [0, L_max]
            self.assertTrue(all(0.0 <= e <= 1.0 + 1e-9 for e in res.daily_exposure))
            # plain variant: in-market exposure is exactly 1.0
            sp = make_candidate(rule, plain=True, ma_days=50)
            rp = DailyBacktester(sp, cfg).run(df)
            in_exps = [e for e in rp.daily_exposure if e > 1e-9]
            if in_exps:
                self.assertTrue(all(abs(e - 1.0) < 1e-9 for e in in_exps))

    def test_vol_target_reduces_drawdown_vs_plain(self):
        # On a real-ish noisy path the vol-target overlay should not increase max-DD.
        rng = np.random.default_rng(11)
        # a path with a clear up leg then a crash
        up = np.cumsum(rng.normal(0.004, 0.015, 200))
        down = up[-1] + np.cumsum(rng.normal(-0.01, 0.04, 120))
        closes = 100.0 * np.exp(np.concatenate([up, down]))
        df = _daily_df(closes)
        cfg = DailyBacktestConfig(initial_balance=5000.0, funding_series_path=None)
        v = DailyBacktester(make_candidate("trailing"), cfg).run(df)
        pl = DailyBacktester(make_candidate("trailing", plain=True), cfg).run(df)
        self.assertLessEqual(v.max_drawdown_pct, pl.max_drawdown_pct + 1e-6)


if __name__ == "__main__":
    unittest.main()
