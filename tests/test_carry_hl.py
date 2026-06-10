"""Hyperliquid carry-lane tests — shim conformance + runner integration.

Network-free (no SDK calls, no creds). Mirrors the `_StubAdapter` discipline of
test_carry_runner_dryrun.py. Covers:
  * HLCarryAdapter duck-types the exact OkxAdapter surface carry_runner uses,
    with OKX envelope shapes (tickers, funding, margin snapshot, order detail).
  * DOUBLE GATE: order paths RAISE (never return a clean envelope) in
    MAINNET_DRY / without a signing wallet, via HLAdapter._assert_can_trade.
  * Mode gates: okx_demo rejected for HL; dry_run=false needs allow_live=true;
    mainnet P3 without HL_CONFIRM_LIVE=YES fails closed at startup.
  * Hourly funding flows into the green button via settlements_per_year=8760
    (and the SAME samples stay OFF at the default 8h cadence).
  * DRY_RUN places zero orders; testnet P3 open/abort/basis-kill flows.
  * State-dir separation (btc-hl vs btc) + parked-safe config defaults.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import carry_runner as cr  # noqa: E402
from carry_runner import (  # noqa: E402
    CarryRunner, CarryRunnerConfig, resolve_mode,
    pull_live_fees, verify_leverage_cap,
)
from mode_gate import (  # noqa: E402
    MODE_DRY, MODE_P3, MODE_MAINNET_DRY, MODE_TESTNET,
)
from hl_carry_adapter import (  # noqa: E402
    HLCarryAdapter, HL_STATIC_FEES, MARGIN_RATIO_CAP,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Explicit account targets for non-DRY constructions (review 2026-06-09 #2/#4:
# any non-DRY HL mode fails closed without a master address + isolation).
_MASTER = "0x" + "aa" * 20
_SUB = "0x" + "bb" * 20

_ENV_KEYS = (
    "OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE",
    "HL_PRIVATE_KEY", "HL_CARRY_PRIVATE_KEY",
    "HL_ACCOUNT_ADDRESS", "HL_CARRY_ACCOUNT_ADDRESS", "HL_CONFIRM_LIVE",
)


def _clean_env():
    for k in _ENV_KEYS:
        os.environ.pop(k, None)


def _funding(rate: str) -> dict:
    return {"code": "0", "msg": "",
            "data": [{"fundingRate": rate, "fundingTime": "1"}]}


def _funding_history(rates: list) -> dict:
    # newest-first, like OKX and like the shim
    return {"code": "0", "msg": "",
            "data": [{"fundingRate": str(r), "fundingTime": str(i)}
                     for i, r in enumerate(rates)]}


def _order_result(ok=True, filled_sz=0.0, avg_px=0.0, error=None):
    return SimpleNamespace(ok=ok, raw={}, filled_sz=filled_sz,
                           avg_px=avg_px, error=error)


# =========================  Fake inner HLAdapter  =========================

class _FakeHL:
    """Network-free stand-in for HLAdapter, recording every order call."""

    MIN_ORDER_USD = 10.0

    def __init__(self, *, mode=MODE_MAINNET_DRY, wallet=True,
                 mids=None, spot_mids=None, hourly_rate=1.2e-05,
                 history_rows=None, margin=None, positions=None,
                 balances=None, order_results=None):
        self.mode = mode
        self.exchange = object() if wallet else None
        self._mids = mids if mids is not None else {"BTC": 63520.0}
        self._spot_mids = spot_mids if spot_mids is not None else {
            "UBTC/USDC": 63500.0, "@142": 63500.0}
        self._hourly = hourly_rate
        self._history_rows = history_rows if history_rows is not None else [
            {"time_ms": 1_000_000 + i * 3_600_000, "rate": 1.2e-05}
            for i in range(300)
        ]
        self._margin = margin
        self._positions = positions if positions is not None else {}
        # balances=False → "read failure" (spot_balances() returns None)
        self._balances = balances if balances is not None else {
            "UBTC": {"total": 0.05, "hold": 0.0, "free": 0.05},
            "USDC": {"total": 3000.0, "hold": 0.0, "free": 3000.0},
        }
        self._order_results = order_results or {}
        self.order_calls = []          # every (kind, args) routed to the venue
        self.positions_raises = False
        self.leverage_pins = []        # every (coin, leverage, is_cross)

    # --- guard (verbatim semantics of HLAdapter._assert_can_trade) ---
    def _assert_can_trade(self):
        if self.exchange is None:
            raise RuntimeError("no signing wallet — supply a private key")
        if self.mode == MODE_MAINNET_DRY:
            raise RuntimeError("MAINNET_DRY: order routing refused.")

    # --- reads ---
    def all_mids(self):
        return dict(self._mids)

    def spot_mids(self):
        return dict(self._spot_mids)

    def funding_daily(self, coins):
        return {c: self._hourly * 24.0 for c in coins if c == "BTC"}

    def funding_history(self, coin, start_ms, end_ms=None, **_):
        return list(self._history_rows)

    def meta(self):
        return {"universe": [{"name": "BTC", "szDecimals": 5,
                              "maxLeverage": 40}]}

    def resolve_spot_pair(self, pair):
        if pair in ("UBTC/USDC", "@142"):
            return {"coin": "@142", "index": 142, "base": "UBTC",
                    "quote": "USDC", "sz_decimals": 5}
        return None

    def margin_state(self):
        return self._margin

    def positions(self):
        if self.positions_raises:
            raise RuntimeError("hard read failure")
        return dict(self._positions)

    def spot_balances(self):
        return None if self._balances is False else dict(self._balances)

    @staticmethod
    def make_cloid(seed):
        return f"cloid:{seed}"

    # --- orders (guarded like the real adapter) ---
    def spot_market_order_usd(self, pair, is_buy, usd_notional, *,
                              slippage=0.05, cloid=None):
        self._assert_can_trade()
        self.order_calls.append(("spot", pair, is_buy, usd_notional))
        return self._order_results.get(
            "spot", _order_result(ok=True, filled_sz=usd_notional / 63500.0,
                                  avg_px=63500.0))

    def market_order_usd(self, coin, is_buy, usd_notional, *,
                         slippage=0.05, cloid=None):
        self._assert_can_trade()
        self.order_calls.append(("perp", coin, is_buy, usd_notional))
        return self._order_results.get(
            "perp", _order_result(ok=True, filled_sz=usd_notional / 63520.0,
                                  avg_px=63520.0))

    def close(self, coin, *, sz=None, slippage=0.05, cloid=None):
        self._assert_can_trade()
        self.order_calls.append(("close", coin, sz))
        return self._order_results.get(
            "close", _order_result(ok=True,
                                   filled_sz=(sz if sz is not None else 0.05),
                                   avg_px=63520.0))

    def set_leverage(self, coin, leverage, *, is_cross=True):
        if self.exchange is None or self.mode == MODE_MAINNET_DRY:
            return {"ok": False, "error": "not live (no wallet / MAINNET_DRY)"}
        self.leverage_pins.append((coin, int(leverage), bool(is_cross)))
        return {"ok": True, "raw": {"status": "ok"}, "error": None}

    def get_leverage(self, coin):
        if not self.leverage_pins:
            return None
        _, lev, cross = self.leverage_pins[-1]
        return {"type": "cross" if cross else "isolated", "value": lev}

    def usd_class_transfer(self, amount_usd, to_perp):
        self._assert_can_trade()
        return {"ok": True, "raw": {}, "error": None}


def _make_shim(fake: _FakeHL, **cfg) -> HLCarryAdapter:
    base = {"network": "mainnet", "coin": "BTC", "spot_pair": "UBTC/USDC",
            "allow_live": False}
    base.update(cfg)
    return HLCarryAdapter(base, hl=fake)


# =========================  Shim conformance  =========================

class TestShimConformance(unittest.TestCase):
    """The exact surface carry_runner consumes must exist with OKX shapes."""

    def setUp(self):
        self.shim = _make_shim(_FakeHL())

    def test_contract_methods_exist(self):
        for m in ("get_spot_ticker", "get_ticker", "assert_unified_margin",
                  "get_margin_snapshot", "place_spot_order", "place_order",
                  "get_spot_order_detail", "get_order_detail"):
            self.assertTrue(callable(getattr(self.shim, m)), m)
        self.assertTrue(callable(self.shim.api.get_funding_rate))
        self.assertTrue(callable(self.shim.api.get_funding_rate_history))

    def test_api_has_no_request_attr(self):
        # carry_runner's fee/leverage probes key off this: no `_request` →
        # static schedule / venue max-leverage paths are used instead.
        self.assertFalse(hasattr(self.shim.api, "_request"))

    def test_spot_ticker_envelope(self):
        r = self.shim.get_spot_ticker("UBTC/USDC")
        self.assertEqual(r["code"], "0")
        self.assertAlmostEqual(float(r["data"][0]["last"]), 63500.0)

    def test_spot_ticker_unknown_pair_fails_closed(self):
        r = self.shim.get_spot_ticker("NOPE/USDC")
        self.assertEqual(r["code"], "1")
        self.assertEqual(r["data"], [])

    def test_perp_ticker_envelope_strips_swap_suffix(self):
        for inst in ("BTC", "BTC-SWAP", None):
            r = self.shim.get_ticker(inst)
            self.assertEqual(r["code"], "0")
            self.assertAlmostEqual(float(r["data"][0]["last"]), 63520.0)

    def test_funding_rate_is_native_hourly(self):
        r = self.shim.api.get_funding_rate(inst_id="BTC-SWAP")
        self.assertEqual(r["code"], "0")
        self.assertAlmostEqual(float(r["data"][0]["fundingRate"]), 1.2e-05)

    def test_funding_history_newest_first_and_limit(self):
        r = self.shim.api.get_funding_rate_history(inst_id="BTC-SWAP", limit=100)
        self.assertEqual(r["code"], "0")
        self.assertEqual(len(r["data"]), 100)
        # fake rows ascend in time → envelope[0] must be the NEWEST
        times = [int(d["fundingTime"]) for d in r["data"]]
        self.assertEqual(times, sorted(times, reverse=True))

    def test_funding_history_honors_window_beyond_okx_100(self):
        fake = _FakeHL(history_rows=[
            {"time_ms": 1_000_000 + i * 3_600_000, "rate": i * 1e-7}
            for i in range(2500)])
        shim = _make_shim(fake)
        r = shim.api.get_funding_rate_history(inst_id="BTC-SWAP", limit=2160)
        self.assertEqual(len(r["data"]), 2160)
        # newest row first (rate of the last fake row = 2499e-7)
        self.assertAlmostEqual(float(r["data"][0]["fundingRate"]), 2499e-7)
        self.assertAlmostEqual(float(r["data"][-1]["fundingRate"]), 340e-7)

    def test_funding_failure_returns_error_envelope(self):
        fake = _FakeHL()
        fake.funding_daily = lambda coins: {}
        shim = _make_shim(fake)
        self.assertEqual(shim.api.get_funding_rate()["code"], "1")
        fake2 = _FakeHL()

        def _boom(*a, **k):
            raise RuntimeError("hard read failure")

        fake2.funding_history = _boom
        shim2 = _make_shim(fake2)
        r = shim2.api.get_funding_rate_history(limit=10)
        self.assertEqual(r["code"], "1")
        self.assertEqual(r["data"], [])

    def test_assert_unified_margin_satisfies_c6(self):
        r = self.shim.assert_unified_margin()
        self.assertTrue(r["ok"])
        self.assertEqual(r["acct_lv"], 3)


class TestShimMarginSnapshot(unittest.TestCase):

    def test_snapshot_inverts_margin_ratio_to_okx_semantics(self):
        # HL ratio = used/equity ("small = safe"); OKX alarm is `< 1.5`
        # ("big = safe") → shim must report equity/used.
        fake = _FakeHL(
            margin={"account_value": 4500.0, "total_margin_used": 1500.0,
                    "total_ntl_pos": 3000.0, "withdrawable": 3000.0,
                    "margin_ratio": 1500.0 / 4500.0},
            positions={"BTC": {"szi": -0.05, "entry_px": 63500.0,
                               "unrealized_pnl": -3.5}},
        )
        snap = _make_shim(fake).get_margin_snapshot(perp_inst_id="BTC")
        self.assertAlmostEqual(snap["margin_ratio"], 3.0)
        self.assertAlmostEqual(snap["total_eq_usd"], 4500.0)
        self.assertAlmostEqual(snap["avail_eq_usd"], 3000.0)
        self.assertAlmostEqual(snap["short_perp_qty"], -0.05)   # signed short
        self.assertAlmostEqual(snap["unrealized_perp_usd"], -3.5)
        self.assertAlmostEqual(snap["spot_btc_qty"], 0.05)      # UBTC total
        self.assertEqual(snap["errors"], [])

    def test_flat_book_ratio_capped_not_infinite(self):
        fake = _FakeHL(margin={"account_value": 1000.0, "total_margin_used": 0.0,
                               "total_ntl_pos": 0.0, "withdrawable": 1000.0,
                               "margin_ratio": 0.0})
        snap = _make_shim(fake).get_margin_snapshot(perp_inst_id="BTC")
        self.assertEqual(snap["margin_ratio"], MARGIN_RATIO_CAP)
        self.assertEqual(snap["short_perp_qty"], 0.0)

    def test_unreadable_account_reports_errors_not_zeros(self):
        fake = _FakeHL(margin=None, balances=False)
        fake.positions_raises = True
        snap = _make_shim(fake).get_margin_snapshot(perp_inst_id="BTC")
        self.assertIsNone(snap["margin_ratio"])
        self.assertIsNone(snap["short_perp_qty"])
        self.assertIsNone(snap["spot_btc_qty"])
        steps = {e["step"] for e in snap["errors"]}
        self.assertEqual(steps, {"margin_state", "positions", "spot_balances"})


# =========================  Order gate (double gate)  =========================

class TestShimOrderGate(unittest.TestCase):
    """MAINNET_DRY / no-wallet must RAISE — never a clean {"code":"1"} reject —
    so a runner bug can't mistake the refusal for an ordinary failed order."""

    def test_mainnet_dry_raises_on_both_legs_with_zero_venue_calls(self):
        fake = _FakeHL(mode=MODE_MAINNET_DRY, wallet=True)
        shim = _make_shim(fake)
        with self.assertRaises(RuntimeError) as cm:
            shim.place_spot_order(inst_id="UBTC/USDC", side="buy",
                                  order_type="market", size="0.05")
        self.assertIn("MAINNET_DRY", str(cm.exception))
        with self.assertRaises(RuntimeError):
            shim.place_order(inst_id="BTC", side="sell",
                             order_type="market", size="0.05")
        with self.assertRaises(RuntimeError):
            shim.place_order(inst_id="BTC", side="buy", order_type="market",
                             size="0.05", reduce_only=True)
        self.assertEqual(fake.order_calls, [])

    def test_no_wallet_raises(self):
        fake = _FakeHL(mode=MODE_TESTNET, wallet=False)
        shim = _make_shim(fake)
        with self.assertRaises(RuntimeError) as cm:
            shim.place_spot_order(inst_id="UBTC/USDC", side="buy",
                                  order_type="market", size="0.05")
        self.assertIn("wallet", str(cm.exception))
        self.assertEqual(fake.order_calls, [])

    def test_usd_class_transfer_also_gated(self):
        shim = _make_shim(_FakeHL(mode=MODE_MAINNET_DRY))
        with self.assertRaises(RuntimeError):
            shim.usd_class_transfer(100.0, to_perp=True)


