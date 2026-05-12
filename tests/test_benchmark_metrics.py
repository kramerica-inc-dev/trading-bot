"""Fase 1 — tests for the BTC buy-and-hold benchmark and risk-adjusted metrics.

Pure measurement: these exercise the benchmark / regime / conditional-metric
helpers in backtest.backtester plus the BacktestResult wiring.  Deterministic —
no randomness, no network.
"""

import math
import unittest
from datetime import datetime, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.backtester import (
    BacktestConfig,
    BacktestResult,
    classify_regimes,
    compute_benchmark,
    conditional_metrics,
)


def _daily_ts(n, start=datetime(2024, 1, 1)):
    return [start + timedelta(days=i) for i in range(n)]


class ComputeBenchmarkTests(unittest.TestCase):
    def test_pure_bull_period(self):
        # Monotone rise: +50% total, no drawdown.
        closes = [100.0 * (1.0 + 0.05 * i) for i in range(11)]  # 100 .. 150
        ts = _daily_ts(11)
        b = compute_benchmark(closes, ts, initial_balance=1000.0)
        self.assertAlmostEqual(b['total_return_pct'], 50.0, places=6)
        self.assertEqual(b['max_drawdown_pct'], 0.0)
        self.assertEqual(b['max_dd_duration_bars'], 0)
        self.assertEqual(b['time_under_water_pct'], 0.0)
        self.assertAlmostEqual(b['final_balance'], 1500.0, places=6)
        self.assertGreater(b['cagr'], 0.0)
        self.assertEqual(len(b['equity_curve']), len(closes))
        self.assertAlmostEqual(b['equity_curve'][0], 1000.0, places=6)

    def test_pure_bear_period(self):
        # Monotone decline: -40% total, fully underwater after bar 0.
        closes = [100.0 * (1.0 - 0.04 * i) for i in range(11)]  # 100 .. 60
        ts = _daily_ts(11)
        b = compute_benchmark(closes, ts, initial_balance=1000.0)
        self.assertAlmostEqual(b['total_return_pct'], -40.0, places=6)
        self.assertAlmostEqual(b['max_drawdown_pct'], 40.0, places=6)
        self.assertEqual(b['max_dd_duration_bars'], 10)  # bars 1..10 underwater
        self.assertAlmostEqual(b['time_under_water_pct'], 1000.0 / 11.0, places=6)
        self.assertLess(b['cagr'], 0.0)

    def test_flat_period(self):
        closes = [100.0] * 11
        ts = _daily_ts(11)
        b = compute_benchmark(closes, ts, initial_balance=500.0)
        self.assertAlmostEqual(b['total_return_pct'], 0.0, places=6)
        self.assertEqual(b['max_drawdown_pct'], 0.0)
        self.assertEqual(b['max_dd_duration_bars'], 0)
        self.assertEqual(b['time_under_water_pct'], 0.0)
        self.assertAlmostEqual(b['final_balance'], 500.0, places=6)
        self.assertEqual(b['cagr'], 0.0)

    def test_single_candle(self):
        b = compute_benchmark([42000.0], [datetime(2024, 1, 1)], initial_balance=100.0)
        self.assertEqual(b['total_return_pct'], 0.0)
        self.assertEqual(b['max_drawdown_pct'], 0.0)
        self.assertEqual(b['cagr'], 0.0)
        self.assertEqual(b['equity_curve'], [100.0])
        self.assertAlmostEqual(b['final_balance'], 100.0, places=6)

    def test_empty_closes(self):
        b = compute_benchmark([], [], initial_balance=100.0)
        self.assertEqual(b['total_return_pct'], 0.0)
        self.assertEqual(b['equity_curve'], [])
        self.assertEqual(b['final_balance'], 100.0)

    def test_no_lookahead_prefix_invariance(self):
        # Benchmark equity at index i must depend only on closes[:i+1]; so a
        # longer series with the same prefix yields the same prefix equity.
        prefix = [100.0, 102.0, 99.0, 105.0]
        full = prefix + [120.0, 80.0, 130.0]
        ts_short = _daily_ts(len(prefix))
        ts_full = _daily_ts(len(full))
        b_short = compute_benchmark(prefix, ts_short, 1000.0)
        b_full = compute_benchmark(full, ts_full, 1000.0)
        for i in range(len(prefix)):
            self.assertAlmostEqual(b_short['equity_curve'][i],
                                   b_full['equity_curve'][i], places=9)


class ClassifyRegimesTests(unittest.TestCase):
    def test_bull_bear_sideways(self):
        # window_days=2 on daily bars => 2-bar trailing window; threshold 5%.
        closes = [100, 101, 102, 103, 115, 113, 112, 90, 85, 80]
        ts = _daily_ts(len(closes))
        r = classify_regimes(closes, ts, window_days=2, threshold_pct=5.0)
        self.assertEqual(len(r), len(closes))
        self.assertEqual(r[0], 'sideways')          # first bar always sideways
        self.assertEqual(r[1], 'sideways')          # +1% over 1 bar
        self.assertEqual(r[2], 'sideways')          # +2% over 2 bars
        self.assertEqual(r[4], 'bull')              # 115 vs 103: +11.6%
        self.assertEqual(r[7], 'bear')              # 90 vs 112: -19.6%
        self.assertEqual(r[9], 'bear')              # 80 vs 90: -11.1%

    def test_pure_bull_all_bull_after_window(self):
        closes = [100.0 * (1.0 + 0.05 * i) for i in range(20)]
        ts = _daily_ts(20)
        r = classify_regimes(closes, ts, window_days=3, threshold_pct=5.0)
        # 3-bar trailing return of a +5%/bar series ~ +15.7% > 5% => bull.
        self.assertTrue(all(x == 'bull' for x in r[3:]))

    def test_pure_bear_all_bear_after_window(self):
        closes = [100.0 * (1.0 - 0.04 * i) for i in range(20)]
        ts = _daily_ts(20)
        r = classify_regimes(closes, ts, window_days=3, threshold_pct=5.0)
        self.assertTrue(all(x == 'bear' for x in r[3:]))

    def test_flat_all_sideways(self):
        closes = [100.0] * 20
        ts = _daily_ts(20)
        r = classify_regimes(closes, ts, window_days=3, threshold_pct=5.0)
        self.assertTrue(all(x == 'sideways' for x in r))

    def test_empty(self):
        self.assertEqual(classify_regimes([], []), [])

    def test_single(self):
        self.assertEqual(classify_regimes([100.0], [datetime(2024, 1, 1)]),
                         ['sideways'])


