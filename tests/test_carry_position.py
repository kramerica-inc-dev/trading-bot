"""Unit tests for `scripts/carry_position.py` — pure math, no I/O.

Mirrors `tests/test_carry_backtest.py` discipline: deterministic, seeded,
no network. Sign conventions:
    spot_qty > 0 (long)
    perp_qty < 0 (short)
    funding cash_flow: short RECEIVES positive rate
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from carry_position import (  # noqa: E402
    CarryPosition, DriftReport, TargetSize,
    annualize_funding, delta_neutral_drift,
    funding_accrual_step, green_button_on,
    projected_next_funding_usd, target_position_for,
    SETTLEMENTS_PER_YEAR,
)


class TestCarryPositionBasics(unittest.TestCase):

    def test_default_position_is_flat(self):
        p = CarryPosition()
        self.assertTrue(p.is_flat)
        self.assertEqual(p.notional_usd_at_entry, 0.0)

    def test_entry_basis_is_spot_minus_perp(self):
        p = CarryPosition(entry_spot_price=63500.0, entry_perp_price=63520.0)
        self.assertEqual(p.entry_basis, -20.0)

    def test_to_from_json_roundtrip(self):
        p = CarryPosition(spot_qty=0.05, perp_qty=-0.05,
                          entry_spot_price=63500.0,
                          entry_perp_price=63510.0,
                          funding_accrued=1.23, fees_paid=0.4,
                          opened_ts="2026-05-13T00:00:00+00:00")
        d = p.to_json()
        p2 = CarryPosition.from_json(d)
        self.assertEqual(p, p2)

    def test_notional_at_entry(self):
        p = CarryPosition(spot_qty=0.1, entry_spot_price=60000.0)
        self.assertAlmostEqual(p.notional_usd_at_entry, 6000.0)


class TestTargetSizing(unittest.TestCase):

    def test_target_for_3k_at_60k_with_2x(self):
        t = target_position_for(3000.0, 60000.0, leverage=2.0)
        self.assertAlmostEqual(t.spot_qty, 0.05, places=9)
        self.assertAlmostEqual(t.perp_qty, -0.05, places=9)
        self.assertAlmostEqual(t.notional_usd, 3000.0)
        self.assertAlmostEqual(t.perp_margin_usd, 1500.0)

    def test_target_zero_notional_or_price_returns_zero(self):
        self.assertEqual(target_position_for(0.0, 60000.0).spot_qty, 0.0)
        self.assertEqual(target_position_for(3000.0, 0.0).spot_qty, 0.0)
        self.assertEqual(target_position_for(-100, 60000.0).spot_qty, 0.0)

    def test_target_floored_by_min_btc(self):
        # 100 USD at 60k = 0.00166 BTC; with min_btc=0.01 → returns zeros
        t = target_position_for(100.0, 60000.0, leverage=2.0, min_btc=0.01)
        self.assertEqual(t.spot_qty, 0.0)
        self.assertEqual(t.perp_qty, 0.0)

    def test_target_rejects_zero_leverage(self):
        with self.assertRaises(ValueError):
            target_position_for(3000.0, 60000.0, leverage=0.0)

    def test_target_perp_qty_sign_is_short(self):
        t = target_position_for(1000.0, 50000.0, leverage=2.0)
        self.assertGreater(t.spot_qty, 0)
        self.assertLess(t.perp_qty, 0)
        self.assertAlmostEqual(t.spot_qty, -t.perp_qty, places=9)

    def test_perp_margin_inversely_proportional_to_leverage(self):
        t1 = target_position_for(3000.0, 60000.0, leverage=1.0)
        t2 = target_position_for(3000.0, 60000.0, leverage=2.0)
        t3 = target_position_for(3000.0, 60000.0, leverage=3.0)
        self.assertAlmostEqual(t1.perp_margin_usd, 3000.0)
        self.assertAlmostEqual(t2.perp_margin_usd, 1500.0)
        self.assertAlmostEqual(t3.perp_margin_usd, 1000.0)


class TestDeltaNeutralDrift(unittest.TestCase):

    def test_perfect_neutral_drift_is_zero(self):
        p = CarryPosition(spot_qty=0.1, perp_qty=-0.1,
                          entry_spot_price=60000.0, entry_perp_price=60010.0)
        d = delta_neutral_drift(p, 60500.0, 60510.0)
        self.assertAlmostEqual(d.net_qty_btc, 0.0, places=9)
        self.assertAlmostEqual(d.net_qty_usd, 0.0, places=6)

    def test_basis_drift_reflects_widening_spread(self):
        # entry basis = -10 USD; now spot=60500 perp=60400 → basis +100
        p = CarryPosition(spot_qty=0.1, perp_qty=-0.1,
                          entry_spot_price=60000.0, entry_perp_price=60010.0)
        d = delta_neutral_drift(p, 60500.0, 60400.0)
        self.assertAlmostEqual(d.basis_now_usd, 100.0)
        self.assertAlmostEqual(d.basis_drift_usd_per_btc, 100.0 - (-10.0))

    def test_asymmetric_legs_show_drift(self):
        # short slightly larger than long: net_qty_btc < 0
        p = CarryPosition(spot_qty=0.1, perp_qty=-0.11,
                          entry_spot_price=60000.0, entry_perp_price=60000.0)
        d = delta_neutral_drift(p, 60000.0, 60000.0)
        self.assertAlmostEqual(d.net_qty_btc, -0.01, places=9)
        self.assertAlmostEqual(d.net_qty_usd, -600.0, places=4)


class TestFundingAccrualSign(unittest.TestCase):

    def test_short_receives_positive_funding(self):
        p = CarryPosition(spot_qty=0.1, perp_qty=-0.1)
        flow = funding_accrual_step(p, funding_rate=0.0001, btc_price=60000.0)
        # short qty 0.1 at $60k = $6000 notional; +1bps → +0.60
        self.assertAlmostEqual(flow, 0.60, places=6)

    def test_short_pays_negative_funding(self):
        p = CarryPosition(spot_qty=0.1, perp_qty=-0.1)
        flow = funding_accrual_step(p, funding_rate=-0.0002, btc_price=60000.0)
        # -2bps on $6000 = -1.20
        self.assertAlmostEqual(flow, -1.20, places=6)

    def test_flat_book_no_cashflow(self):
        p = CarryPosition()
        self.assertEqual(
            funding_accrual_step(p, 0.001, 60000.0), 0.0,
        )

    def test_zero_price_no_cashflow(self):
        p = CarryPosition(spot_qty=0.1, perp_qty=-0.1)
        self.assertEqual(
            funding_accrual_step(p, 0.001, 0.0), 0.0,
        )

    def test_projected_next_funding(self):
        # +0.0001 on $6000 = +0.60
        self.assertAlmostEqual(projected_next_funding_usd(6000.0, 0.0001), 0.60)
        self.assertEqual(projected_next_funding_usd(0.0, 0.0001), 0.0)
        self.assertEqual(projected_next_funding_usd(-100.0, 0.0001), 0.0)


class TestAnnualisation(unittest.TestCase):

    def test_bps_per_8h_to_annual(self):
        # 0.96 bps/8h ≈ 10.5%/yr (matches STRATEGY-CARRY.md)
        ann = annualize_funding(0.000096)
        self.assertAlmostEqual(ann, 0.000096 * SETTLEMENTS_PER_YEAR, places=12)
        self.assertGreater(ann, 0.10)
        self.assertLess(ann, 0.11)

    def test_zero_funding_zero_annual(self):
        self.assertEqual(annualize_funding(0.0), 0.0)


class TestGreenButton(unittest.TestCase):

    def test_above_threshold_on(self):
        # +1bps/8h every settlement → +10.95%/yr; threshold +5%/yr → ON
        r = green_button_on([0.0001] * 30, threshold_annualised=0.05)
        self.assertTrue(r["on"])
        self.assertGreater(r["trailing_annualised"], 0.05)
        self.assertEqual(r["reason"], "above_threshold")

    def test_below_threshold_off(self):
        # +0.1bps/8h every settlement → ~+1.1%/yr; threshold +5%/yr → OFF
        r = green_button_on([0.00001] * 30, threshold_annualised=0.05)
        self.assertFalse(r["on"])
        self.assertEqual(r["reason"], "below_threshold")

    def test_insufficient_history(self):
        r = green_button_on([], threshold_annualised=0.05, min_samples=5)
        self.assertFalse(r["on"])
        self.assertEqual(r["reason"], "insufficient_history (have 0, need 5)")

    def test_threshold_exact_value_is_off(self):
        # Strictly greater-than per spec; equality → OFF
        # exactly +5%/yr means trailing_annualised == threshold
        # 5e-2 / (3*365) = ~4.566e-5
        r = green_button_on([5e-2 / SETTLEMENTS_PER_YEAR] * 30,
                            threshold_annualised=0.05)
        self.assertFalse(r["on"])

    def test_negative_funding_off(self):
        r = green_button_on([-0.0001] * 30, threshold_annualised=0.05)
        self.assertFalse(r["on"])
        self.assertLess(r["trailing_annualised"], 0)


if __name__ == "__main__":
    unittest.main()