class TestShimOrders(unittest.TestCase):
    """Testnet (armed) path: base-unit sizing → USD notional → HL IOC."""

    def setUp(self):
        self.fake = _FakeHL(mode=MODE_TESTNET, wallet=True)
        self.shim = _make_shim(self.fake, network="testnet")

    def test_spot_leg_routes_with_usd_notional_and_caches_fill(self):
        r = self.shim.place_spot_order(
            inst_id="UBTC/USDC", side="buy", order_type="market",
            size="0.05000000", td_mode="cash", target_currency="base_ccy")
        self.assertEqual(r["code"], "0")
        oid = r["data"][0]["ordId"]
        kind, pair, is_buy, usd = self.fake.order_calls[0]
        self.assertEqual((kind, pair, is_buy), ("spot", "UBTC/USDC", True))
        self.assertAlmostEqual(usd, 0.05 * 63500.0)
        det = self.shim.get_spot_order_detail("UBTC/USDC", order_id=oid)
        self.assertEqual(det["data"][0]["state"], "filled")
        self.assertAlmostEqual(float(det["data"][0]["accFillSz"]), 0.05)

    def test_perp_open_leg_routes_sell(self):
        r = self.shim.place_order(inst_id="BTC", side="sell",
                                  order_type="market", size="0.05000000",
                                  margin_mode="isolated")
        self.assertEqual(r["code"], "0")
        kind, coin, is_buy, usd = self.fake.order_calls[0]
        self.assertEqual((kind, coin, is_buy), ("perp", "BTC", False))
        self.assertAlmostEqual(usd, 0.05 * 63520.0)

    def test_reduce_only_routes_via_market_close_with_requested_size(self):
        r = self.shim.place_order(inst_id="BTC", side="buy",
                                  order_type="market", size="0.05000000",
                                  margin_mode="isolated", reduce_only=True)
        self.assertEqual(r["code"], "0")
        # The unwind must pass the carry's OWN qty — sz=None would market-close
        # the ENTIRE account position (another lane's leg on a shared account).
        self.assertEqual(self.fake.order_calls, [("close", "BTC", 0.05)])
        det = self.shim.get_order_detail("BTC", order_id=r["data"][0]["ordId"])
        self.assertEqual(det["data"][0]["state"], "filled")

    def test_partial_fill_cached_as_partially_filled_with_actual_qty(self):
        # IOC fills 0.02 of a requested 0.05 → NOT 'filled' (the runner's
        # poll loop must time the leg out and abort/flatten on the real fill).
        self.fake._order_results["perp"] = _order_result(
            ok=True, filled_sz=0.02, avg_px=63520.0)
        r = self.shim.place_order(inst_id="BTC", side="sell",
                                  order_type="market", size="0.05000000")
        self.assertEqual(r["code"], "0")
        det = self.shim.get_order_detail("BTC", order_id=r["data"][0]["ordId"])
        self.assertEqual(det["data"][0]["state"], "partially_filled")
        self.assertAlmostEqual(float(det["data"][0]["accFillSz"]), 0.02)

    def test_fill_within_rounding_tolerance_counts_as_filled(self):
        # szDecimals rounding can shave ~3e-4 relative off the request — that
        # must NOT be misread as a partial (would cause false abort/flatten).
        self.fake._order_results["perp"] = _order_result(
            ok=True, filled_sz=0.05 * (1 - 5e-4), avg_px=63520.0)
        r = self.shim.place_order(inst_id="BTC", side="sell",
                                  order_type="market", size="0.05000000")
        det = self.shim.get_order_detail("BTC", order_id=r["data"][0]["ordId"])
        self.assertEqual(det["data"][0]["state"], "filled")

    def test_venue_reject_maps_to_error_envelope(self):
        self.fake._order_results["spot"] = _order_result(
            ok=False, error="notional $5.00 < $10 min (UBTC/USDC)")
        r = self.shim.place_spot_order(inst_id="UBTC/USDC", side="buy",
                                       order_type="market", size="0.0000001")
        self.assertEqual(r["code"], "1")
        self.assertIn("min", r["msg"])
        self.assertEqual(r["data"], [])

    def test_unknown_order_id_returns_empty_data_fail_closed(self):
        det = self.shim.get_order_detail("BTC", order_id="never-placed")
        self.assertEqual(det["data"], [])


