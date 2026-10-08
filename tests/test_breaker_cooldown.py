"""Breaker redesign (2026-10-08): cut off → cooldown → market-gated auto-resume.

Design: hl-lanes/LANE-hl-xsectional.md. Locks:
  1. a sharp drop flattens the book and enters `cooldown` (not terminal);
  2. no resume while the market is unsettled (nor before the minimum cooldown);
  3. resume after stabilisation, with the peak re-anchored to equity at resume;
  4. the hard floor (HWM-based) and the auto-resume budget stay TERMINAL;
  5. a restart during cooldown keeps the cooldown (state survives the process);
  6. the stability measure works on a testnet-like universe (no BTC, regime
     tag "unknown") and fails safe on missing data.

Network-free: venue and clock are stubbed.
"""

import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import xs_cooldown as C  # noqa: E402

try:
    import hl_xs_runner as R
    from xs_core import XSState
    HAVE_SDK = True
except ImportError:
    HAVE_SDK = False

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
TESTNET_UNIVERSE = ["ETH", "SOL", "BNB", "ADA", "AVAX", "DOGE"]


def _hourly(coins, *, hours=24 * 31 + 3, sigma=0.004, shock_last=0, shock_mult=5.0,
            drift_last=0.0, seed=7):
    """Synthetic hourly closes: common factor + idiosyncratic noise. Optionally
    the last `shock_last` hours get `shock_mult`× volatility (+ a drift)."""
    rng = np.random.default_rng(seed)
    common = rng.normal(0, sigma, hours)
    out = {}
    for c in coins:
        r = common + rng.normal(0, sigma, hours)
        if shock_last:
            r[-shock_last:] = (common[-shock_last:] * shock_mult
                               + rng.normal(0, sigma * shock_mult, shock_last)
                               + drift_last)
        out[c] = 100.0 * np.exp(np.cumsum(r))
    return out


class TestMarketStability(unittest.TestCase):
    def test_calm_market_is_stable(self):
        chk = C.market_stability(_hourly(TESTNET_UNIVERSE))
        self.assertTrue(chk["ok"], chk)
        self.assertEqual(chk["n_coins"], 6)

    def test_volatile_last_day_is_unsettled(self):
        chk = C.market_stability(_hourly(TESTNET_UNIVERSE, shock_last=24))
        self.assertFalse(chk["ok"])
        self.assertIn("unsettled", chk["reason"])
        self.assertGreater(chk["vol_ratio"], 1.5)

    def test_steady_grind_caught_by_move_ratio(self):
        # low-vol but one-directional slide: -0.6%/h for 24h on every coin
        chk = C.market_stability(_hourly(TESTNET_UNIVERSE, shock_last=24, shock_mult=1.0,
                                         drift_last=-0.006))
        self.assertFalse(chk["ok"])
        self.assertGreater(chk["move_ratio"], 3.0)

    def test_testnet_universe_without_btc_works(self):
        # the testnet universe has no BTC → the diagnostic regime tag is "unknown";
        # the stability measure doesn't depend on either and still yields a verdict
        import regime_tag
        closes_daily = {c: np.linspace(100, 110, 200) for c in TESTNET_UNIVERSE}
        self.assertEqual(regime_tag.compute_regime(closes_daily)["label"], "unknown")
        self.assertTrue(C.market_stability(_hourly(TESTNET_UNIVERSE))["ok"])

    def test_insufficient_data_fails_safe(self):
        short = {c: v[-100:] for c, v in _hourly(TESTNET_UNIVERSE).items()}
        chk = C.market_stability(short)
        self.assertFalse(chk["ok"])
        self.assertIn("insufficient", chk["reason"])
        self.assertFalse(C.market_stability({})["ok"])

    def test_too_few_coins_fails_safe(self):
        chk = C.market_stability(_hourly(["ETH", "SOL", "BNB"]), min_coins=4)
        self.assertFalse(chk["ok"])

    def test_hard_floor_and_window_helpers(self):
        self.assertAlmostEqual(C.hard_floor_equity(156.0, 0.30), 109.2)
        self.assertIsNone(C.hard_floor_equity(None, 0.30))
        self.assertIsNone(C.hard_floor_equity(156.0, 0.0))
        now = T0.timestamp()
        self.assertEqual(C.resumes_in_window([now - 31 * 86400, now - 86400], now, 30),
                         [now - 86400])


