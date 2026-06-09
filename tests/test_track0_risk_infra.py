"""Track 0 risk-infra tests (2026-06-09): leverage read-back verification +
gross clamp, soft de-lever band, configurable neutrality tolerance, margin
snapshot, the equity-sampler live_trading fix, and the Telegram notifier.

All network-free; venue surfaces are stubbed exactly like the existing
test_hl_xs_runner.py / test_hl_adapter.py patterns."""

import json
import os
import sys
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

try:
    import hl_xs_runner as R
    from hl_adapter import HLAdapter
    HAVE_SDK = True
except ImportError:
    HAVE_SDK = False

import equity_sampler  # noqa: E402
import notify as notify_mod  # noqa: E402


def _closes(finals, lookback=120):
    """{sym: closes} where each series' trailing-`lookback` return == final."""
    out = {}
    for sym, final in finals.items():
        cl = np.ones(lookback + 2)
        cl[-1] = 1.0 + final
        out[sym] = cl
    return out


def _targets_stub(**over):
    cfg = R.HLXSConfig(lookback_days=120, m=2, **over)
    stub = types.SimpleNamespace(cfg=cfg, _logs=[])
    stub.log = lambda e: stub._logs.append(e)
    stub._apply_exposure_caps = types.MethodType(R.HLXSRunner._apply_exposure_caps, stub)
    return stub


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestSoftDelever(unittest.TestCase):
    FINALS = {"A": 0.5, "B": 0.4, "C": 0.0, "D": -0.4, "E": -0.5}

    def test_no_delever_below_band(self):
        stub = _targets_stub(soft_delever_dd_pct=0.06, soft_delever_factor=0.5)
        t = R.HLXSRunner._targets(stub, _closes(self.FINALS), 5000.0, dd=0.03)
        self.assertAlmostEqual(sum(abs(v) for v in t.values()), 10000.0, places=3)
        self.assertFalse(stub._delever_active)

    def test_delever_scales_targets_in_band(self):
        stub = _targets_stub(soft_delever_dd_pct=0.06, soft_delever_factor=0.5)
        t = R.HLXSRunner._targets(stub, _closes(self.FINALS), 5000.0, dd=0.08)
        self.assertAlmostEqual(sum(abs(v) for v in t.values()), 5000.0, places=3)
        self.assertAlmostEqual(sum(t.values()), 0.0, places=6)   # neutrality preserved
        self.assertTrue(stub._delever_active)
        self.assertTrue(any(e.get("action") == "soft_delever" for e in stub._logs))

    def test_disabled_by_default(self):
        stub = _targets_stub()                                    # sdd None
        t = R.HLXSRunner._targets(stub, _closes(self.FINALS), 5000.0, dd=0.50)
        self.assertAlmostEqual(sum(abs(v) for v in t.values()), 10000.0, places=3)
        self.assertFalse(stub._delever_active)


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestGrossClamp(unittest.TestCase):
    """gross_exposure > 1.0 only sizes when the leverage pin is read-back
    verified; otherwise it clamps to 1.0 (and never blocks the rebalance)."""
    FINALS = {"A": 0.5, "B": 0.4, "C": 0.0, "D": -0.4, "E": -0.5}

    def _gross(self, *, live, verified, ge=1.5):
        stub = _targets_stub(gross_exposure=ge)
        stub.live_trading = live
        stub._leverage_verified = verified
        t = R.HLXSRunner._targets(stub, _closes(self.FINALS), 5000.0)
        return sum(abs(v) for v in t.values()), stub

    def test_live_unverified_clamps_to_1x(self):
        gross, stub = self._gross(live=True, verified=False)
        self.assertAlmostEqual(gross, 10000.0, places=3)          # ge used = 1.0
        self.assertTrue(any(e.get("action") == "gross_clamped" for e in stub._logs))

    def test_live_verified_uses_configured_gross(self):
        gross, _ = self._gross(live=True, verified=True)
        self.assertAlmostEqual(gross, 15000.0, places=3)          # ge used = 1.5

    def test_sim_never_clamps(self):
        gross, stub = self._gross(live=False, verified=False)
        self.assertAlmostEqual(gross, 15000.0, places=3)
        self.assertFalse(any(e.get("action") == "gross_clamped" for e in stub._logs))


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestEnsureLeverageVerify(unittest.TestCase):
    def _stub(self, get_leverage=None):
        calls = []
        adapter = types.SimpleNamespace(
            set_leverage=lambda c, lev: (calls.append((c, lev)) or {"ok": True}))
        if get_leverage is not None:
            adapter.get_leverage = get_leverage
        stub = types.SimpleNamespace(
            _leverage_set=False, _leverage_verified=False, live_trading=True,
            cfg=R.HLXSConfig(max_leverage=5, universe=["BTC", "ETH"]),
            _logs=[], _calls=calls, adapter=adapter)
        stub.log = lambda e: stub._logs.append(e)
        stub._ensure_leverage = types.MethodType(R.HLXSRunner._ensure_leverage, stub)
        return stub

    def test_readback_confirms_pin(self):
        stub = self._stub(get_leverage=lambda c: {"type": "cross", "value": 5})
        stub._ensure_leverage(None)
        self.assertTrue(stub._leverage_verified)
        self.assertEqual(len(stub._calls), 2)                     # pin sent once

    def test_mismatch_stays_unverified_and_retries_verify_only(self):
        stub = self._stub(get_leverage=lambda c: {"type": "cross", "value": 40})
        stub._ensure_leverage(None)
        self.assertFalse(stub._leverage_verified)
        stub._ensure_leverage(None)                               # retry verify
        self.assertEqual(len(stub._calls), 2)                     # pin NOT re-sent

    def test_lower_pin_than_cap_is_still_safe(self):
        # operator pinned 3x manually; cap is 5x → not a violation
        stub = self._stub(get_leverage=lambda c: {"type": "cross", "value": 3})
        stub._ensure_leverage(None)
        self.assertTrue(stub._leverage_verified)

    def test_isolated_margin_is_unverified_even_at_ok_value(self):
        stub = self._stub(get_leverage=lambda c: {"type": "isolated", "value": 5})
        stub._ensure_leverage(None)
        self.assertFalse(stub._leverage_verified)

    def test_no_readback_surface_keeps_old_behaviour(self):
        stub = self._stub(get_leverage=None)                      # adapter w/o get_leverage
        stub._ensure_leverage(None)
        stub._ensure_leverage(None)
        self.assertEqual(len(stub._calls), 2)                     # idempotent pin
        self.assertFalse(stub._leverage_verified)


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestConfigValidation(unittest.TestCase):
    def _load(self, extra):
        base = {"network": "mainnet", "allow_live": False, **extra}
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cfg.json"
            p.write_text(json.dumps(base))
            return R.load_config(str(p))

    def test_valid_delever_band_loads(self):
        cfg = self._load({"soft_delever_dd_pct": 0.06, "soft_delever_factor": 0.5})
        self.assertEqual(cfg.soft_delever_dd_pct, 0.06)

    def test_factor_out_of_range_rejected(self):
        with self.assertRaises(ValueError):
            self._load({"soft_delever_factor": 1.5})

    def test_band_at_or_above_catastrophe_rejected(self):
        with self.assertRaises(ValueError):
            self._load({"soft_delever_dd_pct": 0.12})   # == catastrophe line

    def test_null_band_disables(self):
        cfg = self._load({"soft_delever_dd_pct": None})
        self.assertIsNone(cfg.soft_delever_dd_pct)