# =========================  Sub-account wiring  =========================

class TestSubAccountWiring(unittest.TestCase):

    def test_exchange_gets_vault_and_master_info_reads_target_sub(self):
        fake_exchange = SimpleNamespace(vault_address=None, account_address=None)
        inner = SimpleNamespace(mode=MODE_MAINNET_DRY, exchange=fake_exchange)
        with patch("hl_carry_adapter.HLAdapter",
                   return_value=inner) as ctor:
            shim = HLCarryAdapter({
                "network": "mainnet", "private_key": "0x" + "11" * 32,
                "account_address": "0xmaster", "sub_account_address": "0xsub",
                "coin": "BTC", "spot_pair": "UBTC/USDC", "allow_live": False,
            })
        # Info reads (positions/margin/balances) must target the SUB.
        self.assertEqual(ctor.call_args.kwargs["account_address"], "0xsub")
        self.assertIs(ctor.call_args.kwargs["allow_live"], False)
        # Orders: agent signs, vaultAddress = sub, payload account = master.
        self.assertEqual(shim.hl.exchange.vault_address, "0xsub")
        self.assertEqual(shim.hl.exchange.account_address, "0xmaster")

    def test_no_sub_account_leaves_exchange_untouched(self):
        fake_exchange = SimpleNamespace(vault_address=None, account_address="0xm")
        inner = SimpleNamespace(mode=MODE_MAINNET_DRY, exchange=fake_exchange)
        with patch("hl_carry_adapter.HLAdapter", return_value=inner):
            HLCarryAdapter({"network": "mainnet",
                            "account_address": "0xm", "allow_live": False})
        self.assertIsNone(fake_exchange.vault_address)
        self.assertEqual(fake_exchange.account_address, "0xm")


