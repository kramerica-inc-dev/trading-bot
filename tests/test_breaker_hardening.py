"""Catastrophe-breaker confirm-guard + safety-cycle de-lever (2026-06-14).

Locks the two fixes for the 2026-06-13 false terminal halt:
  1. The slow drawdown-from-peak catastrophe trigger must be CONFIRMED over
     catastrophe_confirm_cycles consecutive reads; the fast intracycle trigger
     still fires immediately. A single transient mark under-read can't strand
     the live book.
  2. _maybe_delever trims the live book x(1-factor) once per episode on the
     safety cycle when drawdown enters the soft band — the between-rebalance
     cushion that was missing (de-lever previously only acted at rebalance).

Network-free; the venue surface is stubbed exactly like test_hl_xs_runner.py.
"""

import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

try:
    import hl_xs_runner as R
    from xs_core import XSState
    HAVE_SDK = True
except ImportError:
    HAVE_SDK = False


def _breaker_stub(*, live=False, confirm=3, cat_dd=0.12, cat_intra=0.08):
    cfg = R.HLXSConfig(catastrophe_drawdown_pct=cat_dd, catastrophe_intracycle_pct=cat_intra,
                       catastrophe_confirm_cycles=confirm, halt_drawdown_pct=0.25)
    stub = types.SimpleNamespace(cfg=cfg, live_trading=live, _logs=[], _flats=0)
    stub.log = lambda e: stub._logs.append(e)
    stub.save_state = lambda s: None
    stub.write_health = lambda s, extra: None
    def _flat():
        stub._flats += 1
        return [{"act": "flatten", "verified_flat": True}]
    stub.flatten_all = _flat
    stub._apply_circuit_breaker = types.MethodType(R.HLXSRunner._apply_circuit_breaker, stub)
    return stub


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestCatastropheConfirmGuard(unittest.TestCase):
    def _run(self, stub, s, dd):
        return stub._apply_circuit_breaker(s, dd)

    def test_single_drawdown_read_does_not_trip_with_confirm_3(self):
        stub = _breaker_stub(confirm=3)
        s = XSState(equity=88.0, peak_equity=100.0, last_settled_equity=88.0)
        out = self._run(stub, s, 0.12)            # one read at the line
        self.assertIsNone(out)
        self.assertEqual(s.cb_state, "normal")
        self.assertEqual(s.catastrophe_streak, 1)

    def test_three_consecutive_reads_trip(self):
        stub = _breaker_stub(confirm=3)
        s = XSState(equity=88.0, peak_equity=100.0, last_settled_equity=88.0)
        self.assertIsNone(self._run(stub, s, 0.12))
        self.assertIsNone(self._run(stub, s, 0.13))
        out = self._run(stub, s, 0.14)
        self.assertEqual(s.cb_state, "catastrophe_halt")
        self.assertEqual(out["cb_state"], "catastrophe_halt")

    def test_transient_spike_then_recovery_resets_streak(self):
        stub = _breaker_stub(confirm=3)
        s = XSState(equity=88.0, peak_equity=100.0, last_settled_equity=88.0)
        self._run(stub, s, 0.12)                  # streak 1
        self._run(stub, s, 0.05)                  # recovered → reset
        self.assertEqual(s.catastrophe_streak, 0)
        self.assertEqual(s.cb_state, "normal")

    def test_intracycle_trips_immediately_despite_confirm(self):
        stub = _breaker_stub(confirm=3)
        # equity dropped 9% vs last settled in ONE cycle → flash-crash trigger
        s = XSState(equity=91.0, peak_equity=100.0, last_settled_equity=100.0)
        out = self._run(stub, s, 0.09)
        self.assertEqual(s.cb_state, "catastrophe_halt")
        self.assertEqual(out["cb_state"], "catastrophe_halt")

    def test_confirm_1_is_legacy_immediate(self):
        stub = _breaker_stub(confirm=1)
        s = XSState(equity=88.0, peak_equity=100.0, last_settled_equity=88.0)
        out = self._run(stub, s, 0.12)
        self.assertEqual(s.cb_state, "catastrophe_halt")
        self.assertEqual(out["cb_state"], "catastrophe_halt")

    def test_live_flattens_on_confirmed_trip(self):
        stub = _breaker_stub(live=True, confirm=2)
        s = XSState(equity=88.0, peak_equity=100.0, last_settled_equity=88.0)
        self._run(stub, s, 0.12)
        self._run(stub, s, 0.12)
        self.assertEqual(s.cb_state, "catastrophe_halt")
        self.assertGreaterEqual(stub._flats, 1)