class _Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def _runner(state_path, equity_box, *, live=True, hourly=None, **cfg_over):
    """A real HLXSRunner (no adapter construction) on a temp state file; venue,
    flatten, reconcile and Telegram stubbed."""
    kw = dict(instance_name="x", catastrophe_confirm_cycles=3,
              catastrophe_drawdown_pct=0.12, catastrophe_intracycle_pct=0.08,
              halt_drawdown_pct=0.25, max_account_staleness_sec=0,
              venue_status_check=False, soft_delever_dd_pct=None,
              universe=list(TESTNET_UNIVERSE), min_assets=4)
    kw.update(cfg_over)
    r = R.HLXSRunner.__new__(R.HLXSRunner)
    r.cfg = R.HLXSConfig(**kw)
    r.live_trading = live
    r.mode = R.MODE_TESTNET if live else R.MODE_MAINNET_DRY
    r._venue_upgrade_until = 0.0
    r._delever_active = False
    r.state_path = Path(state_path)
    r.dir = r.state_path.parent
    r.trades_path = r.dir / "trades.log"
    r.health_path = r.dir / "health.json"
    r.flattens = 0
    r.notes = []
    r.logs = []
    r.health = {}

    def _flat():
        r.flattens += 1
        return [{"act": "flatten_verify", "verified_flat": True, "remaining": []}]
    r.flatten_all = _flat
    r.reconcile = lambda s, t: {"ok": True}
    r.log = lambda e: r.logs.append(e)
    r.write_health = lambda s, extra: r.health.update(R.breaker_health(r.cfg, s), extra=extra)
    r.hourly_calls = 0
    box = {"hourly": hourly if hourly is not None else _hourly(TESTNET_UNIVERSE)}

    def _hourly_closes(coins, hours):
        r.hourly_calls += 1
        return box["hourly"]
    r.market = box
    r.adapter = types.SimpleNamespace(
        all_mids=lambda: {}, account_value=lambda: equity_box[0],
        last_account_age_s=lambda: 0.0, exchange_status=lambda: None,
        hourly_closes=_hourly_closes, address="0xabc")
    return r