# =========================  Startup probes  =========================

class TestStartupProbesViaShim(unittest.TestCase):

    def test_static_fee_schedule_used_instead_of_okx_rest(self):
        shim = _make_shim(_FakeHL())
        fees = pull_live_fees(shim, spot_inst="UBTC/USDC", perp_inst="BTC")
        self.assertAlmostEqual(fees["spot_taker"], HL_STATIC_FEES["spot_taker"])
        self.assertAlmostEqual(fees["perp_taker"], 0.00045)
        self.assertEqual(fees["sources"]["spot_taker"], "adapter_static")

    def test_max_leverage_from_meta_passes_configured_cap(self):
        shim = _make_shim(_FakeHL())
        out = verify_leverage_cap(shim, configured_cap=2.0, perp_inst="BTC")
        self.assertTrue(out["ok"])
        self.assertEqual(out["effective_max"], 40.0)

    def test_max_leverage_mismatch_blocks(self):
        fake = _FakeHL()
        fake.meta = lambda: {"universe": [{"name": "BTC", "maxLeverage": 1}]}
        out = verify_leverage_cap(_make_shim(fake), configured_cap=2.0,
                                  perp_inst="BTC")
        self.assertFalse(out["ok"])
        self.assertIn("MISMATCH", out["message"])

    def test_max_leverage_unreadable_keeps_configured_cap(self):
        fake = _FakeHL()
        fake.meta = lambda: (_ for _ in ()).throw(RuntimeError("down"))
        out = verify_leverage_cap(_make_shim(fake), configured_cap=2.0,
                                  perp_inst="BTC")
        self.assertTrue(out["ok"])
        self.assertIsNone(out["effective_max"])

    def test_okx_probe_paths_unchanged_without_shim_methods(self):
        # Adapters WITHOUT static_fee_schedule/max_leverage take the legacy
        # branches exactly as before (regression for the OKX lane).
        adapter = type("A", (), {})()
        fees = pull_live_fees(adapter, spot_inst="BTC-USDT", perp_inst="BTC-USDT")
        self.assertEqual(fees["sources"]["spot_maker"], "fallback")
        out = verify_leverage_cap(adapter, configured_cap=2.0,
                                  perp_inst="BTC-USDT")
        self.assertIn("no live adapter", out["message"])


# =========================  Runner-facing HL stub  =========================

class _StubApi:
    """api namespace WITHOUT `_request` (faithful to the shim)."""

    def __init__(self, funding_rate, funding_history):
        self.get_funding_rate = MagicMock(return_value=_funding(funding_rate))
        self.get_funding_rate_history = MagicMock(
            return_value=_funding_history(funding_history))


class _StubHLShim:
    """Runner-facing HLCarryAdapter stand-in (DRY flavor: orders forbidden)."""

    def __init__(self, spot="60000.0", perp="60010.0",
                 funding_rate="1.2e-05", funding_history=None, account=None,
                 mode=MODE_MAINNET_DRY):
        self.spot = spot
        self.perp = perp
        self.account = account
        self.mode = mode
        self.api = _StubApi(funding_rate, funding_history or [])
        self.leverage_pins = []        # every (leverage, is_cross) pinned
        self.place_spot_order = MagicMock(side_effect=AssertionError(
            "DRY-RUN VIOLATION: place_spot_order called"))
        self.place_order = MagicMock(side_effect=AssertionError(
            "DRY-RUN VIOLATION: place_order called"))

    def get_spot_ticker(self, inst_id=None):
        return {"code": "0", "msg": "", "data": [{"last": self.spot}]}

    def get_ticker(self, inst_id=None):
        return {"code": "0", "msg": "", "data": [{"last": self.perp}]}

    def assert_unified_margin(self):
        return {"ok": True, "acct_lv": 3, "message": "stub HL unified"}

    def get_margin_snapshot(self, perp_inst_id=None, **_):
        if self.account is None:
            return {"total_eq_usd": None, "errors": []}
        return dict(self.account)

    def static_fee_schedule(self):
        return dict(HL_STATIC_FEES)

    def max_leverage(self, perp_inst=None):
        return 40.0

    def set_leverage(self, leverage, *, is_cross=True):
        self.leverage_pins.append((int(leverage), bool(is_cross)))
        return {"ok": True, "raw": {"status": "ok"}, "error": None}

    def get_leverage(self):
        if not self.leverage_pins:
            return None
        lev, cross = self.leverage_pins[-1]
        return {"type": "cross" if cross else "isolated", "value": lev}

    def get_spot_order_detail(self, inst_id=None, order_id=None, **_):
        return {"code": "1", "msg": "unknown", "data": []}

    def get_order_detail(self, inst_id=None, order_id=None, **_):
        return {"code": "1", "msg": "unknown", "data": []}


