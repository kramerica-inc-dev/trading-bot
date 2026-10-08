"""Carry-hl lane hardening (2026-10-08): DRY-RUN book simulation + the six
blockers from the 2026-07-30 review.

  * DRY simulation: open sized like `go`, HL taker fees, funding accrued from
    SETTLED history (idempotent), basis mark-to-market, close realises P&L,
    health/dashboard carry the result.
  * #1 watcher uses the read-only --gate-only path (no state, no creds).
  * #2 single-instance flock; duplicate unit retired.
  * #3 `carry_hl_go go` validates first, archives DRY state, rolls back.
  * #4 gate_min_samples = 1080 on the HL config (= backtest).
  * #5 Telegram alerts + carry block for the daily health report.
  * live guard: a state file carrying a DRY simulation never trades live.
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import carry_runner as cr  # noqa: E402
import carry_hl_go as go  # noqa: E402
import carry_health_check as chc  # noqa: E402
from carry_position import go_sizing  # noqa: E402
from mode_gate import MODE_TESTNET  # noqa: E402
from test_carry_hl import (  # noqa: E402
    _MASTER, _StubHLLiveShim, _StubHLShim, _clean_env, _make_hl_runner,
)

SPOT_T = 0.0007     # HL base-tier spot taker (hl_carry_adapter HL_STATIC_FEES)
PERP_T = 0.00045    # HL base-tier perp taker
RATE_ON = 1.2e-05   # 10.5%/yr hourly


def _rows(rates_with_ms):
    """Funding-history envelope, newest-first, with real ms timestamps."""
    data = [{"fundingRate": repr(r), "fundingTime": str(t)}
            for t, r in sorted(rates_with_ms, key=lambda x: -x[0])]
    return {"code": "0", "msg": "", "data": data}


def _now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _sim_runner(tmp, stub, **kw):
    kw.setdefault("sim_deposit_usd", 1500.0)
    kw.setdefault("sim_spot_fraction", 0.55)
    kw.setdefault("live_max_usd", 1000.0)
    return _make_hl_runner(tmp, stub, **kw)


def _age_refresh(runner):
    st = runner.load_state()
    st.funding_window_last_refresh_ts = "2026-01-01T00:00:00+00:00"
    runner.save_state(st)


# =========================  DRY simulation  =========================

class TestDrySimulation(unittest.TestCase):

    def setUp(self):
        _clean_env()
        p = patch.object(cr, "notify", MagicMock())
        self.notify = p.start()
        self.addCleanup(p.stop)

    def test_gate_on_opens_simulated_book_sized_like_go(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = _StubHLShim(spot="60000.0", perp="60010.0",
                               funding_history=[RATE_ON] * 240)
            r = _sim_runner(Path(tmp), stub)
            e = r.one_cycle()
            self.assertEqual(e["action"]["kind"], "would_open")
            self.assertTrue(e["action"]["simulated"])
            per_leg = go_sizing(825.0, 675.0, 1000.0, 0.6)["per_leg_notional_usd"]
            self.assertAlmostEqual(per_leg, 808.82)
            st = r.load_state()
            qty = per_leg / 60000.0
            self.assertAlmostEqual(st.simulated_position["spot_qty"], qty)
            self.assertAlmostEqual(st.simulated_position["perp_qty"], -qty)
            fees = qty * 60000.0 * SPOT_T + qty * 60010.0 * PERP_T
            self.assertAlmostEqual(st.simulated_position["fees_paid"], fees)
            self.assertIsNotNone(st.sim_started_ts)
            self.assertEqual(st.sim_deposit_usd, 1500.0)
            self.assertAlmostEqual(st.simulated_equity, 1500.0 - fees)
            stub.place_spot_order.assert_not_called()
            stub.place_order.assert_not_called()
            # reconcile stays clean on the simulated book
            self.assertTrue(e["reconcile"]["ok"], e["reconcile"])

    def test_dry_uses_hl_static_fees_without_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = _StubHLShim(funding_history=[RATE_ON] * 240)
            r = _sim_runner(Path(tmp), stub)
            r.one_cycle()
            self.assertEqual(r.fees["spot_taker"], SPOT_T)
            self.assertEqual(r.fees["perp_taker"], PERP_T)

    def test_second_cycle_holds_and_does_not_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = _StubHLShim(funding_history=[RATE_ON] * 240)
            r = _sim_runner(Path(tmp), stub)
            r.one_cycle()
            e2 = r.one_cycle()
            self.assertEqual(e2["action"]["kind"], "noop")

    def test_funding_accrues_from_settled_rows_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = _StubHLShim(spot="60000.0", perp="60000.0",
                               funding_history=[RATE_ON] * 240)
            r = _sim_runner(Path(tmp), stub)
            r.one_cycle()
            st = r.load_state()
            open_ms = st.funding_last_accrued_ms
            qty = st.simulated_position["spot_qty"]
            hist = [(open_ms - k * 3_600_000, RATE_ON) for k in range(1, 200)]
            after = [(open_ms + k * 3_600_000, 2e-05) for k in (1, 2, 3)]
            stub.api.get_funding_rate_history.return_value = _rows(hist + after)
            _age_refresh(r)
            e = r.one_cycle()
            exp = 3 * qty * 60000.0 * 2e-05
            self.assertEqual(e["funding_accrual"]["settlements"], 3)
            self.assertAlmostEqual(e["funding_accrual"]["usd"], exp)
            self.assertAlmostEqual(
                r.load_state().simulated_position["funding_accrued"], exp)
            # same rows again → nothing double-counted
            _age_refresh(r)
            e = r.one_cycle()
            self.assertEqual(e["funding_accrual"]["settlements"], 0)
            self.assertAlmostEqual(
                r.load_state().simulated_position["funding_accrued"], exp)

    def test_negative_funding_costs_the_short(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = _StubHLShim(spot="60000.0", perp="60000.0",
                               funding_history=[RATE_ON] * 240)
            r = _sim_runner(Path(tmp), stub)
            r.one_cycle()
            open_ms = r.load_state().funding_last_accrued_ms
            hist = [(open_ms - k * 3_600_000, RATE_ON) for k in range(1, 240)]
            stub.api.get_funding_rate_history.return_value = _rows(
                hist + [(open_ms + 3_600_000, -1e-05)])
            _age_refresh(r)
            r.one_cycle()
            self.assertLess(r.load_state().simulated_position["funding_accrued"], 0)

    def test_basis_is_marked_to_market(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = _StubHLShim(spot="60000.0", perp="60010.0",
                               funding_history=[RATE_ON] * 240)
            r = _sim_runner(Path(tmp), stub)
            r.one_cycle()
            qty = r.load_state().simulated_position["spot_qty"]
            stub.spot, stub.perp = "61000.0", "61030.0"   # basis widened by $20
            e = r.one_cycle()
            exp = qty * (61000.0 - 60000.0) - qty * (61030.0 - 60010.0)
            self.assertAlmostEqual(e["pnl"]["basis_pnl"], exp)
            h = json.loads((Path(tmp) / "carry_hl_test" / "health.json").read_text())
            self.assertAlmostEqual(h["pnl"]["basis_pnl"], exp)
            self.assertAlmostEqual(
                h["simulated_equity"],
                1500.0 + h["pnl"]["net_pnl"])
            self.assertTrue(h["simulating"])

    def test_gate_off_closes_simulated_book_and_realises_pnl(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = _StubHLShim(spot="60000.0", perp="60000.0",
                               funding_history=[RATE_ON] * 240)
            r = _sim_runner(Path(tmp), stub)
            r.one_cycle()
            st = r.load_state()
            qty = st.simulated_position["spot_qty"]
            open_fees = st.simulated_position["fees_paid"]
            open_ms = st.funding_last_accrued_ms
            # funding collapses → gate OFF; one more settled row after open
            rows = [(open_ms - k * 3_600_000, 0.0) for k in range(1, 240)]
            rows.append((open_ms + 3_600_000, RATE_ON))
            stub.api.get_funding_rate_history.return_value = _rows(rows)
            _age_refresh(r)
            e = r.one_cycle()
            self.assertEqual(e["action"]["kind"], "would_unwind")
            self.assertEqual(e["action"]["reason"], "green_button_off")
            closed = e["action"]["closed"]
            funding = qty * 60000.0 * RATE_ON
            close_fees = qty * 60000.0 * (SPOT_T + PERP_T)
            self.assertAlmostEqual(closed["funding_accrued"], funding)
            self.assertAlmostEqual(closed["fees_paid"], open_fees + close_fees)
            self.assertAlmostEqual(closed["net_pnl"],
                                   funding - open_fees - close_fees)
            st = r.load_state()
            self.assertEqual(st.round_trips, 1)
            self.assertAlmostEqual(st.realized_pnl, closed["net_pnl"])
            self.assertEqual(st.simulated_position["spot_qty"], 0.0)
            self.assertAlmostEqual(st.simulated_equity,
                                   1500.0 + closed["net_pnl"])

    def test_manual_halt_unwinds_simulated_book(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = _StubHLShim(funding_history=[RATE_ON] * 240)
            r = _sim_runner(Path(tmp), stub)
            r.one_cycle()
            r.halt_sentinel.touch()
            e = r.one_cycle()
            self.assertEqual(e["action"]["kind"], "would_unwind")
            self.assertTrue(e["action"]["simulated"])
            self.assertEqual(r.load_state().simulated_position["spot_qty"], 0.0)

    def test_sim_disabled_keeps_old_log_only_behaviour(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = _StubHLShim(funding_history=[RATE_ON] * 240)
            r = _sim_runner(Path(tmp), stub, sim_enabled=False)
            r.one_cycle()
            self.assertEqual(r.load_state().simulated_position["spot_qty"], 0.0)


# =========================  live guard  =========================

class TestSimStateNeverTradesLive(unittest.TestCase):

    def setUp(self):
        _clean_env()

    def test_p3_refuses_a_state_with_a_dry_simulation(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stub = _StubHLLiveShim(mode=MODE_TESTNET,
                                   funding_history=[RATE_ON] * 240)
            r = _make_hl_runner(tmp, stub, have_creds=True, dry_run=False,
                                allow_live=True, hl_network="testnet",
                                hl_account_address=_MASTER,
                                hl_dedicated_account_confirmed=True,
                                initial_notional_usd=1000.0)
            st = r.load_state()
            st.sim_started_ts = "2026-10-01T00:00:00+00:00"
            st.simulated_position["spot_qty"] = 0.01
            st.simulated_position["perp_qty"] = -0.01
            r.save_state(st)
            with patch.object(cr, "notify", MagicMock()):
                with self.assertRaises(RuntimeError) as cm:
                    r.one_cycle()
            self.assertIn("DRY-RUN simulation", str(cm.exception))
            self.assertEqual(stub.spot_orders, [])
            self.assertEqual(stub.perp_orders, [])


# =========================  #4 gate min samples  =========================

class TestGateMinSamples(unittest.TestCase):

    def setUp(self):
        _clean_env()

    def test_shipped_hl_config_matches_backtest(self):
        cfg = cr.load_config(str(ROOT / "configs" / "carry-hl-btc.json"))
        self.assertEqual(cfg.gate_min_samples, 1080)
        self.assertEqual(cfg.gate_min_samples, int(90 * 24 * 0.5))
        self.assertTrue(cfg.dry_run)
        self.assertFalse(cfg.allow_live)
        self.assertEqual(cfg.sim_deposit_usd, 1500.0)

    def test_short_history_keeps_gate_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = _StubHLShim(funding_history=[RATE_ON] * 1079)
            r = _make_hl_runner(Path(tmp), stub, gate_min_samples=1080)
            e = r.one_cycle()
            self.assertFalse(e["gate"]["on"])
            self.assertIn("insufficient_history", e["gate"]["reason"])
            self.assertEqual(r.load_state().simulated_position["spot_qty"], 0.0)

    def test_full_history_turns_gate_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = _StubHLShim(funding_history=[RATE_ON] * 1080)
            r = _make_hl_runner(Path(tmp), stub, gate_min_samples=1080)
            with patch.object(cr, "notify", MagicMock()):
                e = r.one_cycle()
            self.assertTrue(e["gate"]["on"])


# =========================  #2 single instance  =========================

class TestInstanceLock(unittest.TestCase):

    def setUp(self):
        _clean_env()

    def test_second_runner_on_same_state_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = _make_hl_runner(Path(tmp), _StubHLShim())
            b = _make_hl_runner(Path(tmp), _StubHLShim())
            fh = a.acquire_instance_lock()
            with self.assertRaises(RuntimeError):
                b.acquire_instance_lock()
            fh.close()                       # released → next one may run
            b.acquire_instance_lock().close()

    def test_duplicate_unit_retired_and_alerts_env_wired(self):
        sysd = ROOT / "deployment" / "systemd"
        self.assertFalse((sysd / "carry-hl@.service").exists())
        unit = (sysd / "carry@.service").read_text()
        self.assertIn("EnvironmentFile=-/etc/trading-bot/carry-alerts.env", unit)
        self.assertEqual(go.UNIT, "carry@hl-btc")


# =========================  #1 watcher  =========================

class TestGreenWatch(unittest.TestCase):

    def setUp(self):
        _clean_env()

    def test_watcher_uses_gate_only_not_a_runner_cycle(self):
        sh = (ROOT / "deployment" / "carry_green_watch.sh").read_text()
        code = [l for l in sh.splitlines() if not l.lstrip().startswith("#")]
        body = "\n".join(code)
        self.assertIn("--gate-only", body)
        self.assertNotIn("--once", body)
        self.assertNotIn("carry-hl-btc.env", body)

    def test_gate_only_is_dry_stateless_and_credential_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stub = _StubHLShim(funding_history=[RATE_ON] * 1200)
            cfg = cr.CarryRunnerConfig(
                instance_name="gw", exchange="hyperliquid",
                spot_symbol="UBTC/USDC", perp_symbol="BTC",
                dry_run=False, allow_live=True,          # live config
                hl_account_address=_MASTER, hl_dedicated_account_confirmed=True,
                settlements_per_year=8760.0, trailing_window_samples=2160,
                gate_min_samples=1080, initial_notional_usd=1000.0)
            seen = {}
            real_cls = cr.CarryRunner

            def _fake_runner(c, state_dir=None):
                seen["mode"] = cr.resolve_mode(c)
                seen["env_key"] = os.environ.get("HL_CARRY_PRIVATE_KEY")
                seen["confirm"] = os.environ.get("HL_CONFIRM_LIVE")
                with patch("hl_carry_adapter.HLCarryAdapter", return_value=stub):
                    return real_cls(c, state_dir=tmp)

            with patch.dict(os.environ, {"HL_CARRY_PRIVATE_KEY": "0x" + "11" * 32,
                                         "HL_CONFIRM_LIVE": "YES"}):
                with patch.object(cr, "CarryRunner", side_effect=_fake_runner):
                    out = cr.gate_only(cfg)
                # creds restored for the caller afterwards
                self.assertEqual(os.environ.get("HL_CONFIRM_LIVE"), "YES")
            self.assertEqual(seen["mode"], cr.MODE_DRY)
            self.assertIsNone(seen["env_key"])
            self.assertIsNone(seen["confirm"])
            self.assertTrue(out["gate"]["on"])
            self.assertEqual(out["mode"], "GATE_ONLY")
            self.assertFalse((tmp / "gw" / "state.json").exists())
            stub.place_order.assert_not_called()
            stub.place_spot_order.assert_not_called()


# =========================  #5 alerts  =========================

class TestAlerts(unittest.TestCase):

    def setUp(self):
        _clean_env()
        p = patch.object(cr, "notify", MagicMock())
        self.notify = p.start()
        self.addCleanup(p.stop)

    def _texts(self):
        return [c.args[0] for c in self.notify.send.call_args_list]

    def test_sim_open_and_close_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = _StubHLShim(funding_history=[RATE_ON] * 240)
            r = _sim_runner(Path(tmp), stub)
            r.one_cycle()
            self.assertTrue(any("SIM OPEN" in t for t in self._texts()))
            r.halt_sentinel.touch()
            r.one_cycle()
            self.assertTrue(any("SIM CLOSE" in t for t in self._texts()))
            self.assertTrue(any("HALTED" in t for t in self._texts()))

    def test_basis_blowout_alerts_once_while_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = _StubHLShim(spot="60000.0", perp="61000.0",
                               funding_history=[0.0] * 240)   # gate off, flat
            r = _make_hl_runner(Path(tmp), stub)
            r.one_cycle()
            r.one_cycle()
            r.one_cycle()
            blow = [t for t in self._texts() if "BASIS BLOWOUT" in t]
            self.assertEqual(len(blow), 1)
            stub.perp = "60000.0"                 # resolves → re-arms
            r.one_cycle()
            self.assertNotIn("basis_blowout", r.load_state().alerts_active)
            stub.perp = "61000.0"
            r.one_cycle()
            blow = [t for t in self._texts() if "BASIS BLOWOUT" in t]
            self.assertEqual(len(blow), 2)

    def test_basis_kill_on_open_book_alerts_halt(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = _StubHLShim(funding_history=[RATE_ON] * 240)
            r = _sim_runner(Path(tmp), stub)
            r.one_cycle()
            stub.perp = str(float(stub.spot) * 1.02)
            e = r.one_cycle()
            self.assertEqual(e["action"]["reason"], "basis_blowout_kill")
            self.assertTrue(any("HALT basis-kill" in t for t in self._texts()))
            self.assertTrue(r.load_state().halted)

    def test_reconcile_failure_alerts(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = _StubHLShim(funding_history=[RATE_ON] * 240)
            r = _make_hl_runner(Path(tmp), stub)
            st = r.load_state()
            st.simulated_position["spot_qty"] = 0.01
            st.simulated_position["perp_qty"] = -0.005   # C3 drift
            r.save_state(st)
            r.one_cycle()
            self.assertTrue(any("RECONCILE FAIL" in t for t in self._texts()))

    def test_legging_abort_alerts_on_live_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = _StubHLLiveShim(mode=MODE_TESTNET,
                                   funding_history=[RATE_ON] * 240,
                                   perp_fails=True)
            r = _make_hl_runner(Path(tmp), stub, have_creds=True,
                                dry_run=False, allow_live=True,
                                hl_network="testnet",
                                hl_account_address=_MASTER,
                                hl_dedicated_account_confirmed=True,
                                initial_notional_usd=1000.0)
            with patch("time.sleep"):
                r.one_cycle()
            self.assertTrue(any("LEGGING ABORT" in t for t in self._texts()))

    def test_alert_failure_never_breaks_a_cycle(self):
        self.notify.send.side_effect = RuntimeError("telegram down")
        with tempfile.TemporaryDirectory() as tmp:
            stub = _StubHLShim(funding_history=[RATE_ON] * 240)
            r = _sim_runner(Path(tmp), stub)
            e = r.one_cycle()
            self.assertEqual(e["action"]["kind"], "would_open")


# =========================  #5 health report block  =========================

class TestCarryHealthCheck(unittest.TestCase):

    def _write(self, root, **over):
        d = Path(root) / "btc-hl"
        d.mkdir(parents=True, exist_ok=True)
        h = {"mode": "DRY_RUN", "cycles_total": 10, "halted": False,
             "last_cycle_ts": datetime.now(timezone.utc).isoformat(),
             "reconcile_ok": True, "legging_aborts_total": 0,
             "gate": {"on": True, "trailing_annualised": 0.059, "samples": 2160},
             "simulated_position": {"spot_qty": 0.013, "perp_qty": -0.013},
             "pnl": {"funding_accrued": 1.2, "fees_paid": 0.93,
                     "basis_pnl": -0.1, "net_pnl": 0.17, "realized_pnl": 0.0},
             "simulating": True, "simulated_equity": 1500.17,
             "sim_started_ts": "2026-10-08T10:00:00+00:00",
             "last_action": "noop"}
        h.update(over)
        (d / "health.json").write_text(json.dumps(h))

    def test_healthy_lane(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp)
            r = chc.carry_check(state_root=Path(tmp))
            self.assertTrue(r["ok"], r)
            lines = chc.carry_report_lines(state_root=Path(tmp))
            self.assertTrue(lines[0].startswith("✅"))
            self.assertTrue(any("P&L netto" in l for l in lines))
            self.assertTrue(any("proef sinds 2026-10-08" in l for l in lines))

    def test_stale_halted_reconcile(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
            self._write(tmp, last_cycle_ts=old, halted=True,
                        halt_reason="basis_blowout", reconcile_ok=False,
                        reconcile_errors_count=2)
            r = chc.carry_check(state_root=Path(tmp))
            self.assertFalse(r["ok"])
            joined = " ".join(r["issues"])
            self.assertIn("STALE", joined)
            self.assertIn("HALTED", joined)
            self.assertIn("reconcile", joined)

    def test_missing_health_is_an_issue_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            lines = chc.carry_report_lines(state_root=Path(tmp))
            self.assertTrue(lines[0].startswith("⚠️"))


# =========================  #3 carry_hl_go  =========================

class _GoEnv:
    """Temp config/env/state + patched systemctl/balances for carry_hl_go."""

    def __init__(self, tmp: Path, *, health_on_start=None):
        self.tmp = tmp
        self.cfg = tmp / "carry-hl-btc.json"
        shipped = json.loads((ROOT / "configs" / "carry-hl-btc.json").read_text())
        shipped.update(hl_account_address=_MASTER,
                       hl_dedicated_account_confirmed=True)
        self.cfg.write_text(json.dumps(shipped, indent=2))
        self.env = tmp / "carry-hl-btc.env"
        self.env.write_text(f"HL_CARRY_PRIVATE_KEY=0x{'11' * 32}\n"
                            f"HL_CARRY_ACCOUNT_ADDRESS={_MASTER}\n")
        self.state = tmp / "state"
        self.state.mkdir()
        (self.state / "state.json").write_text(json.dumps(
            {"sim_started_ts": "2026-10-01T00:00:00+00:00", "dry_run": True}))
        (self.state / "health.json").write_text(json.dumps(
            {"mode": "DRY_RUN", "last_cycle_ts": "2026-10-01T00:00:00+00:00"}))
        self.health = self.state / "health.json"
        self.calls = []
        self.health_on_start = health_on_start
        self.start_ok = True

    def systemctl(self, *args):
        self.calls.append(args)
        if args[0] == "start":
            if not self.start_ok:
                return False
            if self.health_on_start is not None:
                h = dict(self.health_on_start)
                h["last_cycle_ts"] = (datetime.now(timezone.utc)
                                      + timedelta(seconds=1)).isoformat()
                self.health.write_text(json.dumps(h))
        return True

    def argv(self, *cmd):
        return ["--config", str(self.cfg), "--env-file", str(self.env),
                "--state-dir", str(self.state), "--health", str(self.health),
                "--verify-timeout", "0.3", *cmd]

    def run(self, *cmd, balances=(825.0, 675.0)):
        with patch.object(go, "_systemctl", side_effect=self.systemctl), \
                patch.object(go, "_balances", return_value=balances), \
                patch.object(go, "VERIFY_POLL_SEC", 0.01):
            return go.main(self.argv(*cmd))


class TestCarryHlGo(unittest.TestCase):

    def setUp(self):
        _clean_env()

    def test_go_sizing_never_trips_runner_p3_guard(self):
        for bal in ((825, 675), (5000, 5000), (100000, 100000)):
            sz = go_sizing(bal[0], bal[1], 1000.0, 0.6)
            self.assertLessEqual(sz["initial_notional_usd"] * 0.6,
                                 sz["live_max_usd"])

    def test_go_success_archives_dry_state_and_flips_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = _GoEnv(Path(tmp), health_on_start={
                "mode": "P3_LIVE", "halted": False,
                "simulated_position": {"spot_qty": 0.0, "perp_qty": 0.0}})
            rc = g.run("go")
            self.assertEqual(rc, 0)
            cfg = json.loads(g.cfg.read_text())
            self.assertFalse(cfg["dry_run"])
            self.assertTrue(cfg["allow_live"])
            self.assertAlmostEqual(cfg["initial_notional_usd"], 1348.03)
            self.assertIn("HL_CONFIRM_LIVE=YES", g.env.read_text())
            self.assertEqual(oct(g.env.stat().st_mode & 0o777), "0o600")
            self.assertFalse((g.state / "state.json").exists())
            self.assertTrue(list(g.state.glob("state.json.dry-*")))
            self.assertEqual([c[0] for c in g.calls], ["stop", "start"])

    def test_go_validation_failure_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = _GoEnv(Path(tmp))
            before_cfg, before_env = g.cfg.read_text(), g.env.read_text()
            with patch.object(go, "_validate_live_config",
                              side_effect=RuntimeError("P3 sizing guard tripped")):
                rc = g.run("go")
            self.assertEqual(rc, 1)
            self.assertEqual(g.cfg.read_text(), before_cfg)
            self.assertEqual(g.env.read_text(), before_env)
            self.assertTrue((g.state / "state.json").exists())
            self.assertEqual(g.calls, [])

    def test_go_failed_start_rolls_back_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = _GoEnv(Path(tmp))
            g.start_ok = False
            before_cfg, before_env = g.cfg.read_text(), g.env.read_text()
            before_state = (g.state / "state.json").read_text()
            rc = g.run("go")
            self.assertEqual(rc, 1)
            self.assertEqual(g.cfg.read_text(), before_cfg)
            self.assertEqual(g.env.read_text(), before_env)
            self.assertEqual((g.state / "state.json").read_text(), before_state)
            self.assertEqual(g.calls[-1], ("restart", go.UNIT))
            self.assertTrue(list(g.tmp.glob("carry-hl-btc.json.bak-go-*")))

    def test_go_halted_first_cycle_flat_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = _GoEnv(Path(tmp), health_on_start={
                "mode": "P3_LIVE", "halted": True,
                "halt_reason": "startup_probe_failure",
                "simulated_position": {"spot_qty": 0.0, "perp_qty": 0.0}})
            rc = g.run("go")
            self.assertEqual(rc, 1)
            self.assertTrue(json.loads(g.cfg.read_text())["dry_run"])
            self.assertNotIn("HL_CONFIRM_LIVE", g.env.read_text())

    def test_go_no_fresh_health_times_out_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = _GoEnv(Path(tmp))          # start writes no health
            rc = g.run("go")
            self.assertEqual(rc, 1)
            self.assertTrue(json.loads(g.cfg.read_text())["dry_run"])

    def test_go_failed_verify_with_open_book_halts_instead_of_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = _GoEnv(Path(tmp), health_on_start={
                "mode": "P3_LIVE", "halted": True, "halt_reason": "x",
                "simulated_position": {"spot_qty": 0.013, "perp_qty": -0.013}})
            rc = g.run("go")
            self.assertEqual(rc, 2)
            self.assertTrue((g.state / "halt").exists())
            self.assertFalse(json.loads(g.cfg.read_text())["dry_run"])

    def test_park_refuses_open_live_book(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = _GoEnv(Path(tmp))
            g.health.write_text(json.dumps({
                "mode": "P3_LIVE",
                "simulated_position": {"spot_qty": 0.01, "perp_qty": -0.01}}))
            rc = g.run("park")
            self.assertEqual(rc, 1)
            self.assertEqual(g.calls, [])

    def test_park_from_flat_live_archives_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = _GoEnv(Path(tmp))
            g.health.write_text(json.dumps({
                "mode": "P3_LIVE",
                "simulated_position": {"spot_qty": 0.0, "perp_qty": 0.0}}))
            g.env.write_text(g.env.read_text() + "HL_CONFIRM_LIVE=YES\n")
            rc = g.run("park")
            self.assertEqual(rc, 0)
            self.assertTrue(json.loads(g.cfg.read_text())["dry_run"])
            self.assertNotIn("HL_CONFIRM_LIVE", g.env.read_text())
            self.assertFalse((g.state / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
