"""Tests for the daily-bar backtest harness, the B1/B2 benchmark strategies,
and the random-entry null harness (milestones M0/M1/M2 of
docs/STRATEGY-V1-TREND-VOLTARGET.md).  Deterministic — RNG is seeded.
"""

import math
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backtest"))

from backtest.daily_backtester import (DailyBacktester, DailyBacktestConfig,
                                       _funding_paid_over, load_funding_series)
from backtest.daily_strategies import BuyAndHold, TrailingStopBH, ScheduleStrategy
from backtest.random_entry_null import (make_random_schedule, random_entry_null,
                                        percentile_of, _realized_mean_hold)


def _daily_df(closes, start=datetime(2024, 1, 1, tzinfo=timezone.utc),
              highs=None, lows=None):
    n = len(closes)
    ts = [start + timedelta(days=i) for i in range(n)]
    return pd.DataFrame({
        "timestamp": ts,
        "open": closes,
        "high": highs if highs is not None else closes,
        "low": lows if lows is not None else closes,
        "close": closes,
        "volume": [1.0] * n,
    })


class _ConstExposure:
    """Strategy that always targets a fixed exposure."""
    def __init__(self, e):
        self.e = e
    def target_exposure(self, history):
        return self.e


class _StepExposure:
    """Targets `before` for the first `switch_at` decisions, then `after`."""
    def __init__(self, before, after, switch_at):
        self.before, self.after, self.switch_at = before, after, switch_at
    def target_exposure(self, history):
        return self.before if len(history) <= self.switch_at else self.after