class _StubHLLiveShim(_StubHLShim):
    """Testnet-P3 flavor: orders fill synchronously; perp leg can be failed."""

    def __init__(self, *a, perp_fails=False, **k):
        super().__init__(*a, **k)
        self.perp_fails = perp_fails
        self._seq = 0
        self._details = {}
        self.spot_orders = []
        self.perp_orders = []
        # shadow the DRY MagicMocks with real implementations
        self.place_spot_order = self._place_spot_order
        self.place_order = self._place_order

    def _fill(self, size):
        self._seq += 1
        oid = f"hl{self._seq}"
        self._details[oid] = {"ordId": oid, "state": "filled",
                              "accFillSz": str(size)}
        return {"code": "0", "msg": "", "data": [{"ordId": oid}]}

    def _place_spot_order(self, **kw):
        self.spot_orders.append(kw)
        return self._fill(kw.get("size", "0"))

    def _place_perp_order(self, **kw):
        return self._fill(kw.get("size", "0"))

    def _place_order(self, **kw):
        self.perp_orders.append(kw)
        if self.perp_fails:
            return {"code": "1", "msg": "rejected", "data": []}
        return self._fill(kw.get("size", "0"))

    def get_spot_order_detail(self, inst_id=None, order_id=None, **_):
        det = self._details.get(str(order_id))
        if det is None:
            return {"code": "1", "msg": "unknown", "data": []}
        return {"code": "0", "msg": "", "data": [dict(det)]}

    def get_order_detail(self, inst_id=None, order_id=None, **_):
        return self.get_spot_order_detail(inst_id, order_id=order_id)


def _make_hl_runner(tmp: Path, stub, have_creds=False, **cfg_overrides):
    defaults = dict(
        instance_name="carry_hl_test",
        exchange="hyperliquid",
        spot_symbol="UBTC/USDC",
        perp_symbol="BTC",
        dry_run=True,
        okx_demo=False,
        settlements_per_year=8760.0,
        trailing_window_samples=2160,
        cycle_interval_sec=1,
        legging_window_sec=1,
    )
    defaults.update(cfg_overrides)
    cfg = CarryRunnerConfig(**defaults)
    with patch.dict(os.environ, {}, clear=False):
        _clean_env()
        if have_creds:
            os.environ["HL_PRIVATE_KEY"] = "0x" + "11" * 32
        with patch("hl_carry_adapter.HLCarryAdapter", return_value=stub):
            runner = CarryRunner(cfg, state_dir=tmp)
    runner.adapter = stub  # type: ignore[assignment]
    runner.have_private_creds = have_creds
    return runner


# =========================  Runner mode gates  =========================

class TestRunnerHLModeGate(unittest.TestCase):

    def setUp(self):
        _clean_env()

    def test_okx_demo_rejected_for_hyperliquid(self):
        cfg = CarryRunnerConfig(exchange="hyperliquid", dry_run=False,
                                okx_demo=True)
        with self.assertRaises(RuntimeError) as cm:
            resolve_mode(cfg)
        self.assertIn("no demo venue", str(cm.exception))
        with self.assertRaises(RuntimeError):
            CarryRunner(cfg)

    def test_live_without_allow_live_rejected(self):
        cfg = CarryRunnerConfig(exchange="hyperliquid", dry_run=False,
                                okx_demo=False, allow_live=False)
        with self.assertRaises(RuntimeError) as cm:
            CarryRunner(cfg)
        self.assertIn("allow_live", str(cm.exception))

    def test_mainnet_p3_without_hl_confirm_fails_closed_at_startup(self):
        # Carry P3 unlocked, but the HL adapter resolves MAINNET_DRY because
        # HL_CONFIRM_LIVE is absent → the runner must refuse to start.
        stub = _StubHLShim(mode=MODE_MAINNET_DRY)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError) as cm:
                _make_hl_runner(Path(tmp), stub, have_creds=True,
                                dry_run=False, allow_live=True,
                                hl_network="mainnet",
                                hl_account_address=_MASTER,
                                hl_sub_account_address=_SUB,
                                initial_notional_usd=1000.0,
                                target_dn_notional_fraction=0.6,
                                live_max_usd=1000.0)
            self.assertIn("HL_CONFIRM_LIVE", str(cm.exception))

    def test_testnet_p3_constructs(self):
        stub = _StubHLLiveShim(mode=MODE_TESTNET)
        with tempfile.TemporaryDirectory() as tmp:
            runner = _make_hl_runner(Path(tmp), stub, have_creds=True,
                                     dry_run=False, allow_live=True,
                                     hl_network="testnet",
                                     hl_account_address=_MASTER,
                                     hl_sub_account_address=_SUB,
                                     initial_notional_usd=1000.0,
                                     target_dn_notional_fraction=0.6,
                                     live_max_usd=1000.0)
        self.assertEqual(runner.mode, MODE_P3)

    def test_non_dry_without_account_address_fails_closed(self):
        # Agent key alone is NOT an account: without an explicit master/sub
        # address the adapter would target the agent wallet's own (empty)
        # account — must refuse at startup, not log leg failures per cycle.
        stub = _StubHLLiveShim(mode=MODE_TESTNET)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError) as cm:
                _make_hl_runner(Path(tmp), stub, have_creds=True,
                                dry_run=False, allow_live=True,
                                hl_network="testnet",
                                initial_notional_usd=1000.0,
                                target_dn_notional_fraction=0.6,
                                live_max_usd=1000.0)
            self.assertIn("account address", str(cm.exception))

    def test_non_dry_without_isolation_fails_closed(self):
        # Master address set but no sub-account and no explicit dedicated-
        # account confirmation → a carry unwind could flatten another lane's
        # position on a shared master. Must refuse at startup.
        stub = _StubHLLiveShim(mode=MODE_TESTNET)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError) as cm:
                _make_hl_runner(Path(tmp), stub, have_creds=True,
                                dry_run=False, allow_live=True,
                                hl_network="testnet",
                                hl_account_address=_MASTER,
                                initial_notional_usd=1000.0,
                                target_dn_notional_fraction=0.6,
                                live_max_usd=1000.0)
            self.assertIn("isolation", str(cm.exception))

    def test_dedicated_account_confirmation_unlocks_without_sub(self):
        # Identity-checked bool True on the explicit confirmation flag is the
        # only way to arm without a sub-account.
        stub = _StubHLLiveShim(mode=MODE_TESTNET)
        with tempfile.TemporaryDirectory() as tmp:
            runner = _make_hl_runner(Path(tmp), stub, have_creds=True,
                                     dry_run=False, allow_live=True,
                                     hl_network="testnet",
                                     hl_account_address=_MASTER,
                                     hl_dedicated_account_confirmed=True,
                                     initial_notional_usd=1000.0,
                                     target_dn_notional_fraction=0.6,
                                     live_max_usd=1000.0)
        self.assertEqual(runner.mode, MODE_P3)

    def test_dry_run_needs_no_account_address(self):
        # The PARKED default (DRY_RUN, public data only) must keep working
        # without any address configured.
        stub = _StubHLShim(funding_rate="1.2e-05",
                           funding_history=[1.2e-05] * 10)
        with tempfile.TemporaryDirectory() as tmp:
            runner = _make_hl_runner(Path(tmp), stub)
            runner.one_cycle()
        self.assertEqual(runner.mode, MODE_DRY)

    def test_p3_sizing_guard_still_binds_on_hl(self):
        cfg = CarryRunnerConfig(exchange="hyperliquid", dry_run=False,
                                okx_demo=False, allow_live=True,
                                hl_network="testnet",
                                initial_notional_usd=5000.0,
                                target_dn_notional_fraction=0.6,
                                live_max_usd=1000.0)
        with self.assertRaises(RuntimeError) as cm:
            resolve_mode(cfg)
        self.assertIn("sizing guard", str(cm.exception))

    def test_default_config_resolves_dry(self):
        cfg = CarryRunnerConfig(exchange="hyperliquid")
        self.assertEqual(resolve_mode(cfg), MODE_DRY)