def _seed(path, **over):
    kw = dict(equity=100.0, peak_equity=100.0, last_settled_equity=100.0,
              hwm_equity=100.0, cycles_total=10, dry_run=False,
              last_rebalance_ts=T0.isoformat())
    kw.update(over)
    Path(path).write_text(json.dumps(XSState(**kw).to_json()))


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestCooldownFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Path(self.tmp.name) / "state.json"
        self.clock = _Clock(T0)
        self.patch = mock.patch.object(R, "_utcnow", self.clock)
        self.patch.start()
        self.notify = []
        fake = types.SimpleNamespace(send=lambda text, **k: self.notify.append(text) or True)
        self.npatch = mock.patch.dict(sys.modules, {"notify": fake})
        self.npatch.start()

    def tearDown(self):
        self.npatch.stop()
        self.patch.stop()
        self.tmp.cleanup()

    def _advance(self, **kw):
        self.clock.t = self.clock.t + timedelta(**kw)

    def _trip(self, r, eq):
        eq[0] = 90.0                                  # 10% drop in one cycle → intracycle
        out = r.run_safety_once()
        self.assertEqual(out["cb_state"], "cooldown")
        return out

    # 1 -------------------------------------------------------------------
    def test_sharp_drop_flattens_and_enters_cooldown(self):
        _seed(self.p)
        eq = [100.0]
        r = _runner(self.p, eq)
        self._trip(r, eq)
        s = r.load_state()
        self.assertEqual(s.cb_state, "cooldown")
        self.assertEqual(s.cooldown_trigger, "intracycle")
        self.assertEqual(s.cooldown_since_ts, T0.isoformat())
        self.assertGreaterEqual(r.flattens, 1)
        self.assertTrue(any(e["action"] == "cooldown_enter" for e in r.logs))
        self.assertEqual(len(self.notify), 1)
        self.assertIn("STOP", self.notify[0])
        cd = r.health["cooldown"]
        self.assertEqual(cd["min_until"], (T0 + timedelta(hours=24)).isoformat())
        self.assertFalse(cd["min_elapsed"])

    def test_deep_and_confirmed_drawdown_also_cool_down(self):
        _seed(self.p, last_settled_equity=87.0)
        eq = [87.0]                                   # 13% dd, no intracycle drop
        r = _runner(self.p, eq)
        r.run_safety_once(); r.run_safety_once()
        self.assertEqual(r.run_safety_once()["cb_state"], "cooldown")
        self.assertEqual(r.load_state().cooldown_trigger, "drawdown")

    # 2 -------------------------------------------------------------------
    def test_no_resume_before_minimum_cooldown(self):
        _seed(self.p)
        eq = [100.0]
        r = _runner(self.p, eq)
        self._trip(r, eq)
        for _ in range(5):
            self._advance(hours=4)                    # up to 20h: still inside S0
            self.assertEqual(r.run_safety_once()["cb_state"], "cooldown")
        self.assertEqual(r.hourly_calls, 0)           # market not even queried yet

    def test_no_resume_while_market_unsettled(self):
        _seed(self.p)
        eq = [100.0]
        r = _runner(self.p, eq, hourly=_hourly(TESTNET_UNIVERSE, shock_last=24))
        self._trip(r, eq)
        self._advance(hours=25)
        for _ in range(6):
            out = r.run_safety_once()
            self.assertEqual(out["cb_state"], "cooldown")
            self._advance(minutes=16)
        s = r.load_state()
        self.assertEqual(s.cooldown_stable_streak, 0)
        self.assertFalse(s.cooldown_last_check["ok"])
        self.assertGreaterEqual(r.flattens, 7)        # book held flat every cycle

    def test_checks_are_throttled(self):
        _seed(self.p)
        eq = [100.0]
        r = _runner(self.p, eq, hourly=_hourly(TESTNET_UNIVERSE, shock_last=24))
        self._trip(r, eq)
        self._advance(hours=25)
        r.run_safety_once()
        self._advance(minutes=3)
        r.run_safety_once()
        self.assertEqual(r.hourly_calls, 1)

    # 3 -------------------------------------------------------------------
    def test_resume_after_stabilisation_reanchors_peak(self):
        _seed(self.p)
        eq = [100.0]
        r = _runner(self.p, eq, hourly=_hourly(TESTNET_UNIVERSE, shock_last=24))
        self._trip(r, eq)
        self._advance(hours=25)
        self.assertEqual(r.run_safety_once()["cb_state"], "cooldown")   # unsettled
        r.market["hourly"] = _hourly(TESTNET_UNIVERSE)                  # market calms
        self._advance(minutes=16)
        self.assertEqual(r.run_safety_once()["cb_state"], "cooldown")   # 1/2 checks
        self._advance(minutes=16)
        out = r.run_safety_once()                                       # 2/2 → resume
        self.assertEqual(out["action"], "cooldown_resume")
        s = r.load_state()
        self.assertEqual(s.cb_state, "normal")
        self.assertAlmostEqual(s.peak_equity, 90.0)                     # re-anchored
        self.assertAlmostEqual(s.last_settled_equity, 90.0)
        self.assertIsNone(s.last_rebalance_ts)                          # next full cycle rebuilds
        self.assertAlmostEqual(s.hwm_equity, 100.0)                     # HWM NOT re-anchored
        self.assertEqual(len(s.auto_resume_ts), 1)
        self.assertIsNone(s.cooldown_since_ts)
        self.assertIn("RESUME", self.notify[-1])
        # next cycle trades normally from the new baseline: 90 → 0% drawdown
        self._advance(minutes=3)
        out = r.run_safety_once()
        self.assertEqual(out["action"], "safety")
        self.assertEqual(r.load_state().cb_state, "normal")

    # 4 -------------------------------------------------------------------
    def test_resume_budget_exhausted_goes_terminal(self):
        now = T0.timestamp()
        _seed(self.p, auto_resume_ts=[now - 10 * 86400, now - 3 * 86400])
        eq = [100.0]
        r = _runner(self.p, eq)
        eq[0] = 90.0
        out = r.run_safety_once()
        self.assertEqual(out["cb_state"], "catastrophe_halt")
        s = r.load_state()
        self.assertEqual(s.terminal_reason, "resume_budget")
        self.assertIn("TERMINAL", self.notify[-1])
        # terminal never auto-resumes, however calm the market and however long
        self._advance(days=5)
        self.assertEqual(r.run_safety_once()["cb_state"], "catastrophe_halt")
        self.assertEqual(r.hourly_calls, 0)

    def test_old_resumes_outside_window_do_not_count(self):
        now = T0.timestamp()
        _seed(self.p, auto_resume_ts=[now - 40 * 86400, now - 35 * 86400])
        eq = [100.0]
        r = _runner(self.p, eq)
        self._trip(r, eq)

    def test_hard_floor_goes_terminal_after_confirm(self):
        # HWM 156 → floor 109.2. Re-anchored peak 120 (after earlier resumes);
        # equity slides to 108 → below floor but only 10% from peak (no trip).
        _seed(self.p, equity=120.0, peak_equity=120.0, last_settled_equity=110.0,
              hwm_equity=156.0)
        eq = [108.0]
        r = _runner(self.p, eq)
        self.assertEqual(r.run_safety_once()["action"], "safety")
        self.assertEqual(r.run_safety_once()["action"], "safety")
        out = r.run_safety_once()
        self.assertEqual(out["cb_state"], "catastrophe_halt")
        self.assertEqual(r.load_state().terminal_reason, "hard_floor")
        self.assertGreaterEqual(r.flattens, 1)

    def test_hard_floor_during_cooldown_goes_terminal(self):
        _seed(self.p, equity=118.0, peak_equity=118.0, last_settled_equity=118.0,
              hwm_equity=156.0)
        eq = [118.0]
        r = _runner(self.p, eq)
        eq[0] = 106.0                                  # intracycle trip AND below floor
        self.assertEqual(r.run_safety_once()["cb_state"], "cooldown")
        r.run_safety_once()
        out = r.run_safety_once()
        self.assertEqual(out["cb_state"], "catastrophe_halt")
        self.assertEqual(r.load_state().terminal_reason, "hard_floor")

    def test_manual_clear_of_terminal_reanchors_hwm(self):
        _seed(self.p, equity=100.0, peak_equity=156.0, hwm_equity=156.0,
              cb_state="normal", last_cb_state="catastrophe_halt",
              auto_resume_ts=[T0.timestamp() - 86400], terminal_reason="hard_floor")
        eq = [100.0]
        r = _runner(self.p, eq)
        self.assertEqual(r.run_safety_once()["action"], "safety")
        s = r.load_state()
        self.assertAlmostEqual(s.peak_equity, 100.0)
        self.assertAlmostEqual(s.hwm_equity, 100.0)
        self.assertEqual(s.auto_resume_ts, [])
        self.assertIsNone(s.terminal_reason)

    def test_op_halt_stays_terminal(self):
        _seed(self.p, cb_state="op_halt")
        eq = [100.0]
        r = _runner(self.p, eq)
        self._advance(days=3)
        self.assertEqual(r.run_safety_once()["cb_state"], "op_halt")
        self.assertEqual(r.hourly_calls, 0)

    # 5 -------------------------------------------------------------------
    def test_restart_during_cooldown_keeps_cooldown(self):
        _seed(self.p)
        eq = [100.0]
        r1 = _runner(self.p, eq, hourly=_hourly(TESTNET_UNIVERSE, shock_last=24))
        self._trip(r1, eq)
        self._advance(hours=2)
        r2 = _runner(self.p, eq)                       # fresh process, calm market
        out = r2.run_safety_once()
        self.assertEqual(out["cb_state"], "cooldown")  # still inside S0 after restart
        s = r2.load_state()
        self.assertEqual(s.cooldown_since_ts, T0.isoformat())   # since preserved
        self.assertEqual(s.cooldown_trigger, "intracycle")
        self.assertGreaterEqual(r2.flattens, 1)        # new process keeps the book flat
        self._advance(hours=23)
        r2.run_safety_once()
        self._advance(minutes=16)
        self.assertEqual(r2.run_safety_once()["action"], "cooldown_resume")

    def test_legacy_halted_state_migrates_to_cooldown(self):
        _seed(self.p, cb_state="halted", peak_equity=140.0)
        eq = [100.0]
        r = _runner(self.p, eq)
        out = r.run_safety_once()
        self.assertEqual(out["cb_state"], "cooldown")
        s = r.load_state()
        self.assertEqual(s.cooldown_trigger, "legacy_halted")
        self.assertEqual(s.cooldown_since_ts, T0.isoformat())

    # 6 -------------------------------------------------------------------
    def test_missing_market_data_never_resumes(self):
        _seed(self.p)
        eq = [100.0]
        r = _runner(self.p, eq, hourly={})
        self._trip(r, eq)
        self._advance(hours=30)
        for _ in range(4):
            self.assertEqual(r.run_safety_once()["cb_state"], "cooldown")
            self._advance(minutes=16)
        self.assertIn("insufficient", r.load_state().cooldown_last_check["reason"])

    def test_sim_mode_cuts_sim_book_and_sends_no_telegram(self):
        _seed(self.p)
        eq = [100.0]
        r = _runner(self.p, eq, live=False)
        # sim equity is the sim mark; stub it to the box value
        r.equity = lambda s, mids: eq[0]
        self._trip(r, eq)
        s = r.load_state()
        self.assertEqual(r.flattens, 0)                # never a venue order in sim
        self.assertEqual(s.positions, {})
        self.assertAlmostEqual(s.cash, 90.0)
        self.assertEqual(self.notify, [])


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestConfigValidation(unittest.TestCase):
    def test_rejects_bad_floor(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"hard_floor_pct": 1.5}, f)
        with self.assertRaises(ValueError):
            R.load_config(f.name)


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestAsyncRiskMonitorQuietInCooldown(unittest.TestCase):
    def test_cb_halted_true_in_cooldown(self):
        import hl_runner_async as A
        stub = types.SimpleNamespace(_latest_health={"cb_state": "cooldown"})
        self.assertTrue(A.AsyncHLXSRunner._cb_halted(stub))


if __name__ == "__main__":
    unittest.main()