class ConditionalMetricsTests(unittest.TestCase):
    def test_segments_split_correctly(self):
        ts = _daily_ts(6)
        # equity rises, rises, falls, falls, rises -> 5 per-bar returns.
        equity = [1000.0, 1100.0, 1210.0, 1100.0, 1000.0, 1050.0]
        regimes = ['sideways', 'bull', 'bull', 'bear', 'bear', 'sideways']
        cm = conditional_metrics(equity, ts, regimes)
        self.assertEqual(set(cm.keys()), {'bull', 'bear', 'sideways'})
        self.assertEqual(cm['bull']['bars'], 2)
        self.assertEqual(cm['bear']['bars'], 2)
        self.assertEqual(cm['sideways']['bars'], 1)
        # Bull bars (+10%, +10%) -> compounded +21%.
        self.assertAlmostEqual(cm['bull']['total_return_pct'], 21.0, places=6)
        # Bear bars are losses -> negative return, positive drawdown.
        self.assertLess(cm['bear']['total_return_pct'], 0.0)
        self.assertGreater(cm['bear']['max_drawdown_pct'], 0.0)
        self.assertEqual(cm['sideways']['max_drawdown_pct'], 0.0)

    def test_empty_and_mismatch(self):
        cm = conditional_metrics([], [], [])
        for r in ('bull', 'bear', 'sideways'):
            self.assertEqual(cm[r]['bars'], 0)
        # length mismatch -> all-zero
        cm2 = conditional_metrics([1.0, 2.0], _daily_ts(2), ['bull'])
        for r in ('bull', 'bear', 'sideways'):
            self.assertEqual(cm2[r]['bars'], 0)


class BacktestResultWiringTests(unittest.TestCase):
    def test_zero_trades_has_benchmark_and_alpha(self):
        # No trades -> flat equity; benchmark still computed from closes.
        ts = _daily_ts(11)
        closes = [100.0 * (1.0 + 0.05 * i) for i in range(11)]  # +50% bull
        equity = [1000.0] * 11
        res = BacktestResult([], equity, ts, BacktestConfig(initial_balance=1000.0),
                             closes=closes)
        self.assertEqual(res.total_trades, 0)
        self.assertAlmostEqual(res.benchmark['total_return_pct'], 50.0, places=6)
        # Strategy did nothing -> total_roi 0 -> alpha == -benchmark return.
        self.assertAlmostEqual(res.alpha_vs_benchmark_pct, -50.0, places=6)
        self.assertIn('bull', res.conditional_metrics)
        self.assertIn('benchmark', res.to_dict())
        self.assertIn('alpha_vs_benchmark_pct', res.to_dict())
        self.assertIn('conditional_metrics', res.to_dict())
        # summary() must not crash and should mention the benchmark.
        s = res.summary()
        self.assertIn('Buy-and-Hold', s)
        self.assertIn('Alpha vs B&H', s)

    def test_no_closes_is_safe(self):
        # Older callers may not pass closes — must not crash.
        ts = _daily_ts(5)
        equity = [1000.0, 1010.0, 1020.0, 1015.0, 1030.0]
        res = BacktestResult([], equity, ts, BacktestConfig(initial_balance=1000.0))
        self.assertEqual(res.benchmark['total_return_pct'], 0.0)
        self.assertEqual(res.benchmark['equity_curve'], [])
        self.assertIsInstance(res.summary(), str)
        d = res.to_dict()
        self.assertEqual(d['benchmark']['total_return_pct'], 0.0)
        self.assertEqual(d['benchmark']['equity_curve_len'], 0)
        self.assertEqual(res.equity_series(), [
            {'ts': t.isoformat(), 'equity': e, 'benchmark': None, 'regime': None}
            for t, e in zip(ts, equity)
        ])

    def test_calmar_and_dd_duration_on_equity(self):
        ts = _daily_ts(6)
        # equity: peak at 1100 (idx1), trough 990 (idx4), then 1000.
        equity = [1000.0, 1100.0, 1050.0, 1000.0, 990.0, 1000.0]
        closes = [100.0] * 6  # flat benchmark
        res = BacktestResult([], equity, ts, BacktestConfig(initial_balance=1000.0),
                             closes=closes)
        # dd_duration / time-under-water come straight off the equity curve.
        self.assertEqual(res.dd_duration_bars, 4)   # idx 2..5 underwater
        self.assertGreater(res.dd_duration_days, 0.0)
        self.assertGreater(res.time_under_water_pct, 0.0)
        # benchmark flat -> calmar 0, alpha == strategy return.
        self.assertEqual(res.benchmark['total_return_pct'], 0.0)


if __name__ == '__main__':
    unittest.main()