# =========================  Runner DRY cycle (hourly cadence)  =========================

class TestRunnerHLDryCycle(unittest.TestCase):

    def test_hourly_samples_at_8760_turn_gate_on_with_correct_sizing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            # 1.2e-5/h × 8760 = +10.512%/yr > +5% threshold → ON
            stub = _StubHLShim(spot="60000.0", perp="60010.0",
                               funding_rate="1.2e-05",
                               funding_history=[1.2e-05] * 240)
            runner = _make_hl_runner(tmp, stub,
                                     initial_notional_usd=5000.0,
                                     target_dn_notional_fraction=0.6,
                                     leverage_cap=2.0,
                                     funding_on_threshold_annualised=0.05)
            entry = runner.one_cycle()
            self.assertEqual(entry["mode"], "DRY_RUN")
            self.assertTrue(entry["gate"]["on"])
            self.assertAlmostEqual(entry["gate"]["trailing_annualised"],
                                   1.2e-05 * 8760, places=9)
            self.assertAlmostEqual(entry["funding_cadence_hours"], 1.0)
            self.assertAlmostEqual(entry["funding_rate_annualised"],
                                   1.2e-05 * 8760, places=9)
            self.assertEqual(entry["action"]["kind"], "would_open")
            self.assertAlmostEqual(entry["target"]["notional_usd"], 3000.0)
            self.assertAlmostEqual(entry["target"]["spot_qty"], 3000.0 / 60000.0)
            self.assertAlmostEqual(entry["target"]["perp_qty"], -3000.0 / 60000.0)
            self.assertAlmostEqual(entry["target"]["perp_margin_usd"], 1500.0)

    def test_same_hourly_samples_stay_off_at_default_8h_cadence(self):
        # The cadence param is load-bearing: 1.2e-5 × 1095 = 1.3%/yr < 5%.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stub = _StubHLShim(funding_rate="1.2e-05",
                               funding_history=[1.2e-05] * 240)
            runner = _make_hl_runner(tmp, stub,
                                     settlements_per_year=1095.0,
                                     funding_on_threshold_annualised=0.05)
            entry = runner.one_cycle()
            self.assertFalse(entry["gate"]["on"])
            self.assertEqual(entry["action"]["kind"], "noop")
            self.assertAlmostEqual(entry["funding_cadence_hours"], 8.0)

    def test_dry_run_places_zero_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stub = _StubHLShim(funding_rate="1.2e-05",
                               funding_history=[1.2e-05] * 240)
            runner = _make_hl_runner(tmp, stub)
            runner.one_cycle()   # would raise AssertionError on any order call
            stub.place_spot_order.assert_not_called()
            stub.place_order.assert_not_called()

    def test_seed_requests_full_trailing_window_not_okx_100(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stub = _StubHLShim(funding_rate="1.2e-05",
                               funding_history=[1.2e-05] * 2160)
            runner = _make_hl_runner(tmp, stub, trailing_window_samples=2160)
            runner.one_cycle()
            kwargs = stub.api.get_funding_rate_history.call_args.kwargs
            self.assertEqual(kwargs["limit"], 2160)
            state = runner.load_state()
            self.assertEqual(len(state.funding_samples), 2160)

    def test_okx_funding_history_still_clamped_to_100(self):
        # Regression: the OKX lane keeps its 100-row page clamp.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cfg = CarryRunnerConfig(instance_name="carry_okx_clamp",
                                    dry_run=True)
            with patch.dict(os.environ, {}, clear=False):
                _clean_env()
                runner = CarryRunner(cfg, state_dir=tmp)
            api = MagicMock()
            api.get_funding_rate_history.return_value = _funding_history([])
            runner.adapter = SimpleNamespace(api=api)
            runner.fetch_funding_history(limit=2160)
            self.assertEqual(
                api.get_funding_rate_history.call_args.kwargs["limit"], 100)

    def test_real_shim_drives_full_dry_cycle(self):
        # Contract parity: the REAL HLCarryAdapter (over a fake HLAdapter in
        # MAINNET_DRY — any order attempt would RAISE) must satisfy the
        # runner's full DRY cycle, not just the test stub.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            shim = _make_shim(_FakeHL(mode=MODE_MAINNET_DRY, hourly_rate=1.2e-05))
            runner = _make_hl_runner(tmp, shim,
                                     initial_notional_usd=5000.0,
                                     target_dn_notional_fraction=0.6,
                                     funding_on_threshold_annualised=0.05)
            entry = runner.one_cycle()
            self.assertEqual(entry["mode"], "DRY_RUN")
            self.assertTrue(entry["gate"]["on"])
            self.assertEqual(entry["gate"]["samples"], 300)
            self.assertAlmostEqual(entry["spot_price"], 63500.0)
            self.assertAlmostEqual(entry["perp_price"], 63520.0)
            self.assertEqual(entry["action"]["kind"], "would_open")
            self.assertAlmostEqual(entry["target"]["spot_qty"], 3000.0 / 63500.0)
            self.assertEqual(shim.hl.order_calls, [])   # zero venue calls
            # JSONL log line written and parseable
            line = (tmp / "carry_hl_test" / "trades.log").read_text().strip()
            self.assertEqual(json.loads(line)["funding_cadence_hours"], 1.0)

    def test_startup_probes_use_shim_static_fees_and_venue_leverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stub = _StubHLShim(funding_rate="1.2e-05",
                               funding_history=[1.2e-05] * 240,
                               account={"acct_lv": 3, "short_perp_qty": 0.0,
                                        "spot_btc_qty": 0.0,
                                        "total_eq_usd": 5000.0,
                                        "margin_ratio": MARGIN_RATIO_CAP,
                                        "errors": []})
            runner = _make_hl_runner(tmp, stub, have_creds=True)
            entry = runner.one_cycle()
            self.assertEqual(entry["fees"]["sources"]["spot_taker"],
                             "adapter_static")
            self.assertAlmostEqual(entry["fees"]["perp_taker"], 0.00045)
            self.assertTrue(entry["leverage_check"]["ok"])
            self.assertEqual(entry["leverage_check"]["effective_max"], 40.0)
            self.assertTrue(entry["reconcile"]["ok"],
                            msg=entry["reconcile"]["errors"])