class _FakeInfo:
    def __init__(self, user=None, post=None, spot=None, raise_user=False):
        self._user = user or {}
        self._post = post
        self._spot = spot
        self._raise = raise_user

    def user_state(self, addr):
        if self._raise:
            raise RuntimeError("transient")
        return self._user

    def spot_user_state(self, addr):
        if self._spot is None:
            raise RuntimeError("spot endpoint outage")
        return self._spot

    def post(self, path, payload):
        if isinstance(self._post, Exception):
            raise self._post
        return self._post


def _adapter_with(info, address="0xMaster"):
    a = HLAdapter.__new__(HLAdapter)
    a.address = address
    a.info = info
    return a


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestMarginState(unittest.TestCase):
    def test_parses_snapshot_spot_outage_falls_back_to_perp_only(self):
        info = _FakeInfo(user={"marginSummary": {"accountValue": "1000.0",
                                                 "totalMarginUsed": "250.0",
                                                 "totalNtlPos": "2000.0"},
                               "withdrawable": "700.0"})
        m = _adapter_with(info).margin_state()
        self.assertAlmostEqual(m["margin_ratio"], 0.25)   # conservative fallback
        self.assertAlmostEqual(m["total_ntl_pos"], 2000.0)
        self.assertAlmostEqual(m["withdrawable"], 700.0)

    def test_unified_account_ratio_uses_total_equity(self):
        # perp side holds ~just the margin earmark; the buffer sits free in spot.
        # ratio must be used/(perp_av + free spot), NOT used/perp_av (~1.0).
        info = _FakeInfo(
            user={"marginSummary": {"accountValue": "67.0", "totalMarginUsed": "67.0"}},
            spot={"balances": [{"coin": "USDC", "total": "106.0", "hold": "0.0"}]})
        m = _adapter_with(info).margin_state()
        self.assertAlmostEqual(m["margin_ratio"], 67.0 / 173.0, places=4)
        self.assertAlmostEqual(m["account_value"], 173.0)

    def test_flat_account_zero_ratio(self):
        info = _FakeInfo(user={"marginSummary": {"accountValue": "500.0"}},
                         spot={"balances": []})
        self.assertEqual(_adapter_with(info).margin_state()["margin_ratio"], 0.0)

    def test_failure_paths_return_none(self):
        self.assertIsNone(_adapter_with(_FakeInfo(raise_user=True)).margin_state())
        self.assertIsNone(_adapter_with(_FakeInfo(user={})).margin_state())
        self.assertIsNone(_adapter_with(_FakeInfo(), address=None).margin_state())


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestGetLeverage(unittest.TestCase):
    def test_parses_leverage(self):
        info = _FakeInfo(post={"leverage": {"type": "cross", "value": 5}})
        self.assertEqual(_adapter_with(info).get_leverage("BTC"),
                         {"type": "cross", "value": 5})

    def test_failure_paths_return_none(self):
        self.assertIsNone(_adapter_with(_FakeInfo(post=RuntimeError("x"))).get_leverage("BTC"))
        self.assertIsNone(_adapter_with(_FakeInfo(post={})).get_leverage("BTC"))
        self.assertIsNone(_adapter_with(_FakeInfo(), address=None).get_leverage("BTC"))


