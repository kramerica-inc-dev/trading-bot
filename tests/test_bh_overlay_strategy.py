"""Pure-function tests for `scripts/bh_overlay_strategy.py`.

The strategy is a thin composition of `backtest/v1_strategies.py:VolTarget`
and `backtest/daily_strategies.py:TrailingStopBH(reenter=True)` — so most of
the math is already covered by `tests/test_v1_strategies.py`. These tests
verify the *composition* behaves correctly: bull leg → exposure ≈ 1.0
(scaled by vol-target), drawdown → exit, new N-day high → re-entry, vol
spike → multiplier drops, no-trade band suppresses tiny rebalances, and
the state machine round-trips through `to_state` / `from_state`.

Deterministic — no random, no I/O.
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from bh_overlay_strategy import (  # noqa: E402
    BHOverlayStrategy, StrategyDecision, should_rebalance,
)


def _daily_df(closes, highs=None, lows=None,
              start=datetime(2024, 1, 1, tzinfo=timezone.utc)):
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


def _replay(strat, df):
    """Replay the strategy bar-by-bar (the runner pattern) and return the
    last decision."""
    last = None
    for t in range(1, len(df) + 1):
        last = strat.decide(df.iloc[:t])
    return last


class TestBHOverlayStrategy(unittest.TestCase):
    def test_bull_leg_full_exposure_when_vol_low(self):
        """Steady bull leg + low vol → trend ON, vol-target multiplier ≈ 1.0."""
        # ~+0.5%/day for 80 days; constant returns → realized vol ≈ 0.
        n = 80
        closes = 100.0 * (1.005 ** np.arange(n))
        df = _daily_df(closes)
        strat = BHOverlayStrategy(sigma_target=0.20, vol_window=30)
        decision = _replay(strat, df)
        self.assertTrue(decision.signal_on, "trend should be ON in steady bull")
        # Vol is near zero → multiplier clipped to L_max = 1.0.
        self.assertGreaterEqual(decision.target_exposure, 0.99)
        self.assertGreaterEqual(decision.vol_target_multiplier, 0.99)
        self.assertEqual(decision.reason, "in_market")

    def test_drawdown_triggers_exit(self):
        """A −12% leg from the running high triggers the trailing stop."""
        # Walk up then down enough to breach the 10% trail.
        up = np.linspace(100, 130, 30)
        down = np.linspace(130, 113, 6)  # ~−13% from 130 → low touches 130*0.90
        closes = np.concatenate([up, down])
        # craft lows so the trail trips on the last bar
        lows = closes * 0.999
        lows[-1] = 116.0  # 130 * 0.90 = 117 → low of 116 < 117 → stop
        df = _daily_df(closes, lows=lows)
        strat = BHOverlayStrategy(trail_pct=0.10, breakout_days=20)
        last = _replay(strat, df)
        self.assertFalse(last.signal_on, "trail should fire and go flat")
        self.assertEqual(last.target_exposure, 0.0)
        self.assertEqual(last.reason, "flat_awaiting_reentry")

    def test_new_n_day_high_reenters(self):
        """After a stop, a new 20-day high on the close re-enters."""
        # Build: bull → trail trip → recovery to a new 20-day-high close.
        seg1 = np.linspace(100, 130, 25)            # bull, peaks ~130
        seg2 = np.linspace(130, 110, 5)             # pullback, force trail
        seg3 = np.linspace(110, 105, 25)            # extended flat-ish window
        seg4 = np.array([115.0])                    # not yet a 20d-high
        # Now push a NEW 20-day high — must exceed all 20 prior closes.
        # The 20 prior closes are the last 20 of seg2+seg3+seg4 — max ≤ ~130.
        # Make the next close beat 135 to be safe.
        seg5 = np.array([140.0])
        closes = np.concatenate([seg1, seg2, seg3, seg4, seg5])
        # Force the trail to trip on the last bar of seg2:
        lows = closes * 0.999
        # entry running_high after seg1 is ~130 (high = close*1.001 ≈ 130.13).
        # trail = 130.13 * 0.90 ≈ 117.12. Set seg2's last low below that:
        seg2_end = len(seg1) + len(seg2) - 1
        lows[seg2_end] = 115.0
        df = _daily_df(closes, lows=lows)

        strat = BHOverlayStrategy(trail_pct=0.10, breakout_days=20)
        # Replay one bar at a time so the state machine sees each bar exactly
        # once (just like the runner).
        last = None
        signals = []
        for t in range(1, len(df) + 1):
            last = strat.decide(df.iloc[:t])
            signals.append(last.signal_on)
        # The final bar must have re-entered (signal flipped back on).
        self.assertTrue(signals[-1], "must re-enter on the new 20-day high")
        self.assertGreater(last.target_exposure, 0.0)

    def test_vol_spike_scales_size_down(self):
        """Vol spike → vol_target_multiplier drops well below L_max."""
        rng = np.random.default_rng(7)
        # 80 calm bars → 30 noisy bars (3× the daily vol)
        calm_lr = rng.normal(0.001, 0.005, 80)
        noisy_lr = rng.normal(0.001, 0.05, 30)
        closes = 100.0 * np.exp(np.cumsum(np.concatenate([calm_lr, noisy_lr])))
        df = _daily_df(closes)
        strat = BHOverlayStrategy(sigma_target=0.20, vol_window=30)
        last = _replay(strat, df)
        # During the noisy regime σ_realized >> σ_target → multiplier << 1.0.
        self.assertTrue(np.isfinite(last.vol_realized))
        self.assertLess(last.vol_target_multiplier, 0.5)
        # Trend is still ON (uptrend) but exposure is scaled down.
        if last.signal_on:
            self.assertLess(last.target_exposure, 0.5)

    def test_state_roundtrip(self):
        """to_state() → from_state() preserves trailing-stop state."""
        closes = np.linspace(100, 130, 40)
        df = _daily_df(closes)
        s1 = BHOverlayStrategy()
        _replay(s1, df)
        snapshot = s1.to_state()

        s2 = BHOverlayStrategy()
        s2.from_state(snapshot)
        # Both should agree on the next decision given the same history.
        d1 = s1.decide(df)  # advances s1's internal state
        d2 = s2.decide(df)  # advances s2's restored state
        self.assertEqual(d1.signal_on, d2.signal_on)
        self.assertAlmostEqual(d1.target_exposure, d2.target_exposure, places=9)


class TestShouldRebalance(unittest.TestCase):
    def test_flat_to_anything_enters(self):
        self.assertTrue(should_rebalance(0.0, 0.3, no_trade_band=0.15))
        self.assertFalse(should_rebalance(0.0, 0.0, no_trade_band=0.15))

    def test_target_zero_always_exits(self):
        # Going to flat is never blocked by the band.
        self.assertTrue(should_rebalance(0.5, 0.0, no_trade_band=0.15))

    def test_within_band_suppressed(self):
        # 5% change relative to current 0.50 → below 15% band → suppress.
        self.assertFalse(should_rebalance(0.50, 0.525, no_trade_band=0.15))

    def test_outside_band_triggers(self):
        # 20% change relative to current 0.50 → above 15% band → rebalance.
        self.assertTrue(should_rebalance(0.50, 0.60, no_trade_band=0.15))


class TestEmptyHistory(unittest.TestCase):
    def test_empty_returns_zero(self):
        strat = BHOverlayStrategy()
        d = strat.decide(pd.DataFrame(columns=["timestamp", "open", "high",
                                               "low", "close", "volume"]))
        self.assertEqual(d.target_exposure, 0.0)
        self.assertFalse(d.signal_on)
        self.assertEqual(d.reason, "empty_history")


if __name__ == "__main__":
    unittest.main()