# =========================  Runner testnet-P3 flows  =========================

class TestRunnerHLTestnetP3(unittest.TestCase):

    def _runner(self, tmp: Path, stub):
        return _make_hl_runner(tmp, stub, have_creds=True,
                               dry_run=False, allow_live=True,
                               hl_network="testnet",
                               hl_account_address=_MASTER,
                               hl_sub_account_address=_SUB,
                               initial_notional_usd=1000.0,
                               target_dn_notional_fraction=0.6,
                               live_max_usd=1000.0,
                               funding_on_threshold_annualised=0.05)

    def test_open_carry_places_both_legs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stub = _StubHLLiveShim(mode=MODE_TESTNET,
                                   funding_rate="1.2e-05",
                                   funding_history=[1.2e-05] * 240)
            runner = self._runner(tmp, stub)
            entry = runner.one_cycle()
            self.assertEqual(entry["mode"], MODE_P3)
            self.assertEqual(entry["action"]["kind"], "do_open")
            self.assertTrue(entry["order_result"]["ok"])
            self.assertEqual(len(stub.spot_orders), 1)
            self.assertEqual(len(stub.perp_orders), 1)
            self.assertEqual(stub.spot_orders[0]["side"], "buy")
            self.assertEqual(stub.perp_orders[0]["side"], "sell")
            state = runner.load_state()
            self.assertAlmostEqual(state.simulated_position["spot_qty"],
                                   600.0 / 60000.0)

    def test_legging_abort_flattens_spot_when_perp_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stub = _StubHLLiveShim(mode=MODE_TESTNET, perp_fails=True,
                                   funding_rate="1.2e-05",
                                   funding_history=[1.2e-05] * 240)
            runner = self._runner(tmp, stub)
            entry = runner.one_cycle()
            self.assertFalse(entry["order_result"]["ok"])
            self.assertEqual(entry["order_result"]["reason"],
                             "legging_abort_perp_failed")
            legs = [leg["leg"] for leg in entry["order_result"]["legs"]]
            self.assertIn("spot_flatten", legs)
            # spot buy + spot flatten-sell; book must stay flat
            self.assertEqual(len(stub.spot_orders), 2)
            self.assertEqual(stub.spot_orders[1]["side"], "sell")
            state = runner.load_state()
            self.assertEqual(state.legging_aborts_total, 1)
            self.assertAlmostEqual(state.simulated_position["spot_qty"], 0.0)

    def test_basis_blowout_unwinds_reduce_only_and_halts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stub = _StubHLLiveShim(mode=MODE_TESTNET,
                                   spot="60000.0", perp="70000.0",  # ~16% basis
                                   funding_rate="1.2e-05",
                                   funding_history=[1.2e-05] * 240)
            runner = self._runner(tmp, stub)
            state = runner.load_state()
            state.dry_run = False
            state.simulated_position = {
                "spot_qty": 0.01, "perp_qty": -0.01,
                "entry_spot_price": 60000.0, "entry_perp_price": 60010.0,
                "funding_accrued": 0.0, "fees_paid": 0.0,
                "opened_ts": "2026-06-09T00:00:00+00:00",
                "last_updated_ts": None,
            }
            runner.save_state(state)
            entry = runner.one_cycle()
            self.assertEqual(entry["action"]["kind"], "do_unwind")
            self.assertEqual(entry["action"]["reason"], "basis_blowout_kill")
            self.assertTrue(entry["order_result"]["ok"])
            self.assertTrue(entry["halted"])
            self.assertEqual(entry["halt_reason"], "basis_blowout")
            # perp close leg must be reduce-only (can never flip the book)
            self.assertTrue(stub.perp_orders[-1].get("reduce_only"))
            state = runner.load_state()
            self.assertAlmostEqual(state.simulated_position["perp_qty"], 0.0)

    def test_first_live_cycle_pins_leverage_with_read_back(self):
        # Review 2026-06-09 #5: verify_leverage_cap only checks the venue
        # ALLOWS the cap — the runner must also PIN it (cross) and verify by
        # read-back, or the short inherits the account's prior leverage (up
        # to 40× BTC) and margin_ratio_alarm can never fire.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stub = _StubHLLiveShim(mode=MODE_TESTNET,
                                   funding_rate="1.2e-05",
                                   funding_history=[1.2e-05] * 240)
            runner = self._runner(tmp, stub)
            runner.one_cycle()
            self.assertEqual(stub.leverage_pins, [(2, True)])
            runner.one_cycle()   # probes are one-shot — no re-pin per cycle
            self.assertEqual(stub.leverage_pins, [(2, True)])

    def test_leverage_pin_failure_halts_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stub = _StubHLLiveShim(mode=MODE_TESTNET,
                                   funding_rate="1.2e-05",
                                   funding_history=[1.2e-05] * 240)
            stub.set_leverage = lambda lev, is_cross=True: {
                "ok": False, "error": "venue rejected"}
            runner = self._runner(tmp, stub)
            with self.assertRaises(RuntimeError) as cm:
                runner.one_cycle()
            self.assertIn("leverage pin", str(cm.exception))
            state = runner.load_state()
            self.assertTrue(state.halted)
            self.assertIn("leverage pin", state.halt_reason or "")

    def test_leverage_read_back_mismatch_halts_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stub = _StubHLLiveShim(mode=MODE_TESTNET,
                                   funding_rate="1.2e-05",
                                   funding_history=[1.2e-05] * 240)
            stub.get_leverage = lambda: {"type": "cross", "value": 40}
            runner = self._runner(tmp, stub)
            with self.assertRaises(RuntimeError) as cm:
                runner.one_cycle()
            self.assertIn("read-back", str(cm.exception))

    def test_partial_spot_fill_aborts_open_without_booking_full_qty(self):
        # Real shim over a fake venue: leg-1 IOC fills ~10% of the request →
        # the shim reports 'partially_filled', the runner times the leg out
        # and books NOTHING (no silent net delta in the simulated book).
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fake = _FakeHL(mode=MODE_TESTNET, hourly_rate=1.2e-05,
                           order_results={"spot": _order_result(
                               ok=True, filled_sz=0.001, avg_px=63500.0)})
            shim = _make_shim(fake, network="testnet")
            runner = self._runner(tmp, shim)
            entry = runner.one_cycle()
            self.assertEqual(entry["action"]["kind"], "do_open")
            self.assertFalse(entry["order_result"]["ok"])
            self.assertEqual(entry["order_result"]["reason"],
                             "spot_open_did_not_fill")
            state = runner.load_state()
            self.assertAlmostEqual(state.simulated_position["spot_qty"], 0.0)
            self.assertAlmostEqual(state.simulated_position["perp_qty"], 0.0)
            # only the spot leg ever reached the venue
            kinds = [c[0] for c in fake.order_calls]
            self.assertEqual(kinds, ["spot"])