class HarnessMechanicsTests(unittest.TestCase):
    def test_flat_strategy_equity_is_constant(self):
        df = _daily_df([100, 110, 90, 105, 120])
        cfg = DailyBacktestConfig(initial_balance=1000.0)
        r = DailyBacktester(_ConstExposure(0.0), cfg).run(df)
        # never invested -> equity never moves, no fees, no funding
        self.assertTrue(all(abs(e - 1000.0) < 1e-9 for e in r.equity_curve))
        self.assertEqual(r.n_rebalances, 0)
        self.assertAlmostEqual(r.total_fees, 0.0)
        self.assertAlmostEqual(r.total_funding, 0.0)
        self.assertAlmostEqual(r.total_roi, 0.0)
        self.assertAlmostEqual(r.max_drawdown_pct, 0.0)

    def test_fully_invested_no_costs_tracks_price(self):
        # No fees, no slippage, no funding -> equity should mirror price 1:1
        # after the single entry on bar 0.
        closes = [100.0, 120.0, 90.0, 150.0]
        df = _daily_df(closes)
        cfg = DailyBacktestConfig(initial_balance=1000.0, fee_maker=0.0, fee_taker=0.0,
                                  slippage_pct=0.0, funding_series_path=None)
        r = DailyBacktester(_ConstExposure(1.0), cfg).run(df)
        # equity[0] = 1000 (flat at bar 0), then scales with close ratio
        expected = [1000.0]
        for i in range(1, len(closes)):
            expected.append(expected[-1] * closes[i] / closes[i - 1])
        for got, exp in zip(r.equity_curve, expected):
            self.assertAlmostEqual(got, exp, places=6)
        self.assertEqual(r.n_rebalances, 1)  # one entry, never changes after
        self.assertAlmostEqual(r.total_roi, (closes[-1] / closes[0] - 1) * 100, places=6)

    def test_entry_fee_and_slippage_charged_on_delta_only(self):
        # One entry from 0 -> 1.0 on bar 0, price flat thereafter so the only
        # equity change is the entry cost.  Blended fee at 80% maker:
        #   0.8*0.001 + 0.2*0.002 = 0.0012 ; slippage 0.10% -> total 0.0022
        # traded notional fraction = 1.0 of $1000 -> cost = $2.20.
        df = _daily_df([100.0, 100.0, 100.0])
        cfg = DailyBacktestConfig(initial_balance=1000.0, fee_maker=0.001, fee_taker=0.002,
                                  maker_fraction=0.8, slippage_pct=0.10,
                                  funding_series_path=None)
        r = DailyBacktester(_ConstExposure(1.0), cfg).run(df)
        self.assertAlmostEqual(r.total_fees, 2.20, places=6)
        # equity after entry: 1000 - 2.20 = 997.80, then price flat
        self.assertAlmostEqual(r.equity_curve[-1], 997.80, places=6)
        self.assertEqual(r.n_rebalances, 1)

    def test_no_trade_band_suppresses_small_rebalances(self):
        # Step exposure 0.50 -> 0.525 after the first decision: a 5% relative
        # change.  With a 15% band it must NOT rebalance; with a 1% band it must.
        df = _daily_df([100.0] * 6)  # price flat so costs are the only signal
        cfg_wide = DailyBacktestConfig(initial_balance=1000.0, no_trade_band_pct=15.0,
                                       funding_series_path=None)
        cfg_tight = DailyBacktestConfig(initial_balance=1000.0, no_trade_band_pct=1.0,
                                        funding_series_path=None)
        r_wide = DailyBacktester(_StepExposure(0.50, 0.525, 1), cfg_wide).run(df)
        r_tight = DailyBacktester(_StepExposure(0.50, 0.525, 1), cfg_tight).run(df)
        # wide band: only the initial 0->1.0 entry rebalances
        self.assertEqual(r_wide.n_rebalances, 1)
        # tight band: initial entry + the small bump
        self.assertEqual(r_tight.n_rebalances, 2)

    def test_going_flat_is_never_blocked_by_band(self):
        # 1.0 -> 0.0 is a "100% relative change" but more importantly risk
        # reduction is always allowed regardless of band.
        df = _daily_df([100.0] * 5)
        cfg = DailyBacktestConfig(initial_balance=1000.0, no_trade_band_pct=99.0,
                                  funding_series_path=None)
        r = DailyBacktester(_StepExposure(1.0, 0.0, 1), cfg).run(df)
        # entry (0->1) then exit (1->0) -> 2 rebalances
        self.assertEqual(r.n_rebalances, 2)
        self.assertEqual(r.daily_exposure[-1], 0.0)

    def test_funding_cost_accounting(self):
        # Construct an explicit 8h funding series and verify the long pays it.
        # Daily bars at 00:00 UTC; between day t (00:00) and t+1 (00:00) there
        # are settlements at 00:00 (excluded, == start), 08:00, 16:00 -> and the
        # next 00:00 IS included (== end).  With a flat constant rate `rf` per
        # settlement that's a known number.
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        df = _daily_df([100.0, 100.0], start=start)  # 1 holding day, flat price
        rf = 0.0001
        # settlements covering (Jan-1 00:00, Jan-2 00:00]: 08:00, 16:00, and
        # Jan-2 00:00  -> 3 settlements.
        fund_ts = [start + timedelta(hours=h) for h in (-8, 0, 8, 16, 24, 32)]
        fdf = pd.DataFrame({"timestamp": fund_ts, "funding_rate": [rf] * len(fund_ts)})
        import tempfile, os
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
            fdf.to_csv(f.name, index=False)
            path = f.name
        try:
            # sanity on the helper itself
            self.assertAlmostEqual(
                _funding_paid_over(load_funding_series(path), start,
                                   start + timedelta(days=1)),
                3 * rf, places=12)
            cfg = DailyBacktestConfig(initial_balance=1000.0, fee_maker=0.0,
                                      fee_taker=0.0, slippage_pct=0.0,
                                      funding_series_path=path)
            r = DailyBacktester(_ConstExposure(1.0), cfg).run(df)
            # exposure 1.0, notional == equity (1000, no fees) -> funding = 1000*3*rf
            self.assertAlmostEqual(r.total_funding, 1000.0 * 3 * rf, places=6)
            self.assertAlmostEqual(r.equity_curve[-1], 1000.0 - 1000.0 * 3 * rf, places=6)
        finally:
            os.unlink(path)

    def test_diagnostics_lengths_match_equity_curve(self):
        df = _daily_df([100, 110, 105, 120, 130, 90])
        cfg = DailyBacktestConfig(initial_balance=1000.0)
        r = DailyBacktester(BuyAndHold(1.0), cfg).run(df)
        n = len(r.equity_curve)
        self.assertEqual(len(r.daily_exposure), n)
        self.assertEqual(len(r.daily_turnover), n)
        self.assertEqual(len(r.daily_fees), n)
        self.assertEqual(len(r.daily_funding), n)
        self.assertEqual(len(r.timestamps), n)
        self.assertEqual(len(r.closes), n)


