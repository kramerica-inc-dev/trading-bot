"""Dry-run tests for `scripts/carry_runner.py`.

Feeds the runner mocked OKX REST responses (no network, no auth). Verifies:
  * Computes the right target sizing when funding is "on".
  * Applies the green-button gate correctly (off → no would_open).
  * Writes a structured JSONL trade-log entry per cycle.
  * NEVER calls `place_order` / `place_spot_order` in dry-run mode.
  * Reconciliation handles an injected mismatch without crashing.
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
    reconcile_carry_state,
)


def _ticker(price: str) -> dict:
    return {"code": "0", "msg": "", "data": [{"last": price}]}


def _funding(rate: str) -> dict:
    return {"code": "0", "msg": "", "data": [{"fundingRate": rate,
                                               "fundingTime": "1"}]}


def _funding_history(rates: list) -> dict:
    # OKX returns newest-first; our fetch_funding_history reverses to oldest-first
    return {"code": "0", "msg": "",
            "data": [{"fundingRate": str(r), "fundingTime": str(i)}
                     for i, r in enumerate(rates)]}


class _StubAdapter:
    """Lightweight stand-in for OkxAdapter that returns prebaked envelopes.

    The runner's market-data path uses these methods:
        get_spot_ticker, get_ticker, api.get_funding_rate,
        api.get_funding_rate_history, assert_unified_margin, get_margin_snapshot
    """

    def __init__(self, spot="63500.0", perp="63520.0", funding_rate="0.0001",
                 funding_history=None, account=None):
        self.spot = spot
        self.perp = perp
        self.funding_rate = funding_rate
        self.funding_history = funding_history or []
        self.account = account
        self.api = MagicMock()
        # Make sure raw API order endpoints are NOT called in dry-run.
        self.api.place_order.side_effect = AssertionError(
            "DRY-RUN VIOLATION: place_order called"
        )
        self.api.place_spot_order.side_effect = AssertionError(
            "DRY-RUN VIOLATION: place_spot_order called"
        )
        self.api.get_funding_rate.return_value = _funding(self.funding_rate)
        self.api.get_funding_rate_history.return_value = _funding_history(
            self.funding_history
        )

    # market data
    def get_spot_ticker(self, inst_id):
        return _ticker(self.spot)

    def get_ticker(self, inst_id):
        return _ticker(self.perp)

    # account
    def assert_unified_margin(self):
        if self.account is None:
            return {"ok": False, "acct_lv": None, "message": "no creds"}
        return {"ok": self.account.get("acct_lv", 0) >= 3,
                "acct_lv": self.account.get("acct_lv"),
                "message": "stub"}

    def get_margin_snapshot(self, perp_inst_id="BTC-USDT"):
        if self.account is None:
            return {"total_eq_usd": None, "errors": []}
        return self.account

    # adapter's own place_spot_order — also forbidden
    def place_spot_order(self, *a, **k):
        raise AssertionError("DRY-RUN VIOLATION: adapter.place_spot_order called")

    def place_order(self, *a, **k):
        raise AssertionError("DRY-RUN VIOLATION: adapter.place_order called")


def _make_runner(tmp: Path, stub: _StubAdapter,
                 have_creds: bool = False,
                 **cfg_overrides) -> CarryRunner:
    """Build a CarryRunner with the stub adapter swapped in."""
    cfg = CarryRunnerConfig(
        instance_name="carry_test",
        dry_run=True,
        cycle_interval_sec=1,
        **cfg_overrides,
    )
    # Build runner with no creds, then replace adapter with stub.
    with patch.dict(os.environ, {}, clear=False):
        # ensure private creds env vars don't leak in from the host
        for k in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE"):
            os.environ.pop(k, None)
        if have_creds:
            os.environ["OKX_API_KEY"] = "k"
            os.environ["OKX_API_SECRET"] = "s"
            os.environ["OKX_API_PASSPHRASE"] = "p"
        runner = CarryRunner(cfg, state_dir=tmp)
    runner.adapter = stub  # type: ignore[assignment]
    runner.have_private_creds = have_creds
    return runner


class TestRunnerStartupGuard(unittest.TestCase):

    def test_rejects_live_mode_in_p1(self):
        cfg = CarryRunnerConfig(dry_run=False)
        with self.assertRaises(RuntimeError) as cm:
            CarryRunner(cfg)
        self.assertIn("dry_run", str(cm.exception))


class TestRunnerDryRunCycle(unittest.TestCase):

    def test_full_cycle_with_funding_on_logs_would_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            # +1bps/8h over 30 settlements → +10.95%/yr >> +5% threshold → ON
            stub = _StubAdapter(
                spot="60000.0", perp="60010.0",
                funding_rate="0.0001",
                funding_history=[0.0001] * 30,
            )
            runner = _make_runner(tmp, stub, have_creds=False,
                                  initial_notional_usd=5000.0,
                                  target_dn_notional_fraction=0.6,
                                  leverage_cap=2.0,
                                  funding_on_threshold_annualised=0.05)
            entry = runner.one_cycle()
            self.assertEqual(entry["mode"], "DRY_RUN")
            self.assertTrue(entry["gate"]["on"])
            self.assertIsNotNone(entry["target"])
            self.assertAlmostEqual(entry["target"]["notional_usd"], 3000.0)
            self.assertAlmostEqual(entry["target"]["spot_qty"], 3000.0 / 60000.0)
            self.assertAlmostEqual(entry["target"]["perp_qty"], -3000.0 / 60000.0)
            self.assertAlmostEqual(entry["target"]["perp_margin_usd"], 1500.0)
            self.assertEqual(entry["action"]["kind"], "would_open")
            self.assertAlmostEqual(entry["action"]["leg_notional_usd"], 3000.0)

    def test_funding_off_no_would_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stub = _StubAdapter(
                spot="60000.0", perp="60010.0",
                funding_rate="0.00001",  # ~1.1%/yr — below +5%
                funding_history=[0.00001] * 30,
            )
            runner = _make_runner(tmp, stub,
                                  funding_on_threshold_annualised=0.05)
            entry = runner.one_cycle()
            self.assertFalse(entry["gate"]["on"])
            self.assertEqual(entry["target_notional_usd"], 0.0)
            self.assertIsNone(entry["target"])
            # Flat book + gate off → no action proposed
            self.assertEqual(entry["action"]["kind"], "noop")

    def test_logs_jsonl_entry_per_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stub = _StubAdapter(
                spot="60000.0", perp="60010.0",
                funding_rate="0.0001",
                funding_history=[0.0001] * 30,
            )
            runner = _make_runner(tmp, stub)
            runner.one_cycle()
            runner.one_cycle()
            log_path = tmp / "carry_test" / "trades.log"
            self.assertTrue(log_path.exists())
            lines = log_path.read_text().strip().split("\n")
            self.assertEqual(len(lines), 2)
            # each line must be parseable JSON
            for line in lines:
                d = json.loads(line)
                self.assertIn("ts", d)
                self.assertIn("gate", d)
                self.assertIn("action", d)
                self.assertIn("reconcile", d)

    def test_state_persisted_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stub = _StubAdapter(funding_history=[0.0001] * 30)
            runner = _make_runner(tmp, stub)
            runner.one_cycle()
            state_path = tmp / "carry_test" / "state.json"
            self.assertTrue(state_path.exists())
            data = json.loads(state_path.read_text())
            self.assertEqual(data["cycles_total"], 1)
            self.assertTrue(data["dry_run"])
            self.assertGreater(len(data["funding_samples"]), 0)

    def test_no_order_endpoints_invoked(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stub = _StubAdapter(funding_history=[0.0001] * 30)
            runner = _make_runner(tmp, stub)
            # Both raise AssertionError on any call (side_effect set in stub).
            # Run a cycle and verify nothing tripped.
            runner.one_cycle()
            stub.api.place_order.assert_not_called()
            stub.api.place_spot_order.assert_not_called()

    def test_basis_blowout_triggers_alert(self):
        # spot=60000, perp=80000 → basis 33% — well above default 1% threshold
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stub = _StubAdapter(spot="60000", perp="80000",
                                funding_history=[0.0001] * 30)
            runner = _make_runner(tmp, stub, basis_kill_pct=0.01)
            entry = runner.one_cycle()
            kinds = {a["kind"] for a in entry["risk_alerts"]}
            self.assertIn("basis_blowout", kinds)


class TestRunnerReconciliation(unittest.TestCase):

    def test_reconcile_clean_state_passes(self):
        cfg = CarryRunnerConfig(dry_run=True)
        state = CarryRunnerState(dry_run=True)
        result = reconcile_carry_state(state, cfg)
        self.assertTrue(result.ok, msg=result.errors)
        self.assertGreaterEqual(result.rules_evaluated, 4)

    def test_reconcile_catches_sign_violation(self):
        # spot_qty < 0 OR perp_qty > 0 violates the carry sign convention
        cfg = CarryRunnerConfig(dry_run=True)
        state = CarryRunnerState(dry_run=True)
        state.simulated_position = {
            "spot_qty": -0.1, "perp_qty": +0.1,
            "entry_spot_price": 60000.0, "entry_perp_price": 60010.0,
            "funding_accrued": 0.0, "fees_paid": 0.0,
            "opened_ts": None, "last_updated_ts": None,
        }
        result = reconcile_carry_state(state, cfg)
        self.assertFalse(result.ok)
        # Both spot_qty<0 and perp_qty>0 should trigger C2; format is "Cn: msg"
        msgs = " | ".join(result.errors)
        self.assertIn("C2", msgs)

    def test_reconcile_catches_dry_run_mismatch(self):
        cfg = CarryRunnerConfig(dry_run=True)
        state = CarryRunnerState(dry_run=False)
        result = reconcile_carry_state(state, cfg)
        self.assertFalse(result.ok)
        self.assertTrue(any("C4" in e for e in result.errors))

    def test_reconcile_handles_injected_mismatch_without_crash(self):
        # Inject a mismatched simulated_position (drift > tolerance) and an
        # account snapshot that says we already have a live position — the
        # runner cycle must complete and the reconcile must report errors.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stub = _StubAdapter(funding_history=[0.0001] * 30,
                                account={"acct_lv": 3,
                                         "short_perp_qty": -0.1,
                                         "total_eq_usd": 5000.0,
                                         "margin_ratio": 5.0,
                                         "errors": []})
            runner = _make_runner(tmp, stub, have_creds=True)
            # Pre-seed state with broken position (skipped P1 invariant).
            state = runner.load_state()
            state.simulated_position = {
                "spot_qty": 0.1, "perp_qty": -0.09,  # 0.01 BTC drift
                "entry_spot_price": 60000.0, "entry_perp_price": 60010.0,
                "funding_accrued": 0.0, "fees_paid": 0.0,
                "opened_ts": "2026-05-13T00:00:00+00:00",
                "last_updated_ts": None,
            }
            runner.save_state(state)
            # Should not crash; should log errors in reconcile result.
            entry = runner.one_cycle()
            self.assertFalse(entry["reconcile"]["ok"])
            self.assertTrue(any("C3" in e for e in entry["reconcile"]["errors"]))

    def test_unified_margin_warning_when_acctLv_low(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stub = _StubAdapter(funding_history=[0.0001] * 30,
                                account={"acct_lv": 2,
                                         "short_perp_qty": None,
                                         "total_eq_usd": 5000.0,
                                         "margin_ratio": 5.0,
                                         "errors": []})
            runner = _make_runner(tmp, stub, have_creds=True)
            entry = runner.one_cycle()
            warnings = entry["reconcile"]["warnings"]
            self.assertTrue(any("C6" in w for w in warnings))


class TestRunnerHealth(unittest.TestCase):

    def test_health_dict_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stub = _StubAdapter(funding_history=[0.0001] * 30)
            runner = _make_runner(tmp, stub)
            runner.one_cycle()
            health_path = tmp / "carry_test" / "health.json"
            self.assertTrue(health_path.exists())
            h = json.loads(health_path.read_text())
            for k in ("alive", "instance", "dry_run", "last_cycle_ts",
                      "cycles_total", "last_funding_rate_8h",
                      "last_funding_annualised", "simulated_equity",
                      "reconcile_ok", "have_private_creds"):
                self.assertIn(k, h)
            self.assertTrue(h["alive"])
            self.assertTrue(h["dry_run"])
            self.assertEqual(h["cycles_total"], 1)


class TestRunnerMarketDataOnlyMode(unittest.TestCase):

    def test_no_private_creds_skips_account_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stub = _StubAdapter(funding_history=[0.0001] * 30, account=None)
            runner = _make_runner(tmp, stub, have_creds=False)
            entry = runner.one_cycle()
            # Without creds we don't call assert_unified_margin
            self.assertIsNone(entry["account"])


# =========================  Spot adapter mocked-HTTP tests  =========================

class TestOkxSpotAdapter(unittest.TestCase):
    """Spot endpoints on OkxAdapter — mocked HTTP layer."""

    def setUp(self):
        from okx_adapter import OkxAdapter
        self.adapter = OkxAdapter({})

    def test_place_spot_order_does_not_munge_symbol(self):
        with patch.object(self.adapter.api, "place_spot_order",
                          return_value={"code": "0"}) as mock:
            self.adapter.place_spot_order(
                "BTC-USDT", side="buy", order_type="limit",
                size="0.01", price="60000",
            )
            kwargs = mock.call_args.kwargs
            self.assertEqual(kwargs["inst_id"], "BTC-USDT")  # NOT -SWAP
            self.assertEqual(kwargs["order_type"], "limit")

    def test_spot_balance_filters_currency(self):
        nested = {
            "code": "0", "data": [{
                "details": [
                    {"ccy": "BTC", "availBal": "0.05", "frozenBal": "0"},
                    {"ccy": "USDT", "availBal": "5000", "frozenBal": "0"},
                ],
            }],
        }
        with patch.object(self.adapter.api, "get_balance", return_value=nested):
            r = self.adapter.get_spot_balance("BTC")
        self.assertEqual(len(r["data"]), 1)
        self.assertEqual(r["data"][0]["currency"], "BTC")

    def test_get_spot_min_size_parses_minSz(self):
        with patch.object(self.adapter.api, "get_spot_instruments",
                          return_value={"code": "0", "data": [
                              {"instId": "BTC-USDT", "minSz": "0.00001",
                               "tickSz": "0.1", "lotSz": "0.00000001"},
                          ]}):
            self.assertEqual(self.adapter.get_spot_min_size("BTC-USDT"),
                             0.00001)

    def test_get_spot_min_size_returns_none_on_failure(self):
        with patch.object(self.adapter.api, "get_spot_instruments",
                          return_value={"code": "error"}):
            self.assertIsNone(self.adapter.get_spot_min_size("BTC-USDT"))

    def test_assert_unified_margin_passes_on_acctLv_3(self):
        with patch.object(self.adapter.api, "get_account_config",
                          return_value={"code": "0",
                                        "data": [{"acctLv": "3"}]}):
            r = self.adapter.assert_unified_margin()
        self.assertTrue(r["ok"])
        self.assertEqual(r["acct_lv"], 3)

    def test_assert_unified_margin_fails_on_acctLv_1(self):
        with patch.object(self.adapter.api, "get_account_config",
                          return_value={"code": "0",
                                        "data": [{"acctLv": "1"}]}):
            r = self.adapter.assert_unified_margin()
        self.assertFalse(r["ok"])
        self.assertEqual(r["acct_lv"], 1)
        self.assertIn("acctLv>=3", r["message"])

    def test_assert_unified_margin_handles_missing_data(self):
        with patch.object(self.adapter.api, "get_account_config",
                          return_value={"code": "error"}):
            r = self.adapter.assert_unified_margin()
        self.assertFalse(r["ok"])
        self.assertIsNone(r["acct_lv"])

    def test_margin_snapshot_parses_balance_and_position(self):
        bal = {"code": "0", "data": [{
            "totalEq": "5000.5", "availEq": "2500.0",
            "mgnRatio": "10.5",
            "details": [
                {"ccy": "BTC", "eq": "0.05"},
                {"ccy": "USDT", "eq": "1900"},
            ],
        }]}
        pos = {"code": "0", "data": [{
            "instId": "BTC-USDT-SWAP",
            "pos": "0.05", "posSide": "short", "upl": "-3.50",
        }]}
        with patch.object(self.adapter.api, "get_balance", return_value=bal), \
             patch.object(self.adapter.api, "get_positions", return_value=pos):
            r = self.adapter.get_margin_snapshot(perp_inst_id="BTC-USDT")
        self.assertAlmostEqual(r["total_eq_usd"], 5000.5)
        self.assertAlmostEqual(r["avail_eq_usd"], 2500.0)
        self.assertAlmostEqual(r["margin_ratio"], 10.5)
        self.assertAlmostEqual(r["spot_btc_qty"], 0.05)
        self.assertAlmostEqual(r["short_perp_qty"], -0.05)
        self.assertAlmostEqual(r["unrealized_perp_usd"], -3.50)


if __name__ == "__main__":
    unittest.main()
