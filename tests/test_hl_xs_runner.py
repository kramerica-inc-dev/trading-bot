"""Tests for the Hyperliquid momentum runner's pure logic (network-free).

The full loop (data + execution) is proven by the MAINNET_DRY integration run
and the testnet _execute_live proof; these cover target-building and the
simulated rebalance accounting without constructing the (networked) adapter.
"""

import json
import sys
import tempfile
import types
import unittest
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

try:
    import hl_xs_runner as R
    from xs_runner import XSState
    HAVE_SDK = True
except ImportError:
    HAVE_SDK = False


def _closes(finals):
    out = {}
    for sym, f in finals.items():
        cl = np.ones(200); cl[-1] = 1.0 + f
        out[sym] = cl
    return out


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestConfig(unittest.TestCase):
    def _write(self, d):
        import json, tempfile
        p = Path(tempfile.mkdtemp()) / "c.json"
        p.write_text(json.dumps(d))
        return str(p)

    def test_load_config_filters_unknown(self):
        cfg = R.load_config(self._write({"instance_name": "x", "m": 2, "network": "testnet", "bogus": 1}))
        self.assertEqual(cfg.m, 2)
        self.assertEqual(cfg.network, "testnet")
        self.assertFalse(hasattr(cfg, "bogus"))

    def test_load_config_rejects_string_allow_live(self):
        # the critical guard: a JSON-string boolean must NOT be accepted
        with self.assertRaises(ValueError):
            R.load_config(self._write({"network": "mainnet", "allow_live": "false"}))

    def test_load_config_rejects_bad_network(self):
        with self.assertRaises(ValueError):
            R.load_config(self._write({"network": "demo"}))


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestVerifyBook(unittest.TestCase):
    def _stub(self, book):
        cfg = R.HLXSConfig(m=2)
        adapter = types.SimpleNamespace(book_notional=lambda: book, MIN_ORDER_USD=10.0)
        return types.SimpleNamespace(cfg=cfg, adapter=adapter)

    def test_balanced_book_ok(self):
        stub = self._stub({"A": 1000.0, "B": 1000.0, "C": -1000.0, "D": -1000.0})
        ok, missing, _ = R.HLXSRunner._verify_book(stub, {"A": 1000.0, "B": 1000.0, "C": -1000.0, "D": -1000.0})
        self.assertTrue(ok)
        self.assertEqual(missing, {})

    def test_one_legged_book_fails(self):
        stub = self._stub({"A": 1000.0, "B": 1000.0})   # shorts never filled
        ok, missing, _ = R.HLXSRunner._verify_book(stub, {"A": 1000.0, "B": 1000.0, "C": -1000.0, "D": -1000.0})
        self.assertFalse(ok)
        self.assertEqual(sorted(missing), ["C", "D"])

    def test_size_skewed_book_fails(self):
        # count-balanced 2L/2S but shorts tiny -> not dollar-neutral
        stub = self._stub({"A": 1000.0, "B": 1000.0, "C": -100.0, "D": -100.0})
        ok, _, _ = R.HLXSRunner._verify_book(stub, {"A": 1000.0, "B": 1000.0, "C": -1000.0, "D": -1000.0})
        self.assertFalse(ok)


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestTargets(unittest.TestCase):
    def _stub(self, **over):
        cfg = R.HLXSConfig(lookback_days=120, m=2, gross_exposure=1.0,
                           cost_rate=0.0005, **over)
        stub = types.SimpleNamespace(cfg=cfg, mode="MAINNET_DRY", _logs=[])
        stub.log = lambda e: stub._logs.append(e)
        stub._apply_exposure_caps = types.MethodType(R.HLXSRunner._apply_exposure_caps, stub)
        return stub

    def test_targets_dollar_neutral(self):
        stub = self._stub()
        finals = {"A": 0.5, "B": 0.4, "C": 0.0, "D": -0.4, "E": -0.5}
        t = R.HLXSRunner._targets(stub, _closes(finals), 5000.0)
        self.assertAlmostEqual(sum(t.values()), 0.0, places=6)          # dollar-neutral
        # gross = 2m * (1/m) * gross_book = 2 * equity * gross_exposure (1x per side)
        self.assertAlmostEqual(sum(abs(v) for v in t.values()), 10000.0, places=3)
        self.assertEqual(sorted(k for k, v in t.items() if v > 0), ["A", "B"])
        self.assertEqual(sorted(k for k, v in t.items() if v < 0), ["D", "E"])


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestSimExecution(unittest.TestCase):
    def _stub(self):
        cfg = R.HLXSConfig(m=2, cost_rate=0.0005)
        return types.SimpleNamespace(cfg=cfg, mode="MAINNET_DRY")

    def test_sim_rebalance_dollar_neutral_and_fee(self):
        stub = self._stub()
        s = XSState(cash=5000.0, equity=5000.0, peak_equity=5000.0)
        targets = {"A": 1250.0, "B": 1250.0, "C": -1250.0, "D": -1250.0}
        mids = {k: 100.0 for k in "ABCD"}
        res = R.HLXSRunner._execute_sim(stub, s, targets, mids, datetime.now(timezone.utc))
        self.assertEqual(res["execution"], "sim")
        self.assertEqual(len(s.positions), 4)
        ln = sum(p.notional for p in s.positions.values() if p.side > 0)
        sn = sum(p.notional for p in s.positions.values() if p.side < 0)
        self.assertAlmostEqual(ln, sn, places=6)            # dollar-neutral
        self.assertGreater(s.fees_paid_total, 0.0)          # turnover fee
        self.assertEqual(sorted(res["longs"]), ["A", "B"])
        self.assertEqual(sorted(res["shorts"]), ["C", "D"])

    def test_sim_keeps_same_side_positions(self):
        from xs_runner import Position
        stub = self._stub()
        s = XSState(cash=5000.0, equity=5000.0, peak_equity=5000.0)
        s.positions = {"A": Position(side=1, notional=1250.0, entry_price=100.0, entered_ts="x")}
        # A stays long in the new target -> should NOT be re-traded (no churn)
        targets = {"A": 1250.0, "B": 1250.0, "C": -1250.0, "D": -1250.0}
        mids = {k: 100.0 for k in "ABCD"}
        R.HLXSRunner._execute_sim(stub, s, targets, mids, datetime.now(timezone.utc))
        self.assertEqual(s.positions["A"].entry_price, 100.0)   # untouched


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestAcceptPostTradeEquity(unittest.TestCase):
    A = staticmethod(R.HLXSRunner._accept_post_trade_equity) if HAVE_SDK else None
    REBAL_OK = {"action": "rebalance", "execution": "live", "complete": True}
    REBAL_FAIL = {"action": "rebalance", "execution": "live", "complete": False}

    def test_completed_live_low_read_rejected_as_transient(self):
        self.assertFalse(self.A(1000.0, 300.0, self.REBAL_OK))     # 30% of pre = settlement artifact

    def test_completed_live_normal_read_accepted(self):
        self.assertTrue(self.A(1000.0, 990.0, self.REBAL_OK))

    def test_incomplete_rebalance_low_read_accepted_as_real(self):
        # a failed/flattened book IS the realistic >50%-loss case -> must record it
        self.assertTrue(self.A(1000.0, 300.0, self.REBAL_FAIL))

    def test_noop_cycle_low_read_accepted(self):
        # no trade just happened -> a low read is real drift/loss, never a transient
        self.assertTrue(self.A(1000.0, 300.0, {"action": "noop"}))

    def test_sim_rebalance_not_treated_as_transient(self):
        self.assertTrue(self.A(1000.0, 300.0, {"action": "rebalance", "execution": "sim"}))

    def test_none_read_rejected(self):
        self.assertFalse(self.A(1000.0, None, self.REBAL_OK))


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestResizeOrder(unittest.TestCase):
    def rz(self, cur, tgt, th=0.10, mn=10.0):
        return R.HLXSRunner._resize_order(cur, tgt, th, mn)

    def test_within_tolerance_no_resize(self):
        self.assertIsNone(self.rz(320.0, 330.0))        # drift 10 < max(10, 33)

    def test_below_min_order_no_resize(self):
        self.assertIsNone(self.rz(100.0, 105.0))        # drift 5 < $10 min

    def test_grow_long(self):
        self.assertEqual(self.rz(300.0, 400.0), (True, 100.0))    # buy 100 more

    def test_trim_long(self):
        self.assertEqual(self.rz(500.0, 300.0), (False, 200.0))   # sell 200 (reduce)

    def test_grow_short(self):
        self.assertEqual(self.rz(-300.0, -400.0), (False, 100.0)) # sell 100 more

    def test_trim_short(self):
        self.assertEqual(self.rz(-500.0, -300.0), (True, 200.0))  # buy back 200

    def test_resize_never_flips_side(self):
        # the trim amount is always strictly less than the current notional
        for cur, tgt in [(500.0, 10.0), (-500.0, -10.0)]:
            ro = self.rz(cur, tgt)
            self.assertIsNotNone(ro)
            self.assertLess(ro[1], abs(cur))


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestHealthLivePositions(unittest.TestCase):
    def _stub(self, tmp, live, book):
        adapter = types.SimpleNamespace(wallet=object(), address="0xMaster",
                                        book_notional=lambda: book, MIN_ORDER_USD=10.0)
        return types.SimpleNamespace(cfg=R.HLXSConfig(), mode="TESTNET",
                                     live_trading=live, adapter=adapter,
                                     health_path=tmp / "health.json")

    def _write_and_read(self, stub, s):
        import json
        R.HLXSRunner.write_health(stub, s, {"last_action": "rebalance"})
        return json.loads((stub.health_path).read_text())

    def test_live_n_positions_from_venue_book(self):
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        book = {"A": 330.0, "B": 330.0, "C": -330.0, "D": -330.0, "E": -330.0, "F": 330.0}
        stub = self._stub(tmp, True, book)              # s.positions empty, but 6 live legs
        s = XSState(cash=990.0, equity=990.0, peak_equity=990.0)
        h = self._write_and_read(stub, s)
        self.assertEqual(h["n_positions"], 6)           # NOT 0 (the bug the audit caught)

    def test_dry_mode_n_positions_from_state(self):
        import tempfile
        from xs_runner import Position
        tmp = Path(tempfile.mkdtemp())
        stub = self._stub(tmp, False, {})               # not live -> use sim dict
        s = XSState(cash=5000.0, equity=5000.0, peak_equity=5000.0)
        s.positions = {"A": Position(side=1, notional=1.0, entry_price=1.0, entered_ts="x")}
        h = self._write_and_read(stub, s)
        self.assertEqual(h["n_positions"], 1)


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestInsightNetBetaDry(unittest.TestCase):
    """The net-beta gauge + per-leg book must be published in sim/DRY too — not
    only in live mode — so the pro-cyclical-tilt risk is visible during the
    MAINNET_DRY forward-paper window (the gap the leaderboard analysis surfaced).
    """

    @staticmethod
    def _varying(syms, n=200):
        t = np.arange(n, dtype=float)
        return {sym: 100.0 + 5.0 * np.sin((t + i * 7) / 9.0) + 0.02 * t
                for i, sym in enumerate(syms)}

    def _stub(self):
        stub = types.SimpleNamespace(
            cfg=R.HLXSConfig(lookback_days=120),
            live_trading=False,
            adapter=types.SimpleNamespace(daily_closes=lambda syms, n: {}),
        )
        # _build_insight calls self._book_snapshot / self._net_beta — bind the
        # real implementations so the unbound-method test path exercises them.
        stub._book_snapshot = types.MethodType(R.HLXSRunner._book_snapshot, stub)
        stub._net_beta = types.MethodType(R.HLXSRunner._net_beta, stub)
        return stub

    def test_net_beta_and_book_published_in_dry(self):
        from xs_runner import Position
        stub = self._stub()
        closes = self._varying(["BTC", "ETH", "SOL"])
        s = XSState(cash=5000.0, equity=5000.0, peak_equity=5000.0)
        s.positions = {
            "ETH": Position(side=1, notional=1000.0,
                            entry_price=float(closes["ETH"][-1]), entered_ts="x"),
            "SOL": Position(side=-1, notional=1000.0,
                            entry_price=float(closes["SOL"][-1]), entered_ts="x"),
        }
        mids = {"ETH": float(closes["ETH"][-1]), "SOL": float(closes["SOL"][-1])}
        out = R.HLXSRunner._build_insight(stub, s, closes, mids, 5000.0)
        self.assertIn("momentum", out)
        self.assertEqual(out.get("book_source"), "sim")     # from the sim basket
        self.assertEqual(len(out["book"]), 2)
        self.assertIn("net_beta", out)                      # the gauge, lit in DRY
        self.assertIsInstance(out["net_beta"], float)
        # signed notionals: long ETH positive, short SOL negative
        by = {b["coin"]: b for b in out["book"]}
        self.assertGreater(by["ETH"]["notional"], 0)
        self.assertLess(by["SOL"]["notional"], 0)

    def test_no_positions_no_book_no_crash(self):
        stub = self._stub()
        closes = self._varying(["BTC", "ETH", "SOL"])
        s = XSState(cash=5000.0, equity=5000.0, peak_equity=5000.0)
        out = R.HLXSRunner._build_insight(stub, s, closes, {}, 5000.0)
        self.assertIn("momentum", out)
        self.assertNotIn("book", out)
        self.assertNotIn("net_beta", out)


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestCatastropheBreaker(unittest.TestCase):
    """(B) the terminal catastrophe breaker: fires at threshold, does NOT
    auto-resume, and a transient None / settling-low read can't trip it."""

    def _stub(self, live, **over):
        cfg = R.HLXSConfig(halt_drawdown_pct=0.25, catastrophe_drawdown_pct=0.12,
                           catastrophe_intracycle_pct=0.08, **over)
        tmp = Path(tempfile.mkdtemp())
        flat_calls = []
        stub = types.SimpleNamespace(
            cfg=cfg, live_trading=live, mode="MAINNET_LIVE" if live else "MAINNET_DRY",
            health_path=tmp / "health.json", state_path=tmp / "state.json",
            adapter=types.SimpleNamespace(wallet=object(), address="0xM",
                                          book_notional=lambda: {}, MIN_ORDER_USD=10.0),
            _flat_calls=flat_calls, _logs=[])
        stub.flatten_all = lambda: (flat_calls.append(True) or [{"act": "flatten"}])
        stub.log = lambda e: stub._logs.append(e)
        stub.equity = lambda st, mids: st.equity        # echo the seeded settled equity
        stub.skips_total = 0
        stub.save_state = types.MethodType(R.HLXSRunner.save_state, stub)
        stub.write_health = types.MethodType(R.HLXSRunner.write_health, stub)
        return stub

    def _cb(self, stub, s, dd):
        return R.HLXSRunner._apply_circuit_breaker(stub, s, dd)

    def test_catastrophe_drawdown_fires_and_flattens(self):
        stub = self._stub(live=True)
        s = XSState(cash=880.0, equity=880.0, peak_equity=1000.0)   # 12% dd
        res = self._cb(stub, s, dd=0.12)
        self.assertEqual(s.cb_state, "catastrophe_halt")
        self.assertEqual(res["cb_state"], "catastrophe_halt")
        self.assertTrue(stub._flat_calls)                          # flattened
        self.assertTrue(any(l.get("action") == "catastrophe_halt" for l in stub._logs))

    def test_catastrophe_does_not_auto_resume(self):
        # once terminal, even a full recovery (dd→0) must NOT clear it
        stub = self._stub(live=True)
        s = XSState(cash=1000.0, equity=1000.0, peak_equity=1000.0, cb_state="catastrophe_halt")
        res = self._cb(stub, s, dd=0.0)
        self.assertEqual(s.cb_state, "catastrophe_halt")           # still terminal
        self.assertEqual(res["action"], "halted")

    def test_intracycle_drop_fires(self):
        stub = self._stub(live=True)
        s = XSState(cash=910.0, equity=910.0, peak_equity=1000.0)  # only 9% dd-from-peak
        s.last_settled_equity = 1000.0                             # but a 9% drop this cycle
        res = self._cb(stub, s, dd=0.09)
        self.assertEqual(s.cb_state, "catastrophe_halt")
        self.assertEqual(res["cb_state"], "catastrophe_halt")

    def test_below_threshold_does_not_fire(self):
        stub = self._stub(live=True)
        s = XSState(cash=950.0, equity=950.0, peak_equity=1000.0)  # 5% dd
        s.last_settled_equity = 970.0                              # ~2% intracycle
        res = self._cb(stub, s, dd=0.05)
        self.assertIsNone(res)
        self.assertEqual(s.cb_state, "normal")
        self.assertFalse(stub._flat_calls)
        self.assertEqual(s.last_settled_equity, 950.0)            # recorded the accepted equity

    def test_transient_none_read_does_not_trip(self):
        # _read_equity must reject a None equity BEFORE the breaker ever sees it
        stub = self._stub(live=True)
        s = XSState(cash=1000.0, equity=1000.0, peak_equity=1000.0)
        s.cycles_total = 5
        stub.equity = lambda st, mids: None
        stub.skips_total = 0
        dd, sc = R.HLXSRunner._read_equity(stub, s, {})
        self.assertIsNone(dd)
        self.assertEqual(sc["action"], "skip")
        self.assertNotEqual(s.cb_state, "catastrophe_halt")

    def test_settling_low_read_via_accept_guard_not_recorded(self):
        # the post-rebalance settlement artifact is filtered by _accept_post_trade_equity,
        # so a transient <50% read never updates settled equity / fabricates a drop
        A = R.HLXSRunner._accept_post_trade_equity
        self.assertFalse(A(1000.0, 300.0, {"action": "rebalance", "execution": "live",
                                            "complete": True}))

    def test_first_cycle_no_false_catastrophe(self):
        # cycle 1: no prior settled equity, peak anchored to current -> no trip
        stub = self._stub(live=True)
        s = XSState(cash=57.0, equity=57.0, peak_equity=57.0)      # the ~$57 live book
        s.cycles_total = 1
        s.last_settled_equity = None
        dd, sc = R.HLXSRunner._read_equity(stub, s, {})
        self.assertEqual(dd, 0.0)
        self.assertIsNone(sc)
        self.assertIsNone(self._cb(stub, s, dd))
        self.assertEqual(s.cb_state, "normal")


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestRunSafetyOnce(unittest.TestCase):
    """(A) run_safety_once checks equity/CB/reconcile WITHOUT rebalancing and
    flattens on a breaker; sim/flat books no-op cleanly."""

    def _runner(self, live, equity, peak=1000.0, **over):
        cfg = R.HLXSConfig(catastrophe_drawdown_pct=0.12, **over)
        tmp = Path(tempfile.mkdtemp())
        flat_calls = []
        stub = types.SimpleNamespace(
            cfg=cfg, live_trading=live, mode="MAINNET_LIVE" if live else "MAINNET_DRY",
            dir=tmp, health_path=tmp / "health.json", state_path=tmp / "state.json",
            trades_path=tmp / "trades.log",
            adapter=types.SimpleNamespace(wallet=object(), address="0xM",
                                          all_mids=lambda: {}, book_notional=lambda: {},
                                          MIN_ORDER_USD=10.0),
            _flat_calls=flat_calls, _rebal_calls=[])
        stub.flatten_all = lambda: (flat_calls.append(True) or [{"act": "flatten"}])
        stub.equity = lambda st, mids: equity
        # seed persisted state at the given equity/peak (cycles_total>0 so the
        # cycle-1 peak-anchor doesn't reset our seeded peak)
        s0 = XSState(cash=equity, equity=equity, peak_equity=peak, cycles_total=3,
                     started_ts="x", dry_run=not live)
        stub.load_state = lambda: XSState.from_json(json.loads(json.dumps(s0.to_json()))) \
            if stub.state_path.exists() else s0
        # bind the real methods under test
        for name in ("run_safety_once", "_read_equity", "_apply_circuit_breaker",
                     "reconcile", "save_state", "write_health", "log"):
            setattr(stub, name, types.MethodType(getattr(R.HLXSRunner, name), stub))
        # tripwire: rebalance/order paths must NOT be touched by the safety cycle
        stub._execute_live = lambda *a, **k: stub._rebal_calls.append("live")
        stub._execute_sim = lambda *a, **k: stub._rebal_calls.append("sim")
        return stub

    def test_safety_no_rebalance_on_normal_book(self):
        stub = self._runner(live=True, equity=1000.0)
        r = stub.run_safety_once()
        self.assertEqual(r["action"], "safety")
        self.assertEqual(stub._rebal_calls, [])        # never rebalanced
        self.assertFalse(stub._flat_calls)             # nothing tripped

    def test_safety_flattens_on_catastrophe(self):
        stub = self._runner(live=True, equity=850.0, peak=1000.0)   # 15% dd > 12%
        r = stub.run_safety_once()
        self.assertEqual(r["action"], "halted")
        self.assertEqual(r["cb_state"], "catastrophe_halt")
        self.assertTrue(stub._flat_calls)              # flattened on breaker
        self.assertEqual(stub._rebal_calls, [])        # still no rebalance

    def test_safety_sim_mode_no_orders(self):
        stub = self._runner(live=False, equity=850.0, peak=1000.0)  # breaker, but sim
        r = stub.run_safety_once()
        self.assertEqual(r["cb_state"], "catastrophe_halt")
        self.assertFalse(stub._flat_calls)             # sim never places venue orders
        self.assertEqual(stub._rebal_calls, [])

    def test_transient_book_read_failure_does_not_flatten(self):
        # Regression (2026-06-05): a transient venue-read failure (e.g. a 502
        # Bad Gateway) during reconcile must NOT flatten + op_halt the live book.
        # It's transient -> hold + retry; a persistent outage is the watchdog's job.
        stub = self._runner(live=True, equity=1000.0)  # equity fine -> no breaker
        def boom():
            raise RuntimeError("(502, 'Bad Gateway')")
        stub.adapter.book_notional = boom
        r = stub.run_safety_once()
        self.assertEqual(r["action"], "safety")        # ran, did not halt
        self.assertFalse(r["reconcile_ok"])            # reconcile reported the read error
        self.assertFalse(stub._flat_calls)             # but the book was NOT flattened
        saved = XSState.from_json(json.loads(stub.state_path.read_text()))
        self.assertEqual(saved.cb_state, "normal")     # and NOT op_halted


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestForceRebalance(unittest.TestCase):
    """--force-rebalance overrides the rebal_days timer for a one-shot operator
    rebalance, without affecting the normal cadence when unset."""

    def _sr(self, force, last_ts, now):
        stub = types.SimpleNamespace(cfg=R.HLXSConfig(rebal_days=5),
                                     _force_rebalance=force)
        s = XSState(cash=1.0, equity=1.0, peak_equity=1.0)
        s.last_rebalance_ts = last_ts
        return types.MethodType(R.HLXSRunner._should_rebalance, stub)(s, now)

    def test_force_overrides_recent_rebalance(self):
        now = datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)
        recent = datetime(2026, 6, 5, 11, 15, tzinfo=timezone.utc).isoformat()  # ~1d ago
        self.assertFalse(self._sr(False, recent, now))   # not due, no override
        self.assertTrue(self._sr(True, recent, now))     # forced -> rebalance now

    def test_unset_force_keeps_timer(self):
        now = datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)
        old = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc).isoformat()        # >5d ago
        self.assertTrue(self._sr(False, old, now))       # genuinely due
        self.assertTrue(self._sr(False, None, now))      # first-ever rebalance


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestExposureCaps(unittest.TestCase):
    """B2: absolute USD ceilings scale the basket down, preserving neutrality."""

    def _stub(self, **over):
        stub = types.SimpleNamespace(cfg=R.HLXSConfig(**over), _logs=[])
        stub.log = lambda e: stub._logs.append(e)
        stub._apply_exposure_caps = types.MethodType(R.HLXSRunner._apply_exposure_caps, stub)
        return stub

    def test_no_caps_is_noop(self):
        stub = self._stub()                      # max_gross_usd/max_net_usd default None
        t = {"A": 1000.0, "B": 1000.0, "C": -1000.0, "D": -1000.0}
        out = stub._apply_exposure_caps(dict(t))
        self.assertEqual(out, t)
        self.assertEqual(stub._logs, [])         # nothing capped → nothing logged

    def test_gross_cap_scales_and_preserves_neutrality(self):
        stub = self._stub(max_gross_usd=1000.0)
        t = {"A": 1000.0, "B": 1000.0, "C": -1000.0, "D": -1000.0}   # gross 4000
        out = stub._apply_exposure_caps(t)
        self.assertAlmostEqual(sum(abs(v) for v in out.values()), 1000.0, places=6)
        self.assertAlmostEqual(sum(out.values()), 0.0, places=6)     # still neutral
        self.assertTrue(any(l.get("action") == "exposure_capped" for l in stub._logs))

    def test_net_cap_bounds_directional(self):
        stub = self._stub(max_net_usd=100.0)
        t = {"A": 1000.0, "B": -500.0}           # net 500, gross 1500
        out = stub._apply_exposure_caps(t)
        self.assertAlmostEqual(abs(sum(out.values())), 100.0, places=6)

    def test_cap_not_triggered_when_under(self):
        stub = self._stub(max_gross_usd=10000.0)
        t = {"A": 1000.0, "B": -1000.0}          # gross 2000 < 10000
        out = stub._apply_exposure_caps(dict(t))
        self.assertEqual(out, t)
        self.assertEqual(stub._logs, [])


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestSimFunding(unittest.TestCase):
    """A1: MAINNET_DRY paper P&L accrues funding so it isn't optimistic."""

    def _stub(self, live, rates=None):
        stub = types.SimpleNamespace(
            live_trading=live, cfg=R.HLXSConfig(flat_funding_annual=0.0365),
            adapter=types.SimpleNamespace(funding_daily=lambda coins: (rates or {})))
        stub._accrue_sim_funding = types.MethodType(R.HLXSRunner._accrue_sim_funding, stub)
        return stub

    def _state_with_long(self):
        from xs_runner import Position
        s = XSState(cash=5000.0, equity=5000.0, peak_equity=5000.0,
                    started_ts="2026-06-01T00:00:00+00:00")
        s.positions = {"BTC": Position(side=1, notional=1000.0, entry_price=100.0, entered_ts="x")}
        s.last_funding_ts = "2026-06-05T00:00:00+00:00"
        return s

    def test_funding_charged_on_held_long(self):
        stub = self._stub(live=False)            # flat fallback 0.0365/yr → 0.0001/day
        s = self._state_with_long()
        now = datetime(2026, 6, 6, 0, 0, tzinfo=timezone.utc)   # 1 day since last accrual
        f = stub._accrue_sim_funding(s, now)
        self.assertAlmostEqual(f, -1000.0 * (0.0365 / 365.0) * 1.0, places=6)  # long pays
        self.assertLess(s.cash, 5000.0)
        self.assertGreater(s.funding_paid_total, 0.0)
        self.assertEqual(s.last_funding_ts, now.isoformat())

    def test_live_mode_is_noop(self):
        stub = self._stub(live=True)
        s = self._state_with_long()
        self.assertEqual(stub._accrue_sim_funding(s, datetime(2026, 6, 6, tzinfo=timezone.utc)), 0.0)
        self.assertEqual(s.cash, 5000.0)         # live funding is in account_value, not here

    def test_no_positions_is_noop(self):
        stub = self._stub(live=False)
        s = XSState(cash=5000.0, equity=5000.0, peak_equity=5000.0, started_ts="x")
        self.assertEqual(stub._accrue_sim_funding(s, datetime(2026, 6, 6, tzinfo=timezone.utc)), 0.0)

    def test_per_asset_rate_used_over_flat(self):
        stub = self._stub(live=False, rates={"BTC": 0.01})   # 1%/day predicted
        s = self._state_with_long()
        now = datetime(2026, 6, 6, 0, 0, tzinfo=timezone.utc)
        f = stub._accrue_sim_funding(s, now)
        self.assertAlmostEqual(f, -1000.0 * 0.01 * 1.0, places=6)   # uses per-asset, not flat


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestFlattenAllHardened(unittest.TestCase):
    """A3: flatten_all retries the read and VERIFIES flat — never a benign-looking
    'flattened' while a read failure left positions open."""

    class _Adapter:
        def __init__(self, snaps):
            self._snaps = list(snaps)
            self.closed = []
        def positions(self):
            snap = self._snaps.pop(0) if self._snaps else {}
            if isinstance(snap, Exception):
                raise snap
            return snap
        def close(self, coin):
            self.closed.append(coin)
            return types.SimpleNamespace(ok=True, error=None)

    def _stub(self, adapter):
        return types.SimpleNamespace(adapter=adapter)

    def test_unconfirmed_when_read_always_fails(self):
        ad = self._Adapter([RuntimeError("502")] * 3)
        stub = self._stub(ad)
        with unittest.mock.patch.object(R.time, "sleep", lambda *_: None):
            out = R.HLXSRunner.flatten_all(stub)
        self.assertFalse(out[0]["ok"])
        self.assertFalse(out[0]["verified_flat"])
        self.assertIn("UNCONFIRMED", out[0]["err"])

    def test_verified_flat_after_close(self):
        ad = self._Adapter([{"BTC": {}, "ETH": {}}, {}])   # read, then empty on verify
        out = R.HLXSRunner.flatten_all(self._stub(ad))
        self.assertEqual(sorted(ad.closed), ["BTC", "ETH"])
        self.assertTrue(out[-1]["verified_flat"])

    def test_straggler_reclosed(self):
        ad = self._Adapter([{"BTC": {}}, {"BTC": {}}, {}])  # still there on 1st verify
        out = R.HLXSRunner.flatten_all(self._stub(ad))
        self.assertTrue(any(o.get("act") == "flatten_retry" for o in out))
        self.assertTrue(out[-1]["verified_flat"])


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestEnsureLeverage(unittest.TestCase):
    """B2: per-coin leverage pinned once per process; no-op in sim."""

    def _stub(self, live):
        calls = []
        stub = types.SimpleNamespace(
            _leverage_set=False, live_trading=live,
            cfg=R.HLXSConfig(max_leverage=5, universe=["BTC", "ETH"]), _logs=[], _calls=calls,
            adapter=types.SimpleNamespace(
                set_leverage=lambda c, lev: (calls.append((c, lev)) or {"ok": True})))
        stub.log = lambda e: stub._logs.append(e)
        stub._ensure_leverage = types.MethodType(R.HLXSRunner._ensure_leverage, stub)
        return stub

    def test_pins_once_then_idempotent(self):
        stub = self._stub(live=True)
        stub._ensure_leverage(None)
        self.assertEqual(stub._calls, [("BTC", 5), ("ETH", 5)])
        self.assertTrue(stub._leverage_set)
        stub._ensure_leverage(None)               # second call no-op
        self.assertEqual(len(stub._calls), 2)

    def test_sim_mode_no_leverage_calls(self):
        stub = self._stub(live=False)
        stub._ensure_leverage(None)
        self.assertEqual(stub._calls, [])


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestDataOutage(unittest.TestCase):
    """B1/A2: a data outage increments a streak and skips; after the threshold an
    open live book is flattened rather than held blind; sim never places orders."""

    def _stub(self, live, *, book=None, cb="normal", max_cycles=3):
        tmp = Path(tempfile.mkdtemp())
        flat = []
        stub = types.SimpleNamespace(
            cfg=R.HLXSConfig(max_data_outage_cycles=max_cycles, data_staleness_hours=36),
            live_trading=live, mode="MAINNET_LIVE" if live else "MAINNET_DRY",
            dir=tmp, state_path=tmp / "state.json", health_path=tmp / "health.json",
            trades_path=tmp / "trades.log", _flat=flat,
            adapter=types.SimpleNamespace(book_notional=lambda: (book or {}),
                                          MIN_ORDER_USD=10.0, wallet=object(), address="0xM"))
        stub.flatten_all = lambda: (flat.append(True) or [{"act": "flatten"}])
        for name in ("_handle_data_outage", "save_state", "write_health", "log"):
            setattr(stub, name, types.MethodType(getattr(R.HLXSRunner, name), stub))
        return stub

    def _state(self, streak=0, cb="normal"):
        return XSState(cash=900.0, equity=900.0, peak_equity=1000.0,
                       cycles_total=4, started_ts="x", cb_state=cb, data_outage_streak=streak)

    def test_below_threshold_skips_no_flatten(self):
        stub = self._stub(live=True, book={"BTC": 500.0})
        s = self._state(streak=0)
        r = stub._handle_data_outage(s, {}, None, {})
        self.assertEqual(r["action"], "skip")
        self.assertEqual(s.data_outage_streak, 1)
        self.assertFalse(stub._flat)

    def test_threshold_with_live_book_flattens_and_halts(self):
        stub = self._stub(live=True, book={"BTC": 500.0, "ETH": -500.0})
        s = self._state(streak=2)                 # this call makes it 3 == threshold
        r = stub._handle_data_outage(s, {}, None, {})
        self.assertEqual(r["action"], "data_outage_flatten")
        self.assertEqual(s.cb_state, "op_halt")
        self.assertTrue(stub._flat)

    def test_threshold_but_no_book_does_not_flatten(self):
        stub = self._stub(live=True, book={})      # no open positions
        s = self._state(streak=2)
        r = stub._handle_data_outage(s, {}, None, {})
        self.assertEqual(r["action"], "skip")
        self.assertFalse(stub._flat)
        self.assertEqual(s.cb_state, "normal")

    def test_sim_mode_never_flattens(self):
        stub = self._stub(live=False, book={"BTC": 500.0})
        s = self._state(streak=5)
        r = stub._handle_data_outage(s, {}, None, {})
        self.assertEqual(r["action"], "skip")
        self.assertFalse(stub._flat)

    def test_stale_reason_reported(self):
        stub = self._stub(live=True, book={})
        s = self._state(streak=0)
        r = stub._handle_data_outage(s, {"BTC": np.ones(200)}, 50.0, {"BTC": 1.0})
        self.assertIn("stale", r["reason"])


if __name__ == "__main__":
    unittest.main()
