"""Tests for the carry-tab API helpers in scripts/dashboard_api.py.

We exercise the per-instance helpers (_list_carry_instances,
_read_carry_state_summary, _read_carry_trades, _validate_carry_instance)
directly against a temp-directory fixture, then hit the HTTP handler via
an in-process ThreadingHTTPServer to verify route shape + 404 behaviour.

This avoids touching the deployed dashboard or any LXC state.
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import urlopen
from urllib.error import HTTPError

# Imported lazily after we patch the paths so the module picks up the test dirs.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


def _fresh_dashboard_api(bot_dir: Path):
    """(Re-)import dashboard_api with BOT_DIR pointing at a fixture root."""
    import importlib
    import os
    os.environ["BOT_DIR"] = str(bot_dir)
    if "dashboard_api" in sys.modules:
        del sys.modules["dashboard_api"]
    import dashboard_api  # type: ignore  # noqa: E402
    return importlib.reload(dashboard_api)


def _write_fixture(root: Path, *, instance: str = "btc",
                   include_account_auth_error: bool = False,
                   funding_samples: list | None = None) -> None:
    """Populate state/carry/<instance>/{health,state,trades.log} under root."""
    inst_dir = root / "state" / "carry" / instance
    inst_dir.mkdir(parents=True, exist_ok=True)

    health = {
        "alive": True,
        "instance": instance,
        "mode": "P2_DEMO",
        "dry_run": False,
        "okx_demo": True,
        "allow_live": False,
        "halted": False,
        "halt_reason": None,
        "last_cycle_ts": "2026-05-13T07:24:40.412288+00:00",
        "cycles_total": 7,
        "last_funding_rate_8h": 0.0001,
        "last_funding_annualised": 0.1095,
        "last_basis_usd": 47.0,
        "last_spot_price": 80911.2,
        "last_perp_price": 80864.2,
        "simulated_equity": 5000.0,
        "simulated_position": {
            "spot_qty": 0.0, "perp_qty": 0.0,
            "entry_spot_price": 0.0, "entry_perp_price": 0.0,
            "funding_accrued": 0.0, "fees_paid": 0.0,
            "opened_ts": None, "last_updated_ts": None,
        },
        "legging_aborts_total": 0,
        "reconcile_ok": True,
        "reconcile_errors_count": 0,
        "have_private_creds": True,
    }
    (inst_dir / "health.json").write_text(json.dumps(health, indent=2))

    samples = funding_samples if funding_samples is not None else [0.0001] * 30
    state = {
        "started_ts": "2026-05-13T07:18:30+00:00",
        "last_cycle_ts": "2026-05-13T07:24:40+00:00",
        "cycles_total": 7,
        "funding_samples": samples,
        "funding_samples_ts": ["2026-05-13T07:24:40+00:00"] * len(samples),
        "simulated_position": health["simulated_position"],
        "simulated_equity": 5000.0,
        "last_funding_rate": 0.0001,
        "last_basis_usd": 47.0,
        "last_spot_price": 80911.2,
        "last_perp_price": 80864.2,
        "last_account_check": {"raw_balance": {"code": "0"}},  # sensitive; must not leak
        "last_reconcile_ok": True,
        "last_reconcile_errors": [],
        "halted": False,
        "halt_reason": None,
        "last_basis_kill_ts": None,
        "legging_aborts_total": 0,
        "last_mode": "P2_DEMO",
        "dry_run": False,
    }
    (inst_dir / "state.json").write_text(json.dumps(state, indent=2))

    account = {"acct_lv": 3, "margin_ratio": 5.0}
    if include_account_auth_error:
        account["raw_balance"] = {
            "code": "error", "msg": "HTTP 401: API key doesn't exist"
        }
    # Three log lines so newest-first ordering can be verified.
    log_lines = []
    for i in range(3):
        log_lines.append({
            "ts": f"2026-05-13T07:2{i}:00+00:00",
            "instance": instance,
            "mode": "P2_DEMO",
            "spot_price": 80000.0 + i,
            "perp_price": 80010.0 + i,
            "basis_usd": -10.0,
            "basis_frac": 0.0001,
            "funding_rate_8h": 0.0001,
            "funding_rate_annualised": 0.1095,
            "gate": {"on": False, "trailing_annualised": 0.044,
                     "threshold": 0.05, "samples": 30, "reason": "below"},
            "action": {"kind": "noop", "reason": "gate_off"},
            "order_result": None,
            "account": account,
            "risk_alerts": [],
            "reconcile": {"ok": True, "errors": [], "rules_evaluated": 4},
        })
    (inst_dir / "trades.log").write_text(
        "\n".join(json.dumps(e) for e in log_lines) + "\n"
    )


class TestCarryHelpers(unittest.TestCase):

    def test_validate_carry_instance_accepts_safe(self):
        with TemporaryDirectory() as tmp:
            mod = _fresh_dashboard_api(Path(tmp))
            self.assertEqual(mod._validate_carry_instance("btc"), "btc")
            self.assertEqual(mod._validate_carry_instance("eth_2"), "eth_2")
            self.assertEqual(mod._validate_carry_instance("carry-btc"), "carry-btc")

    def test_validate_carry_instance_rejects_traversal(self):
        with TemporaryDirectory() as tmp:
            mod = _fresh_dashboard_api(Path(tmp))
            for bad in ("../etc", "../../root", "btc/../etc", "",
                        ".secret", "a/b", "A", "A" * 33):
                with self.assertRaises(ValueError, msg=f"should reject {bad!r}"):
                    mod._validate_carry_instance(bad)

    def test_list_instances_empty_when_dir_missing(self):
        with TemporaryDirectory() as tmp:
            mod = _fresh_dashboard_api(Path(tmp))
            self.assertEqual(mod._list_carry_instances(), [])

    def test_list_instances_returns_summary_with_health(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fixture(root, instance="btc")
            mod = _fresh_dashboard_api(root)
            out = mod._list_carry_instances()
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0]["instance"], "btc")
            self.assertEqual(out[0]["mode"], "P2_DEMO")
            self.assertFalse(out[0]["halted"])
            self.assertEqual(out[0]["cycles_total"], 7)

    def test_list_instances_skips_dirs_without_health(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fixture(root, instance="btc")
            # Empty placeholder dir — must be filtered out.
            (root / "state" / "carry" / "empty").mkdir(parents=True)
            mod = _fresh_dashboard_api(root)
            out = mod._list_carry_instances()
            self.assertEqual([x["instance"] for x in out], ["btc"])

    def test_state_summary_downsamples_funding_samples(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fixture(root, instance="btc",
                           funding_samples=[0.0001] * 270)
            mod = _fresh_dashboard_api(root)
            s = mod._read_carry_state_summary("btc")
            self.assertLessEqual(len(s["funding_samples"]), mod.CARRY_FUNDING_SAMPLES_MAX)
            self.assertEqual(s["funding_samples_total"], 270)
            # The summary must NOT include `last_account_check` (could leak).
            self.assertNotIn("last_account_check", s)

    def test_state_summary_returns_error_when_missing(self):
        with TemporaryDirectory() as tmp:
            mod = _fresh_dashboard_api(Path(tmp))
            out = mod._read_carry_state_summary("btc")
            self.assertIn("_error", out)

    def test_trades_newest_first(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fixture(root, instance="btc")
            mod = _fresh_dashboard_api(root)
            rows = mod._read_carry_trades("btc", limit=10)
            self.assertEqual(len(rows), 3)
            self.assertGreater(rows[0]["ts"], rows[-1]["ts"])


class TestCarryHTTPRoutes(unittest.TestCase):
    """Spin up the ThreadingHTTPServer in-process and hit the routes."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        _write_fixture(cls.root, instance="btc", include_account_auth_error=True)
        cls.mod = _fresh_dashboard_api(cls.root)
        # Bind on port 0 → kernel picks free port.
        from http.server import ThreadingHTTPServer
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), cls.mod.DashboardHandler)
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._tmp.cleanup()

    def _get(self, path: str):
        url = f"http://127.0.0.1:{self.port}{path}"
        with urlopen(url, timeout=5) as r:
            return r.getcode(), json.loads(r.read())

    def test_instances_list_returns_btc(self):
        code, body = self._get("/api/carry/instances")
        self.assertEqual(code, 200)
        names = [i["instance"] for i in body["instances"]]
        self.assertIn("btc", names)

    def test_health_endpoint_returns_payload(self):
        code, body = self._get("/api/carry/btc/health")
        self.assertEqual(code, 200)
        self.assertEqual(body["instance"], "btc")
        self.assertEqual(body["mode"], "P2_DEMO")
        self.assertIn("simulated_position", body)

    def test_state_endpoint_returns_summary(self):
        code, body = self._get("/api/carry/btc/state")
        self.assertEqual(code, 200)
        self.assertEqual(body["instance"], "btc")
        self.assertIn("funding_samples", body)
        # Sensitive field stays out of the response.
        self.assertNotIn("last_account_check", body)

    def test_trades_endpoint_newest_first(self):
        code, body = self._get("/api/carry/btc/trades?limit=10")
        self.assertEqual(code, 200)
        self.assertEqual(body["instance"], "btc")
        rows = body["trades"]
        self.assertTrue(len(rows) >= 2)
        self.assertGreater(rows[0]["ts"], rows[-1]["ts"])
        # Sanity: auth-error payload made it through verbatim so the UI
        # can drive its yellow banner.
        rb = rows[0].get("account", {}).get("raw_balance", {})
        self.assertIn("API key", rb.get("msg", ""))

    def test_health_404_for_missing_instance(self):
        try:
            with urlopen(f"http://127.0.0.1:{self.port}/api/carry/ghost/health",
                         timeout=5) as r:
                code = r.getcode()
                body = json.loads(r.read())
        except HTTPError as e:
            code, body = e.code, json.loads(e.read())
        self.assertEqual(code, 404)
        self.assertIn("error", body)

    def test_state_404_for_missing_instance(self):
        try:
            with urlopen(f"http://127.0.0.1:{self.port}/api/carry/ghost/state",
                         timeout=5) as r:
                code = r.getcode()
                body = json.loads(r.read())
        except HTTPError as e:
            code, body = e.code, json.loads(e.read())
        self.assertEqual(code, 404)

    def test_bad_instance_name_returns_400(self):
        try:
            with urlopen(
                f"http://127.0.0.1:{self.port}/api/carry/..%2Fetc/health",
                timeout=5,
            ) as r:
                code = r.getcode()
                body = json.loads(r.read())
        except HTTPError as e:
            code, body = e.code, json.loads(e.read())
        self.assertEqual(code, 400)

    def test_unknown_resource_returns_404(self):
        try:
            with urlopen(f"http://127.0.0.1:{self.port}/api/carry/btc/bogus",
                         timeout=5) as r:
                code = r.getcode()
                body = json.loads(r.read())
        except HTTPError as e:
            code, body = e.code, json.loads(e.read())
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