# =========================  Funding-window integrity  =========================

class TestFundingWindowPerSettlement(unittest.TestCase):
    """Review 2026-06-09 #1: the green-button window must hold one sample per
    SETTLEMENT (trailing 90d), never one per 60s cycle (trailing ~36h)."""

    def test_steady_state_cycles_do_not_displace_the_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            # Predicted rate is a huge outlier vs the settled history — it must
            # NEVER enter the window between settlements.
            stub = _StubHLShim(funding_rate="9.9e-03",
                               funding_history=[1.2e-05] * 240)
            runner = _make_hl_runner(tmp, stub)
            runner.one_cycle()
            runner.one_cycle()
            runner.one_cycle()
            state = runner.load_state()
            self.assertEqual(len(state.funding_samples), 240)
            self.assertTrue(all(abs(s - 1.2e-05) < 1e-12
                                for s in state.funding_samples))
            # history fetched once — refresh is per settlement, not per cycle
            self.assertEqual(stub.api.get_funding_rate_history.call_count, 1)
            # the gate annualises the SETTLED window, not the spiky prediction
            self.assertAlmostEqual(
                json.loads((tmp / "carry_hl_test" / "trades.log")
                           .read_text().strip().split("\n")[-1])
                ["gate"]["trailing_annualised"],
                1.2e-05 * 8760, places=9)

    def test_window_refreshes_wholesale_after_a_settlement_period(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stub = _StubHLShim(funding_rate="1.2e-05",
                               funding_history=[1.2e-05] * 240)
            runner = _make_hl_runner(tmp, stub)
            runner.one_cycle()
            self.assertEqual(stub.api.get_funding_rate_history.call_count, 1)
            # Age the last refresh past one HL settlement period (1h @ 8760).
            state = runner.load_state()
            state.funding_window_last_refresh_ts = "2026-01-01T00:00:00+00:00"
            runner.save_state(state)
            stub.api.get_funding_rate_history.return_value = _funding_history(
                [2.4e-05] * 240)
            runner.one_cycle()
            self.assertEqual(stub.api.get_funding_rate_history.call_count, 2)
            state = runner.load_state()
            self.assertTrue(all(abs(s - 2.4e-05) < 1e-12
                                for s in state.funding_samples))

    def test_failed_history_read_keeps_previous_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stub = _StubHLShim(funding_rate="1.2e-05",
                               funding_history=[1.2e-05] * 240)
            runner = _make_hl_runner(tmp, stub)
            runner.one_cycle()
            state = runner.load_state()
            state.funding_window_last_refresh_ts = "2026-01-01T00:00:00+00:00"
            runner.save_state(state)
            stub.api.get_funding_rate_history.return_value = {
                "code": "1", "msg": "boom", "data": []}
            runner.one_cycle()
            state = runner.load_state()
            self.assertEqual(len(state.funding_samples), 240)  # stale > zeroed


# =========================  Config + state-dir separation  =========================

class TestHLConfigAndStateSeparation(unittest.TestCase):

    def test_shipped_config_is_parked_safe(self):
        cfg = cr.load_config(str(PROJECT_ROOT / "configs" / "carry-hl-btc.json"))
        self.assertEqual(cfg.exchange, "hyperliquid")
        self.assertEqual(cfg.instance_name, "btc-hl")
        self.assertEqual(cfg.spot_symbol, "UBTC/USDC")
        self.assertEqual(cfg.perp_symbol, "BTC")
        self.assertTrue(cfg.dry_run)
        self.assertFalse(cfg.okx_demo)
        self.assertFalse(cfg.allow_live)
        self.assertFalse(cfg.hl_dedicated_account_confirmed)
        self.assertIsNone(cfg.hl_sub_account_address)
        self.assertEqual(cfg.hl_network, "mainnet")
        self.assertEqual(cfg.leverage_cap, 2.0)
        self.assertEqual(cfg.settlements_per_year, 8760)
        self.assertEqual(cfg.trailing_window_samples, 2160)
        self.assertEqual(cfg.funding_on_threshold_annualised, 0.05)
        self.assertEqual(cfg.live_max_usd, 1000)
        self.assertEqual(resolve_mode(cfg), MODE_DRY)

    def test_flipping_dry_run_alone_cannot_go_live(self):
        cfg = cr.load_config(str(PROJECT_ROOT / "configs" / "carry-hl-btc.json"))
        cfg.dry_run = False
        with self.assertRaises(RuntimeError):
            resolve_mode(cfg)   # allow_live=false → fail closed

    def test_state_dir_does_not_collide_with_okx_carry(self):
        hl = cr.load_config(str(PROJECT_ROOT / "configs" / "carry-hl-btc.json"))
        okx = cr.load_config(str(PROJECT_ROOT / "configs" / "carry-btc.json"))
        self.assertNotEqual(hl.instance_name, okx.instance_name)
        base = PROJECT_ROOT / "state" / "carry"
        self.assertNotEqual(base / hl.instance_name, base / okx.instance_name)

    def test_runner_writes_under_its_own_instance_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stub = _StubHLShim(funding_rate="1.2e-05",
                               funding_history=[1.2e-05] * 10)
            runner = _make_hl_runner(tmp, stub, instance_name="btc-hl")
            runner.one_cycle()
            self.assertTrue((tmp / "btc-hl" / "state.json").exists())
            self.assertTrue((tmp / "btc-hl" / "trades.log").exists())
            self.assertFalse((tmp / "btc").exists())
            d = json.loads((tmp / "btc-hl" / "state.json").read_text())
            self.assertTrue(d["dry_run"])


if __name__ == "__main__":
    unittest.main()