class TestEquitySampler(unittest.TestCase):
    """The live_trading-flag fix: a LIVE account above $1000 must keep sampling
    (the old `equity >= 1000` sim heuristic silently stopped the history)."""

    def _run(self, health):
        with tempfile.TemporaryDirectory() as d:
            hp, op = Path(d) / "health.json", Path(d) / "equity_history.jsonl"
            hp.write_text(json.dumps(health))
            rc = equity_sampler.sample(hp, op)
            lines = op.read_text().strip().splitlines() if op.exists() else []
            return rc, lines

    def test_live_above_1000_is_sampled(self):
        rc, lines = self._run({"ts": "t1", "equity": 5000.0, "live_trading": True,
                               "peak_equity": 5100.0, "cb_state": "normal"})
        self.assertEqual(rc, 0)
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["equity"], 5000.0)

    def test_sim_is_skipped(self):
        rc, lines = self._run({"ts": "t1", "equity": 5000.0, "live_trading": False})
        self.assertEqual(rc, 0)
        self.assertEqual(lines, [])

    def test_idempotent_on_ts(self):
        with tempfile.TemporaryDirectory() as d:
            hp, op = Path(d) / "health.json", Path(d) / "equity_history.jsonl"
            hp.write_text(json.dumps({"ts": "t1", "equity": 50.0, "live_trading": True}))
            equity_sampler.sample(hp, op)
            equity_sampler.sample(hp, op)
            self.assertEqual(len(op.read_text().strip().splitlines()), 1)

    def test_missing_health_is_error(self):
        with tempfile.TemporaryDirectory() as d:
            rc = equity_sampler.sample(Path(d) / "nope.json", Path(d) / "out.jsonl")
            self.assertEqual(rc, 1)


class TestNotify(unittest.TestCase):
    def test_no_env_is_noop_false(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(notify_mod.send("hi"))

    def test_sends_on_env(self):
        resp = unittest.mock.MagicMock()
        resp.__enter__.return_value = types.SimpleNamespace(status=200)
        with unittest.mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t",
                                                   "TELEGRAM_CHAT_ID": "c"}):
            with unittest.mock.patch.object(notify_mod.urllib.request, "urlopen",
                                            return_value=resp) as uo:
                self.assertTrue(notify_mod.send("hi"))
                self.assertIn("bott/", uo.call_args[0][0].full_url)

    def test_never_raises(self):
        with unittest.mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t",
                                                   "TELEGRAM_CHAT_ID": "c"}):
            with unittest.mock.patch.object(notify_mod.urllib.request, "urlopen",
                                            side_effect=RuntimeError("net down")):
                self.assertFalse(notify_mod.send("hi"))


if __name__ == "__main__":
    unittest.main()