class TrailingStopStrategyTests(unittest.TestCase):
    def test_stop_then_reentry_on_constructed_path(self):
        # Path: rise to 130 (running high), then a -10%+ dip to <=117 triggers
        # the stop on day index 3 (close 116) -> flat on day 4.  Then it grinds
        # back up and makes a new N-day high -> re-enters.
        # closes:   100 110 130 116 118 125 140 ...
        # idx:        0   1   2   3   4   5   6
        # running high of HIGH = 130 by idx 2.  130*0.9 = 117.  At idx 3, low<=117
        # (close==low==116) -> stop fires; exposure for day idx4 == 0.
        # With N=4: at idx 6, close 140 vs max(close[3..6]) = 140 -> new 4d high
        #   -> re-enter; exposure for day idx7 == 1.0.
        closes = [100, 110, 130, 116, 118, 125, 140, 145]
        df = _daily_df(closes)  # high==low==close for simplicity
        s = TrailingStopBH(trail_pct=0.10, breakout_days=4, reenter=True)
        s.reset()
        exposures = []
        for t in range(len(closes)):
            exposures.append(s.target_exposure(df.iloc[: t + 1]))
        # decisions at the close of day t set exposure over day t+1
        # idx0: in, no stop -> 1.0 ; idx1: 1.0 ; idx2: still in, runmax 130, no
        # stop (close 130 == runmax) -> 1.0 ; idx3: low 116 <= 117 -> 0.0 ;
        # idx4 (close 118, flat, max(close[1..4])=130 != 118) -> 0.0 ;
        # idx5 (close 125, max(close[2..5])=130) -> 0.0 ;
        # idx6 (close 140, max(close[3..6])=140 == 140) -> 1.0 ; idx7 -> 1.0
        self.assertEqual(exposures, [1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 1.0, 1.0])

    def test_oneshot_never_reenters(self):
        closes = [100, 110, 130, 116, 118, 125, 140, 145]
        df = _daily_df(closes)
        s = TrailingStopBH(trail_pct=0.10, breakout_days=4, reenter=False)
        s.reset()
        exposures = [s.target_exposure(df.iloc[: t + 1]) for t in range(len(closes))]
        # same first 4, then stays 0 forever
        self.assertEqual(exposures, [1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0])

    def test_no_stop_means_always_in(self):
        # Monotone rise: never triggers a stop.
        closes = [100 * (1.05 ** i) for i in range(20)]
        df = _daily_df(closes)
        s = TrailingStopBH(trail_pct=0.10, breakout_days=5, reenter=True)
        s.reset()
        exposures = [s.target_exposure(df.iloc[: t + 1]) for t in range(len(closes))]
        self.assertTrue(all(e == 1.0 for e in exposures))

    def test_intraday_low_triggers_before_close(self):
        # close stays above the trail line but the intraday LOW dips below it.
        # highs: 100 110 130 ; lows: ... 100 105 116 (idx2 low 116 <= 130*0.9)
        closes = [100, 110, 128, 125]
        highs = [101, 111, 130, 126]
        lows = [99, 109, 116, 124]
        df = _daily_df(closes, highs=highs, lows=lows)
        s = TrailingStopBH(trail_pct=0.10, breakout_days=10, reenter=True)
        s.reset()
        exposures = [s.target_exposure(df.iloc[: t + 1]) for t in range(len(closes))]
        # idx2: runmax(high)=130, 130*0.9=117, low 116 <= 117 -> stop -> 0.0
        self.assertEqual(exposures[:3], [1.0, 1.0, 0.0])


