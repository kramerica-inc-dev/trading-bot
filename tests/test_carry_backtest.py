"""Deterministic unit tests for the cash-and-carry backtester
(`backtest/carry_backtest.py`) — funding-accrual sign, fee charging, basis mark,
and the on/off funding gate.  No network, no RNG except the seeded basis series.
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backtest"))

from backtest.carry_backtest import CarryConfig, run_carry_backtest


def _funding_df(rates, start=datetime(2024, 1, 1, tzinfo=timezone.utc)):
    ts = [start + timedelta(hours=8 * i) for i in range(len(rates))]
    return pd.DataFrame({"timestamp": ts, "funding_rate": list(rates)})


_ZERO_BASIS_CFG = dict(basis_sigma_bps=0.0, basis_meanrev=1.0)  # basis identically 0


class TestCarryAccounting(unittest.TestCase):

    def test_positive_funding_grows_equity(self):
        # 10 settlements of +1bps, no fees, no basis -> short RECEIVES funding.
        cfg = CarryConfig(initial_balance=1000.0, fee_maker_spot=0, fee_taker_spot=0,
                          fee_maker_perp=0, fee_taker_perp=0, **_ZERO_BASIS_CFG)
        r = run_carry_backtest(_funding_df([0.0001] * 10), cfg,
                               perp_basis=np.zeros(10))
        # equity = 1000 * 1.0001**10 (compounded on notional == full equity)
        self.assertAlmostEqual(r.equity_curve[-1], 1000.0 * (1.0001 ** 10), places=6)
        self.assertGreater(r.total_funding, 0.0)
        self.assertEqual(r.total_fees, 0.0)
        self.assertAlmostEqual(r.total_basis_pnl, 0.0, places=9)

    def test_negative_funding_shrinks_equity(self):
        # negative funding -> the short PAYS -> equity falls.
        cfg = CarryConfig(initial_balance=1000.0, fee_maker_spot=0, fee_taker_spot=0,
                          fee_maker_perp=0, fee_taker_perp=0, **_ZERO_BASIS_CFG)
        r = run_carry_backtest(_funding_df([-0.0002] * 5), cfg, perp_basis=np.zeros(5))
        self.assertLess(r.equity_curve[-1], 1000.0)
        self.assertLess(r.total_funding, 0.0)
        self.assertAlmostEqual(r.equity_curve[-1], 1000.0 * (1.0 - 0.0002) ** 5, places=6)

    def test_fees_charged_open_and_close(self):
        # zero funding, zero basis -> only the 4-fill round-trip cost remains.
        # maker_fraction=1 -> per-leg-open fee = maker_spot + maker_perp.
        cfg = CarryConfig(initial_balance=1000.0, fee_maker_spot=0.0002,
                          fee_maker_perp=0.0002, fee_taker_spot=0.0006,
                          fee_taker_perp=0.0006, maker_fraction=1.0, **_ZERO_BASIS_CFG)
        r = run_carry_backtest(_funding_df([0.0] * 4), cfg, perp_basis=np.zeros(4))
        # open: notional==1000, cost = 1000 * (0.0002+0.0002) = 0.40 -> equity 999.60
        # close: notional == 999.60, cost = 999.60 * 0.0004 = 0.39984
        expected = 1000.0 - 0.40
        expected = expected - expected * 0.0004
        self.assertAlmostEqual(r.equity_curve[-1], expected, places=6)
        self.assertEqual(r.n_legs, 2)  # one open burst + one close burst
        self.assertAlmostEqual(r.total_fees, 1000.0 - expected, places=6)

    def test_round_trip_fee_property(self):
        cfg = CarryConfig(fee_maker_spot=0.0002, fee_maker_perp=0.0002,
                          fee_taker_spot=0.0006, fee_taker_perp=0.0006,
                          maker_fraction=0.5)
        # blended per fill = 0.0004; per-leg-open (2 fills) = 0.0008; round-trip = 0.0016
        self.assertAlmostEqual(cfg.fee_per_leg_open, 0.0008, places=9)
        self.assertAlmostEqual(cfg.fee_round_trip, 0.0016, places=9)

    def test_basis_mark_long_spot_short_perp(self):
        # If the perp PREMIUM widens (basis 0 -> +b), a long-spot/short-perp pair
        # LOSES -notional*b.  Build a basis that steps once from 0 to +5bps and stays.
        n = 6
        basis = np.zeros(n)
        basis[2:] = 0.0005  # +5 bps from settlement 2 onward
        cfg = CarryConfig(initial_balance=1000.0, fee_maker_spot=0, fee_taker_spot=0,
                          fee_maker_perp=0, fee_taker_perp=0)
        r = run_carry_backtest(_funding_df([0.0] * n), cfg, perp_basis=basis)
        # only the single +5bps step is marked (entered at i=0 with basis 0):
        # equity ~= 1000 * (1 - 0.0005)
        self.assertAlmostEqual(r.equity_curve[-1], 1000.0 * (1.0 - 0.0005), places=6)
        self.assertLess(r.total_basis_pnl, 0.0)

    def test_basis_discount_helps_short(self):
        # perp goes to a DISCOUNT (basis -> negative) -> long-spot/short-perp GAINS.
        n = 5
        basis = np.zeros(n)
        basis[1:] = -0.0010  # -10 bps
        cfg = CarryConfig(initial_balance=1000.0, fee_maker_spot=0, fee_taker_spot=0,
                          fee_maker_perp=0, fee_taker_perp=0)
        r = run_carry_backtest(_funding_df([0.0] * n), cfg, perp_basis=basis)
        self.assertGreater(r.equity_curve[-1], 1000.0)
        self.assertGreater(r.total_basis_pnl, 0.0)

    def test_funding_gate_sits_out_negative_stretch(self):
        # 10 negative settlements then 10 positive.  A trailing-window gate that
        # exits when the trailing mean < 0 should be FLAT through the negatives
        # and ON through the positives -> end equity > the always-on case.
        rates = [-0.0003] * 10 + [0.0003] * 10
        fd = _funding_df(rates)
        always_on = CarryConfig(initial_balance=1000.0, fee_maker_spot=0,
                                fee_taker_spot=0, fee_maker_perp=0, fee_taker_perp=0,
                                **_ZERO_BASIS_CFG)
        gated = CarryConfig(initial_balance=1000.0, fee_maker_spot=0, fee_taker_spot=0,
                            fee_maker_perp=0, fee_taker_perp=0,
                            funding_gate_window=3, funding_gate_thresh=0.0,
                            **_ZERO_BASIS_CFG)
        r_on = run_carry_backtest(fd, always_on, perp_basis=np.zeros(20))
        r_gate = run_carry_backtest(fd, gated, perp_basis=np.zeros(20))
        self.assertLess(r_on.equity_curve[-1], 1000.0)       # always-on eats the negatives
        self.assertGreater(r_gate.equity_curve[-1], r_on.equity_curve[-1])
        # the gate must have toggled at least once on + once off
        self.assertGreaterEqual(r_gate.n_legs, 2)

    def test_metrics_finite_and_signed(self):
        rates = [0.0001] * 100
        r = run_carry_backtest(_funding_df(rates),
                               CarryConfig(initial_balance=5000.0, **_ZERO_BASIS_CFG),
                               perp_basis=np.zeros(100))
        self.assertGreater(r.annualized_return, 0.0)
        self.assertGreaterEqual(r.max_drawdown, 0.0)
        self.assertTrue(np.isfinite(r.sharpe))
        self.assertGreater(r.total_return, 0.0)


if __name__ == "__main__":
    unittest.main()
