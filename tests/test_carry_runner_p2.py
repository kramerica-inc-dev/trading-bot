"""P2 tests for `scripts/carry_runner.py`.

Covers the new behaviors added in P2:
  * Three-state gate (DRY_RUN / P2_DEMO / P3_LIVE) resolved from config.
  * P3 live guard: requires allow_live=true AND per-leg notional <= live_max_usd.
  * Live-mode order placement is wired via the adapter (mocked).
  * Legging-window protection: leg-2 failure flattens leg 1 + counts an abort.
  * Basis-blowout: flatten + halt + sticky halted flag.
  * Manual halt sentinel: unwinds an open position, then noops while present.
  * Reconciliation in live mode: exchange-vs-simulated drift trips C5.
  * Live fee schedule pull falls back gracefully on no-creds adapters.

No network — all OKX calls are stubbed.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import carry_runner as cr  # noqa: E402
from carry_runner import (  # noqa: E402
    CarryRunner, CarryRunnerConfig, CarryRunnerState,
    MODE_DRY, MODE_P2, MODE_P3, resolve_mode,
    pull_live_fees, verify_leverage_cap, reconcile_carry_state,
)


# ----- shared helpers -----


def _ticker(price: str) -> dict:
    return {"code": "0", "msg": "", "data": [{"last": price}]}


def _funding(rate: str) -> dict:
    return {"code": "0", "msg": "",
            "data": [{"fundingRate": rate, "fundingTime": "1"}]}


def _funding_history(rates: list) -> dict:
    return {"code": "0", "msg": "",
            "data": [{"fundingRate": str(r), "fundingTime": str(i)}
                     for i, r in enumerate(rates)]}


def _ord_response(ord_id: str) -> dict:
    return {"code": "0", "data": [{"ordId": ord_id}]}


def _order_detail(state: str, fill_sz: str = "0") -> dict:
    return {"code": "0", "data": [{"state": state, "accFillSz": fill_sz}]}


class _StubLiveAdapter:
    """Mock OkxAdapter for P2/P3 cycles. Tracks order calls."""

    def __init__(
        self, *, spot="60000.0", perp="60010.0",
        funding_rate="0.0001", funding_history=None, account=None,
        spot_fills_state="filled", perp_fills_state="filled",
        fee_response=None, leverage_response=None,
    ):
        self.spot = spot
        self.perp = perp
        self.funding_rate = funding_rate
        self.funding_history = funding_history or [0.0001] * 30
        self.account = account
        self.api = MagicMock()
        self.api.get_funding_rate.return_value = _funding(self.funding_rate)
        self.api.get_funding_rate_history.return_value = _funding_history(
            self.funding_history
        )
        # Default: any /api/v5/account/trade-fee or leverage probe responds OK.
        self.api._request.return_value = fee_response or {
            "code": "0",
            "data": [{"maker": "0.0002", "taker": "0.0005", "lever": "10"}],
        }
        # Track order placements
        self.spot_orders = []
        self.perp_orders = []
        self._spot_fill_state = spot_fills_state
        self._perp_fill_state = perp_fills_state
        self._next_oid = 1000
        self.spot_open_failure = False    # if True, place_spot_order returns reject
        self.perp_open_failure = False    # if True, place_order returns reject

    def _next_id(self) -> str:
        self._next_oid += 1
        return str(self._next_oid)

    def get_spot_ticker(self, inst_id):
        return _ticker(self.spot)

    def get_ticker(self, inst_id):
        return _ticker(self.perp)

    def assert_unified_margin(self):
        if self.account is None:
            return {"ok": False, "acct_lv": None, "message": "no creds"}
        return {"ok": self.account.get("acct_lv", 0) >= 3,
                "acct_lv": self.account.get("acct_lv"), "message": "stub"}

    def get_margin_snapshot(self, perp_inst_id="BTC-USDT"):
        return self.account or {"errors": []}

    # ----- order primitives -----

    def place_spot_order(self, **kwargs):
        self.spot_orders.append(kwargs)
        if self.spot_open_failure:
            return {"code": "error", "msg": "spot reject"}
        return _ord_response(self._next_id())

    def place_order(self, **kwargs):
        self.perp_orders.append(kwargs)
        if self.perp_open_failure:
            return {"code": "error", "msg": "perp reject"}
        return _ord_response(self._next_id())

    def get_spot_order_detail(self, inst_id, order_id=None, **kw):
        return _order_detail(self._spot_fill_state)

    def get_order_detail(self, inst_id, order_id=None, **kw):
        return _order_detail(self._perp_fill_state)


def _make_runner(tmp: Path, stub, *,
                 mode_overrides=None,
                 have_creds=True,
                 **cfg_overrides) -> CarryRunner:
    cfg_kwargs = dict(instance_name="carry_test", cycle_interval_sec=1)
    cfg_kwargs.update(cfg_overrides)
    cfg = CarryRunnerConfig(**cfg_kwargs)
    with patch.dict(os.environ, {}, clear=False):
        for k in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE"):
            os.environ.pop(k, None)
        if have_creds:
            os.environ["OKX_API_KEY"] = "k"
            os.environ["OKX_API_SECRET"] = "s"
            os.environ["OKX_API_PASSPHRASE"] = "p"
        runner = CarryRunner(cfg, state_dir=tmp)
    runner.adapter = stub  # type: ignore[assignment]
    runner.have_private_creds = have_creds
    # Skip live probes — tests inject their own.
    runner._startup_probes_done = True
    runner.fees = {"spot_maker": 0.0002, "spot_taker": 0.0005,
                   "perp_maker": 0.0002, "perp_taker": 0.0005,
                   "sources": {k: "fallback" for k in
                               ("spot_maker", "spot_taker", "perp_maker", "perp_taker")}}
    runner.leverage_check = {"configured_cap": cfg.leverage_cap,
                              "ok": True, "message": "stub",
                              "account_max": 10.0, "contract_max": 100.0,
                              "effective_max": 10.0, "errors": []}
    return runner


# =========================  Mode resolution  =========================


class TestModeResolution(unittest.TestCase):

    def test_dry_run_default(self):
        cfg = CarryRunnerConfig()
        self.assertEqual(resolve_mode(cfg), MODE_DRY)

    def test_p2_demo_when_okx_demo_and_not_dry(self):
        cfg = CarryRunnerConfig(dry_run=False, okx_demo=True)
        self.assertEqual(resolve_mode(cfg), MODE_P2)

    def test_dry_run_wins_over_okx_demo(self):
        # dry_run=true takes precedence regardless of okx_demo
        cfg = CarryRunnerConfig(dry_run=True, okx_demo=True)
        self.assertEqual(resolve_mode(cfg), MODE_DRY)

    def test_live_attempt_without_allow_live_rejected(self):
        cfg = CarryRunnerConfig(dry_run=False, okx_demo=False, allow_live=False)
        with self.assertRaises(RuntimeError) as cm:
            resolve_mode(cfg)
        self.assertIn("allow_live=true", str(cm.exception))

    def test_p3_live_allowed_when_sized_under_live_max(self):
        cfg = CarryRunnerConfig(
            dry_run=False, okx_demo=False, allow_live=True,
            initial_notional_usd=1000.0, target_dn_notional_fraction=0.5,
            live_max_usd=1000.0,
        )
        # per-leg = 500 < 1000 → allowed
        self.assertEqual(resolve_mode(cfg), MODE_P3)

    def test_p3_live_rejected_when_oversized(self):
        cfg = CarryRunnerConfig(
            dry_run=False, okx_demo=False, allow_live=True,
            initial_notional_usd=5000.0, target_dn_notional_fraction=0.6,
            live_max_usd=1000.0,
        )
        # per-leg = 3000 > 1000 → rejected
        with self.assertRaises(RuntimeError) as cm:
            resolve_mode(cfg)
        self.assertIn("P3 sizing guard", str(cm.exception))


# =========================  P2 order placement  =========================


class TestP2Open(unittest.TestCase):

    def test_p2_open_places_both_legs_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = _StubLiveAdapter()
            runner = _make_runner(
                Path(tmp), stub,
                dry_run=False, okx_demo=True,
                initial_notional_usd=1000.0,
                target_dn_notional_fraction=0.6,
            )
            entry = runner.one_cycle()
            self.assertEqual(entry["mode"], MODE_P2)
            self.assertEqual(entry["action"]["kind"], "do_open")
            self.assertTrue(entry["order_result"]["ok"])
            # Spot first (buy), then perp (sell short)
            self.assertEqual(len(stub.spot_orders), 1)
            self.assertEqual(stub.spot_orders[0]["side"], "buy")
            self.assertEqual(len(stub.perp_orders), 1)
            self.assertEqual(stub.perp_orders[0]["side"], "sell")
            # State should reflect open
            state_path = Path(tmp) / "carry_test" / "state.json"
            data = json.loads(state_path.read_text())
            self.assertGreater(data["simulated_position"]["spot_qty"], 0)
            self.assertLess(data["simulated_position"]["perp_qty"], 0)

    def test_p2_open_legging_abort_flattens_spot(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Perp leg "doesn't fill" — runner must flatten spot
            stub = _StubLiveAdapter(perp_fills_state="live")
            runner = _make_runner(
                Path(tmp), stub,
                dry_run=False, okx_demo=True,
                initial_notional_usd=1000.0,
                target_dn_notional_fraction=0.6,
                legging_window_sec=1,
            )
            entry = runner.one_cycle()
            self.assertFalse(entry["order_result"]["ok"])
            self.assertEqual(entry["order_result"]["reason"],
                             "legging_abort_perp_failed")
            # Must have placed a spot buy and a spot sell (flatten).
            sides = [o["side"] for o in stub.spot_orders]
            self.assertEqual(sides, ["buy", "sell"])
            # State must remain flat (spot_qty == 0) — leg-1 was flattened.
            state_path = Path(tmp) / "carry_test" / "state.json"
            data = json.loads(state_path.read_text())
            self.assertAlmostEqual(data["simulated_position"]["spot_qty"], 0.0)
            self.assertEqual(data["legging_aborts_total"], 1)


class TestP2BasisKill(unittest.TestCase):

    def test_basis_blowout_with_open_position_flattens_and_halts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stub = _StubLiveAdapter()
            runner = _make_runner(
                tmp, stub,
                dry_run=False, okx_demo=True,
                initial_notional_usd=1000.0, target_dn_notional_fraction=0.6,
                basis_kill_pct=0.001,   # very tight
            )
            # Pre-load an open position so the kill flattens it.
            state = runner.load_state()
            state.simulated_position = {
                "spot_qty": 0.01, "perp_qty": -0.01,
                "entry_spot_price": 60000.0, "entry_perp_price": 60010.0,
                "funding_accrued": 0.0, "fees_paid": 0.0,
                "opened_ts": "2026-05-13T00:00:00+00:00",
                "last_updated_ts": None,
            }
            runner.save_state(state)
            # Make basis huge → must trip the kill.
            stub.perp = "70000.0"
            entry = runner.one_cycle()
            self.assertIn("basis_blowout",
                          [a["kind"] for a in entry["risk_alerts"]])
            self.assertEqual(entry["action"]["kind"], "do_unwind")
            self.assertEqual(entry["action"]["reason"], "basis_blowout_kill")
            self.assertTrue(entry["halted"])
            self.assertEqual(entry["halt_reason"], "basis_blowout")
            # State persisted with halt flag
            data = json.loads((tmp / "carry_test" / "state.json").read_text())
            self.assertTrue(data["halted"])

    def test_subsequent_cycle_after_halt_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stub = _StubLiveAdapter()
            runner = _make_runner(
                tmp, stub,
                dry_run=False, okx_demo=True,
                initial_notional_usd=1000.0, target_dn_notional_fraction=0.6,
            )
            state = runner.load_state()
            state.halted = True
            state.halt_reason = "basis_blowout"
            runner.save_state(state)
            entry = runner.one_cycle()
            self.assertEqual(entry["action"]["kind"], "noop")
            self.assertIn("halted", entry["action"]["reason"])


class TestP2ManualHalt(unittest.TestCase):

    def test_halt_sentinel_unwinds_open_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stub = _StubLiveAdapter()
            runner = _make_runner(
                tmp, stub,
                dry_run=False, okx_demo=True,
                initial_notional_usd=1000.0, target_dn_notional_fraction=0.6,
            )
            # Open position + halt sentinel
            state = runner.load_state()
            state.simulated_position = {
                "spot_qty": 0.01, "perp_qty": -0.01,
                "entry_spot_price": 60000.0, "entry_perp_price": 60010.0,
                "funding_accrued": 0.0, "fees_paid": 0.0,
                "opened_ts": "2026-05-13T00:00:00+00:00",
                "last_updated_ts": None,
            }
            runner.save_state(state)
            runner.halt_sentinel.write_text("manual halt")
            entry = runner.one_cycle()
            self.assertTrue(entry["manual_halt"])
            self.assertEqual(entry["action"]["kind"], "do_unwind")
            self.assertEqual(entry["action"]["reason"],
                             "manual_halt_with_open_position")

    def test_halt_sentinel_with_flat_book_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stub = _StubLiveAdapter()
            runner = _make_runner(
                tmp, stub,
                dry_run=False, okx_demo=True,
                initial_notional_usd=1000.0, target_dn_notional_fraction=0.6,
            )
            runner.halt_sentinel.write_text("manual halt")
            entry = runner.one_cycle()
            self.assertEqual(entry["action"]["kind"], "noop")
            self.assertEqual(entry["action"]["reason"], "manual_halt_flat")


# =========================  Reconcile in live mode  =========================


class TestReconcileLiveMode(unittest.TestCase):

    def test_exchange_perp_drift_in_p2_trips_C5(self):
        cfg = CarryRunnerConfig(dry_run=False, okx_demo=True)
        state = CarryRunnerState(dry_run=False)
        state.simulated_position = {
            "spot_qty": 0.01, "perp_qty": -0.01,
            "entry_spot_price": 60000.0, "entry_perp_price": 60010.0,
            "funding_accrued": 0.0, "fees_paid": 0.0,
            "opened_ts": None, "last_updated_ts": None,
        }
        snap = {"acct_lv": 3, "short_perp_qty": -0.02,  # exchange says 0.02 short
                "spot_btc_qty": 0.01, "margin_ratio": 5.0, "errors": []}
        res = reconcile_carry_state(state, cfg, account_snapshot=snap,
                                    mode=MODE_P2)
        self.assertFalse(res.ok)
        self.assertTrue(any("C5" in e for e in res.errors))

    def test_exchange_matches_simulated_in_p2_passes(self):
        cfg = CarryRunnerConfig(dry_run=False, okx_demo=True)
        state = CarryRunnerState(dry_run=False)
        state.simulated_position = {
            "spot_qty": 0.01, "perp_qty": -0.01,
            "entry_spot_price": 60000.0, "entry_perp_price": 60010.0,
            "funding_accrued": 0.0, "fees_paid": 0.0,
            "opened_ts": None, "last_updated_ts": None,
        }
        snap = {"acct_lv": 3, "short_perp_qty": -0.01,
                "spot_btc_qty": 0.01, "margin_ratio": 5.0, "errors": []}
        res = reconcile_carry_state(state, cfg, account_snapshot=snap,
                                    mode=MODE_P2)
        self.assertTrue(res.ok, msg=res.errors)


# =========================  Fee schedule pull  =========================


class TestFeePull(unittest.TestCase):

    def test_pull_live_fees_uses_api_when_available(self):
        api = MagicMock()
        api._request.return_value = {
            "code": "0", "data": [{"maker": "0.0001", "taker": "0.0003"}],
        }
        adapter = type("A", (), {"api": api})()
        fees = pull_live_fees(adapter, spot_inst="BTC-USDT", perp_inst="BTC-USDT")
        self.assertAlmostEqual(fees["spot_maker"], 0.0001)
        self.assertAlmostEqual(fees["perp_taker"], 0.0003)
        self.assertEqual(fees["sources"]["spot_maker"], "live")

    def test_pull_live_fees_falls_back_on_no_api(self):
        adapter = type("A", (), {})()  # no .api → fallback
        fees = pull_live_fees(adapter, spot_inst="BTC-USDT", perp_inst="BTC-USDT")
        self.assertEqual(fees["sources"]["spot_maker"], "fallback")
        self.assertAlmostEqual(fees["spot_maker"], cr.FALLBACK_FEES["spot_maker"])

    def test_pull_live_fees_falls_back_on_api_error(self):
        api = MagicMock()
        api._request.return_value = {"code": "error", "msg": "auth"}
        adapter = type("A", (), {"api": api})()
        fees = pull_live_fees(adapter, spot_inst="BTC-USDT", perp_inst="BTC-USDT")
        self.assertEqual(fees["sources"]["spot_maker"], "fallback")


# =========================  Leverage verification  =========================


class TestLeverageVerification(unittest.TestCase):

    def test_leverage_ok_when_effective_above_configured(self):
        api = MagicMock()
        # First call (account/leverage-info): account_max = 10
        # Second call (public/instruments): contract_max = 100
        api._request.side_effect = [
            {"code": "0", "data": [{"lever": "10"}]},
            {"code": "0", "data": [{"lever": "100"}]},
        ]
        adapter = type("A", (), {"api": api})()
        out = verify_leverage_cap(adapter, configured_cap=2.0,
                                   perp_inst="BTC-USDT")
        self.assertTrue(out["ok"])
        self.assertEqual(out["effective_max"], 10.0)

    def test_leverage_mismatch_when_effective_below_configured(self):
        api = MagicMock()
        api._request.side_effect = [
            {"code": "0", "data": [{"lever": "1"}]},
            {"code": "0", "data": [{"lever": "1"}]},
        ]
        adapter = type("A", (), {"api": api})()
        out = verify_leverage_cap(adapter, configured_cap=2.0,
                                   perp_inst="BTC-USDT")
        self.assertFalse(out["ok"])
        self.assertIn("MISMATCH", out["message"])

    def test_leverage_no_data_still_passes(self):
        # If neither API call returns data, we keep the configured cap.
        api = MagicMock()
        api._request.return_value = {"code": "error", "msg": "x"}
        adapter = type("A", (), {"api": api})()
        out = verify_leverage_cap(adapter, configured_cap=2.0,
                                   perp_inst="BTC-USDT")
        self.assertTrue(out["ok"])


# =========================  P3 sizing guard via constructor  =========================


class TestP3ConstructorGuard(unittest.TestCase):

    def test_constructor_refuses_oversized_live_config(self):
        cfg = CarryRunnerConfig(
            dry_run=False, okx_demo=False, allow_live=True,
            initial_notional_usd=10_000.0, target_dn_notional_fraction=0.6,
            live_max_usd=1000.0,
        )
        with self.assertRaises(RuntimeError):
            CarryRunner(cfg)

    def test_constructor_refuses_live_without_allow_live(self):
        cfg = CarryRunnerConfig(dry_run=False, okx_demo=False, allow_live=False)
        with self.assertRaises(RuntimeError):
            CarryRunner(cfg)


if __name__ == "__main__":
    unittest.main()