class B2VerificationTests(unittest.TestCase):
    """The §7 verification gate: the one-shot trailing-stop-on-BH must roughly
    reproduce docs/edge-diagnosis/I-ablations.md row 14 (~+15-20% return,
    ~8-12% max-DD) **over the diagnosis window** — i.e. the ~2025-04 → 2026-04
    span of BTC-USDT_5m.csv.  The daily CSV now covers ~3.3 years (2023-01 →);
    over that longer window plain BH is up >300% and the one-shot rule (which
    exits once in 2023 and never re-enters) does NOT beat it — that's expected
    and is exactly why M3 picks the variant deliberately.  This test therefore
    slices the daily series down to the diagnosis window before checking.
    Skips if the daily CSV is absent."""

    DIAG_START = pd.Timestamp("2025-04-13", tz="UTC")
    DIAG_END = pd.Timestamp("2026-04-18", tz="UTC")

    def test_oneshot_b2_reproduces_diagnosis_numbers(self):
        csv = Path(__file__).resolve().parent.parent / "backtest" / "data" / "BTC-USDT_1d.csv"
        if not csv.exists():
            self.skipTest("BTC-USDT_1d.csv not present (regenerable via "
                          "python -m backtest.build_daily_csv)")
        df = pd.read_csv(csv)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df[(df["timestamp"] >= self.DIAG_START) & (df["timestamp"] < self.DIAG_END)] \
            .reset_index(drop=True)
        if len(df) < 200:
            self.skipTest("daily CSV does not cover the diagnosis window")
        # No funding (the diagnosis didn't subtract perp funding), realistic fees.
        cfg = DailyBacktestConfig(initial_balance=5000.0, funding_series_path=None)
        r = DailyBacktester(TrailingStopBH(0.10, 20, reenter=False), cfg).run(df)
        # Expect ~+15-21% total return and ~8-12% max-DD over the diagnosis window.
        self.assertGreater(r.total_roi, 8.0,
                           f"B2 one-shot return {r.total_roi:.2f}% too low — possible bug")
        self.assertLess(r.total_roi, 30.0, f"B2 one-shot return {r.total_roi:.2f}% too high")
        self.assertGreater(r.max_drawdown_pct, 5.0)
        self.assertLess(r.max_drawdown_pct, 14.0,
                        f"B2 one-shot max-DD {r.max_drawdown_pct:.2f}% — diagnosis was ~10%")
        # And it must beat plain buy-and-hold (B1) over the diagnosis window.
        b1 = DailyBacktester(BuyAndHold(1.0), cfg).run(df)
        self.assertGreater(r.total_roi, b1.total_roi)
        self.assertLess(r.max_drawdown_pct, b1.max_drawdown_pct)


class RandomEntryNullTests(unittest.TestCase):
    def test_schedule_matches_time_in_market_and_hold_targets(self):
        rng = np.random.default_rng(42)
        n_bars = 4000
        tim_target, hold_target = 0.55, 25.0
        # average over several long draws -> realized TiM / hold within tolerance
        tims, holds = [], []
        for _ in range(40):
            sched = make_random_schedule(n_bars, tim_target, hold_target, rng)
            tims.append(float(np.mean(sched)))
            holds.append(_realized_mean_hold(sched))
        self.assertAlmostEqual(float(np.mean(tims)), tim_target, delta=0.03)
        self.assertAlmostEqual(float(np.mean(holds)), hold_target, delta=4.0)

    def test_schedule_is_deterministic_for_seed(self):
        rng1 = np.random.default_rng(7)
        rng2 = np.random.default_rng(7)
        s1 = make_random_schedule(500, 0.5, 20.0, rng1)
        s2 = make_random_schedule(500, 0.5, 20.0, rng2)
        np.testing.assert_array_equal(s1, s2)

    def test_percentile_of_basic(self):
        dist = list(range(0, 100))  # 0..99
        self.assertAlmostEqual(percentile_of(50, dist), 50.0)   # 50 values < 50
        self.assertAlmostEqual(percentile_of(0, dist), 0.0)
        self.assertAlmostEqual(percentile_of(100, dist), 100.0)
        self.assertAlmostEqual(percentile_of(25, dist), 25.0)
        self.assertTrue(math.isnan(percentile_of(1.0, [])))

    def test_null_distribution_runs_and_is_seed_stable(self):
        df = _daily_df([100 + 5 * math.sin(i / 7.0) + i * 0.3 for i in range(120)])
        cfg = DailyBacktestConfig(initial_balance=1000.0, funding_series_path=None)
        a = random_entry_null(df, 0.6, 20.0, reps=30, config=cfg, seed=99)
        b = random_entry_null(df, 0.6, 20.0, reps=30, config=cfg, seed=99)
        np.testing.assert_array_almost_equal(a.total_return_pct, b.total_return_pct)
        # realized targets roughly hit even on this short series (averaged over reps)
        self.assertAlmostEqual(float(np.mean(a.time_in_market)), 0.6, delta=0.12)
        self.assertEqual(a.reps, 30)
        # a flat strategy that is always invested is one extreme of the null
        # range — its return should sit within [min, max] of the null returns.
        full_in = DailyBacktester(BuyAndHold(1.0), cfg).run(df).total_roi
        self.assertGreaterEqual(full_in, a.total_return_pct.min() - 1e-6)


if __name__ == "__main__":
    unittest.main()