def _delever_stub(*, live=True, cb="normal", sdd=0.06, factor=0.5,
                  book=None, min_order=10.0):
    cfg = R.HLXSConfig(soft_delever_dd_pct=sdd, soft_delever_factor=factor,
                       instance_name="t", slippage=0.02)
    calls = []
    adapter = types.SimpleNamespace(
        MIN_ORDER_USD=min_order,
        book_notional=lambda: dict(book or {}),
        make_cloid=lambda seed: seed,
        market_order_usd=lambda coin, is_buy, usd, slippage=None, cloid=None: (
            calls.append((coin, is_buy, round(usd, 2))) or
            types.SimpleNamespace(ok=True, error=None)))
    stub = types.SimpleNamespace(cfg=cfg, live_trading=live, adapter=adapter,
                                 _logs=[], _calls=calls)
    stub.log = lambda e: stub._logs.append(e)
    stub._maybe_delever = types.MethodType(R.HLXSRunner._maybe_delever, stub)
    return stub


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestSafetyDelever(unittest.TestCase):
    BOOK = {"BTC": 270.0, "ETH": 270.0, "ADA": -270.0, "DOT": -270.0}

    def test_trims_each_leg_to_factor_on_band_entry(self):
        stub = _delever_stub(book=self.BOOK)
        s = XSState(cb_state="normal", cycles_total=5)
        out = stub._maybe_delever(s, 0.07)
        self.assertEqual(out["action"], "safety_delever")
        self.assertTrue(s.delever_active)
        # each leg reduced by (1-0.5)*270 = 135; longs SELL (is_buy False), shorts BUY
        self.assertIn(("BTC", False, 135.0), stub._calls)
        self.assertIn(("ADA", True, 135.0), stub._calls)
        self.assertEqual(len(stub._calls), 4)

    def test_idempotent_when_already_delevered(self):
        stub = _delever_stub(book=self.BOOK)
        s = XSState(cb_state="normal", delever_active=True)
        self.assertIsNone(stub._maybe_delever(s, 0.08))
        self.assertEqual(stub._calls, [])

    def test_recovery_clears_episode(self):
        stub = _delever_stub(book=self.BOOK)
        s = XSState(cb_state="normal", delever_active=True)
        stub._maybe_delever(s, 0.02)              # < sdd*0.5 = 0.03
        self.assertFalse(s.delever_active)
        self.assertEqual(stub._calls, [])         # clear only, no trim

    def test_below_band_noop(self):
        stub = _delever_stub(book=self.BOOK)
        s = XSState(cb_state="normal")
        self.assertIsNone(stub._maybe_delever(s, 0.04))
        self.assertEqual(stub._calls, [])

    def test_sim_mode_never_trades(self):
        stub = _delever_stub(live=False, book=self.BOOK)
        s = XSState(cb_state="normal")
        self.assertIsNone(stub._maybe_delever(s, 0.08))
        self.assertEqual(stub._calls, [])

    def test_halted_state_noop(self):
        stub = _delever_stub(book=self.BOOK)
        s = XSState(cb_state="catastrophe_halt")
        self.assertIsNone(stub._maybe_delever(s, 0.08))
        self.assertEqual(stub._calls, [])

    def test_dust_leg_skipped(self):
        stub = _delever_stub(book={"BTC": 270.0, "ETH": 270.0,
                                   "ADA": -270.0, "DOT": -270.0, "XRP": 5.0})
        s = XSState(cb_state="normal")
        stub._maybe_delever(s, 0.08)
        self.assertNotIn("XRP", [c for c, _, _ in stub._calls])

    def test_disabled_when_sdd_none(self):
        stub = _delever_stub(sdd=None, book=self.BOOK)
        s = XSState(cb_state="normal")
        self.assertIsNone(stub._maybe_delever(s, 0.50))
        self.assertEqual(stub._calls, [])


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestResumeReanchor(unittest.TestCase):
    """A manually-cleared terminal halt (cb_state set back to 'normal' while
    last_cb_state is still terminal) must re-anchor peak to current equity +
    clear staleness counters on the first read — else the stale pre-halt peak
    re-trips instantly. _read_equity owns this transition."""

    def _stub(self, live=True):
        cfg = R.HLXSConfig()
        stub = types.SimpleNamespace(cfg=cfg, live_trading=live, _logs=[])
        stub.log = lambda e: stub._logs.append(e)
        stub.save_state = lambda s: None
        stub.write_health = lambda s, extra: None
        stub.equity = lambda s, mids: stub._eq
        stub._read_equity = types.MethodType(R.HLXSRunner._read_equity, stub)
        return stub

    def test_reanchor_on_cleared_catastrophe(self):
        stub = self._stub()
        stub._eq = 170.84
        # operator cleared cb_state→normal but left last_cb_state=catastrophe_halt;
        # stale peak 185.68 would be a 7.99% dd → must re-anchor to 170.84
        s = XSState(equity=170.84, peak_equity=185.68, cycles_total=4000,
                    last_settled_equity=185.0, cb_state="normal",
                    last_cb_state="catastrophe_halt", catastrophe_streak=2)
        dd, sc = stub._read_equity(s, {})
        self.assertIsNone(sc)
        self.assertAlmostEqual(s.peak_equity, 170.84)
        self.assertAlmostEqual(dd, 0.0)
        self.assertEqual(s.catastrophe_streak, 0)
        self.assertFalse(s.delever_active)
        self.assertTrue(any(e.get("action") == "resume_reanchor" for e in stub._logs))

    def test_no_reanchor_in_steady_state(self):
        stub = self._stub()
        stub._eq = 170.0
        s = XSState(equity=170.0, peak_equity=185.68, cycles_total=4000,
                    last_settled_equity=180.0, cb_state="normal", last_cb_state="normal")
        dd, sc = stub._read_equity(s, {})
        self.assertAlmostEqual(s.peak_equity, 185.68)        # untouched
        self.assertGreater(dd, 0.07)                          # real drawdown preserved


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestStreakNotPersisted(unittest.TestCase):
    """A restart mid-confirm-sequence must not inherit a partial streak."""

    def test_load_state_resets_streak(self):
        import json, tempfile
        with tempfile.TemporaryDirectory() as d:
            cfg = R.HLXSConfig(instance_name="x")
            runner = R.HLXSRunner.__new__(R.HLXSRunner)
            runner.cfg = cfg
            runner.live_trading = False
            runner.state_path = Path(d) / "state.json"
            s = XSState(equity=100.0, peak_equity=100.0, catastrophe_streak=2)
            runner.state_path.write_text(json.dumps(s.to_json()))
            loaded = R.HLXSRunner.load_state(runner)
            self.assertEqual(loaded.catastrophe_streak, 0)


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestConfirmConfigValidation(unittest.TestCase):
    def test_zero_confirm_rejected(self):
        import json, tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text(json.dumps({"network": "mainnet", "allow_live": False,
                                     "catastrophe_confirm_cycles": 0}))
            with self.assertRaises(ValueError):
                R.load_config(str(p))


if __name__ == "__main__":
    unittest.main()
