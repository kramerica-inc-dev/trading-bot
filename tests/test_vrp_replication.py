"""Tests for the faithful VRP replication engine (data-independent)."""

import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backtest"))

from sweep.vrp_replication import (  # noqa: E402
    bs_price, bs_delta, bs_vega, strike_for_delta,
    replicate_cycle, ReplConfig,
)


class TestBlackScholes(unittest.TestCase):

    def test_put_call_parity_r0(self):
        # r=0: C - P = S - K
        S, K, T, sig = 100.0, 110.0, 30 / 365, 0.6
        c = bs_price(S, K, T, sig, "c")
        p = bs_price(S, K, T, sig, "p")
        self.assertAlmostEqual(c - p, S - K, places=6)

    def test_atm_straddle_approx(self):
        # ATM straddle ~ 0.8 * S * sig * sqrt(T)
        S, T, sig = 50_000.0, 30 / 365, 0.65
        straddle = bs_price(S, S, T, sig, "c") + bs_price(S, S, T, sig, "p")
        approx = 0.7979 * S * sig * np.sqrt(T)
        self.assertAlmostEqual(straddle / approx, 1.0, delta=0.02)

    def test_deltas_bounds_and_signs(self):
        S, K, T, sig = 100.0, 100.0, 30 / 365, 0.6
        self.assertTrue(0.4 < bs_delta(S, K, T, sig, "c") < 0.7)   # ATM call ~0.5+
        self.assertTrue(-0.7 < bs_delta(S, K, T, sig, "p") < -0.3)  # ATM put ~-0.5+
        self.assertGreater(bs_vega(S, K, T, sig), 0.0)

    def test_strike_for_delta_inverts(self):
        S, T, sig = 30_000.0, 30 / 365, 0.7
        for kind, target in (("c", 0.15), ("p", -0.15), ("c", 0.25), ("p", -0.25)):
            K = strike_for_delta(S, T, sig, kind, target)
            self.assertAlmostEqual(bs_delta(S, K, T, sig, kind), target, places=4)
        # 15-delta call strike is above spot, 15-delta put strike below spot
        self.assertGreater(strike_for_delta(S, T, sig, "c", 0.15), S)
        self.assertLess(strike_for_delta(S, T, sig, "p", -0.15), S)


def _gbm_path(S0, sig, days, seed):
    rng = np.random.default_rng(seed)
    dt = 1.0 / 365.0
    z = rng.normal(0, 1, days)
    logret = (-0.5 * sig * sig) * dt + sig * np.sqrt(dt) * z
    return S0 * np.concatenate([[1.0], np.exp(np.cumsum(logret))])


class TestReplication(unittest.TestCase):

    def test_realized_equals_implied_nets_zero(self):
        # No costs, hedge at IV, realized vol == implied -> E[P&L] ~ 0
        iv = 0.60
        cfg = ReplConfig(hedge_cost_bps=0.0, opt_entry_spread_volpts=0.0, wing_ratio=0.0)
        pnls = [replicate_cycle(_gbm_path(100.0, iv, 30, s), iv, cfg).pnl_volpts
                for s in range(400)]
        self.assertLess(abs(np.mean(pnls)), 3.0)   # mean within a few vol points of 0

    def test_realized_above_implied_loses(self):
        # Path much more volatile than the IV we sold -> short vol loses
        iv = 0.40
        cfg = ReplConfig(hedge_cost_bps=0.0, opt_entry_spread_volpts=0.0, wing_ratio=0.0)
        pnls = [replicate_cycle(_gbm_path(100.0, 3.0 * iv, 30, s), iv, cfg).pnl_volpts
                for s in range(300)]
        self.assertLess(np.mean(pnls), -5.0)

    def test_realized_below_implied_profits(self):
        iv = 0.80
        cfg = ReplConfig(hedge_cost_bps=0.0, opt_entry_spread_volpts=0.0, wing_ratio=0.0)
        pnls = [replicate_cycle(_gbm_path(100.0, 0.2 * iv, 30, s), iv, cfg).pnl_volpts
                for s in range(300)]
        self.assertGreater(np.mean(pnls), 5.0)

    def test_tail_hedge_reduces_gap_loss(self):
        # Deterministic crash: flat, then a -40% gap, then flat
        S0 = 100.0
        closes = np.concatenate([
            np.full(5, S0), np.full(26, 0.60 * S0),
        ])[:31]
        iv = 0.60
        base = dict(hedge_cost_bps=0.0, opt_entry_spread_volpts=0.0)
        naked = replicate_cycle(closes, iv, ReplConfig(wing_ratio=0.0, **base)).pnl_volpts
        hedged = replicate_cycle(
            closes, iv,
            ReplConfig(wing_delta=0.15, wing_ratio=1.0, wing_skew_volpts=0.0, **base)
        ).pnl_volpts
        self.assertLess(naked, 0.0)            # naked short straddle loses on the crash
        self.assertGreater(hedged, naked)      # long wings cushion the tail


if __name__ == "__main__":
    unittest.main()
