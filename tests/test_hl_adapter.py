"""Tests for the Hyperliquid adapter's pure logic (network-free).

The signing + data paths are proven by the testnet self-test
(`python -m scripts.hl_adapter --selftest`); these cover the safety gate and
response parsing without touching the network.
"""

import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

try:
    from hl_adapter import (  # noqa: E402
        resolve_hl_mode, _mask, HLAdapter,
        MODE_TESTNET, MODE_MAINNET_DRY, MODE_MAINNET_LIVE,
    )
    HAVE_SDK = True
except ImportError:
    HAVE_SDK = False


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestModeGate(unittest.TestCase):
    def test_testnet(self):
        self.assertEqual(resolve_hl_mode("testnet", False), MODE_TESTNET)
        self.assertEqual(resolve_hl_mode("testnet", True), MODE_TESTNET)

    def test_mainnet_dry_default(self):
        self.assertEqual(resolve_hl_mode("mainnet", False), MODE_MAINNET_DRY)

    def test_mainnet_live_strict(self):
        import os
        os.environ.pop("HL_CONFIRM_LIVE", None)
        # allow_live=True alone is NOT enough (needs HL_CONFIRM_LIVE=YES)
        self.assertEqual(resolve_hl_mode("mainnet", True), MODE_MAINNET_DRY)
        # truthy non-bool must NEVER select live (the critical bug this guards)
        self.assertEqual(resolve_hl_mode("mainnet", "false"), MODE_MAINNET_DRY)
        self.assertEqual(resolve_hl_mode("mainnet", 1), MODE_MAINNET_DRY)
        os.environ["HL_CONFIRM_LIVE"] = "YES"
        try:
            self.assertEqual(resolve_hl_mode("mainnet", True), MODE_MAINNET_LIVE)
            self.assertEqual(resolve_hl_mode("mainnet", "true"), MODE_MAINNET_DRY)   # strict identity
            self.assertEqual(resolve_hl_mode("mainnet", 1), MODE_MAINNET_DRY)
        finally:
            os.environ.pop("HL_CONFIRM_LIVE", None)

    def test_bad_network(self):
        with self.assertRaises(ValueError):
            resolve_hl_mode("demo", False)


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestMask(unittest.TestCase):
    def test_masks_address(self):
        m = _mask("0x1234567890abcdef1234")
        self.assertTrue(m.startswith("0x1234"))
        self.assertTrue(m.endswith("1234"))
        self.assertIn("…", m)

    def test_short_passthrough(self):
        self.assertEqual(_mask(None), "None")


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestParseOrder(unittest.TestCase):
    def test_filled(self):
        raw = {"status": "ok", "response": {"data": {"statuses": [
            {"filled": {"totalSz": "0.001", "avgPx": "66000.0", "oid": 1}}]}}}
        r = HLAdapter._parse_order(raw)
        self.assertTrue(r.ok)
        self.assertAlmostEqual(r.filled_sz, 0.001)
        self.assertAlmostEqual(r.avg_px, 66000.0)

    def test_status_err(self):
        raw = {"status": "err", "response": "insufficient margin"}
        r = HLAdapter._parse_order(raw)
        self.assertFalse(r.ok)
        self.assertIn("insufficient", r.error)

    def test_resting_is_not_filled(self):
        # a marketable IOC that rests/cancels with NO fill must be ok=False
        # (phantom-leg guard) — not a silent success.
        raw = {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 7}}]}}}
        r = HLAdapter._parse_order(raw)
        self.assertFalse(r.ok)
        self.assertEqual(r.filled_sz, 0.0)

    def test_zero_fill_is_not_ok(self):
        raw = {"status": "ok", "response": {"data": {"statuses": [
            {"filled": {"totalSz": "0.0", "avgPx": "66000.0", "oid": 1}}]}}}
        self.assertFalse(HLAdapter._parse_order(raw).ok)

    def test_none_response(self):
        r = HLAdapter._parse_order(None)
        self.assertFalse(r.ok)
        self.assertIn("None", r.error)

    def test_order_level_error(self):
        raw = {"status": "ok", "response": {"data": {"statuses": [
            {"error": "Order has invalid size"}]}}}
        r = HLAdapter._parse_order(raw)
        self.assertFalse(r.ok)
        self.assertIn("invalid size", r.error)


class _FakeInfo:
    """Network-free stand-in exposing the two reads account_value uses."""
    def __init__(self, perp=None, spot=None, raise_perp=False, raise_spot=False):
        self._perp = perp or {}
        self._spot = spot or {}
        self._raise_perp = raise_perp
        self._raise_spot = raise_spot

    def user_state(self, addr):
        if self._raise_perp:
            raise RuntimeError("transient net error")
        return self._perp

    def spot_user_state(self, addr):
        if self._raise_spot:
            raise RuntimeError("spot endpoint outage")
        return self._spot


def _adapter_with(info, address="0xMaster"):
    a = HLAdapter.__new__(HLAdapter)        # bypass __init__ (no network/meta fetch)
    a.address = address
    a.info = info
    return a


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestAccountValue(unittest.TestCase):
    # equity = perp marginSummary.accountValue + free spot USDC (total - hold)
    def test_standard_mode_uses_perp_account_value(self):
        # funds on perp side, spot empty → perp accountValue is the equity
        info = _FakeInfo(perp={"marginSummary": {"accountValue": "999.0"}},
                         spot={"balances": []})
        self.assertAlmostEqual(_adapter_with(info).account_value(), 999.0)

    def test_unified_mode_no_positions_uses_spot(self):
        # perp marginSummary is the 'not meaningful' 0, all USDC free in spot
        info = _FakeInfo(perp={"marginSummary": {"accountValue": "0.0"}},
                         spot={"balances": [{"coin": "USDC", "total": "999.0", "hold": "0.0"}]})
        self.assertAlmostEqual(_adapter_with(info).account_value(), 999.0)

    def test_unified_mode_with_positions_sums_perp_and_free_spot(self):
        # empirically-observed unified-with-positions shape: equity = perp AV +
        # free spot (total-hold) = 198.85 + (990.01-197.99) = 990.87. Subtracting
        # `hold` removes the margin that unified mode also counts in perp AV;
        # NOT subtracting it would wrongly give 1188 on a ~991 account.
        info = _FakeInfo(
            perp={"marginSummary": {"accountValue": "198.85"}},
            spot={"balances": [{"coin": "USDC", "total": "990.01", "hold": "197.99"}]})
        self.assertAlmostEqual(_adapter_with(info).account_value(), 990.87, places=2)

    def test_genuinely_unfunded_returns_zero(self):
        info = _FakeInfo(perp={"marginSummary": {"accountValue": "0.0"}},
                         spot={"balances": [{"coin": "USDC", "total": "0.0", "hold": "0.0"}]})
        self.assertEqual(_adapter_with(info).account_value(), 0.0)

    def test_spot_outage_perp_funded_uses_perp(self):
        # a perp-funded (standard-mode) account must NOT skip on a spot outage
        import hl_adapter
        with unittest.mock.patch.object(hl_adapter.time, "sleep", lambda *_: None):
            info = _FakeInfo(perp={"marginSummary": {"accountValue": "750.0"}}, raise_spot=True)
            self.assertAlmostEqual(_adapter_with(info).account_value(), 750.0)

    def test_spot_outage_no_perp_returns_none(self):
        # perp ~0 + spot unreadable → genuinely undeterminable → None (skip, no halt)
        import hl_adapter
        with unittest.mock.patch.object(hl_adapter.time, "sleep", lambda *_: None):
            info = _FakeInfo(perp={"marginSummary": {"accountValue": "0.0"}}, raise_spot=True)
            self.assertIsNone(_adapter_with(info).account_value())

    def test_no_address_returns_zero(self):
        self.assertEqual(_adapter_with(_FakeInfo(), address=None).account_value(), 0.0)

    def test_transient_read_failure_returns_none(self):
        import hl_adapter
        with unittest.mock.patch.object(hl_adapter.time, "sleep", lambda *_: None):
            self.assertIsNone(_adapter_with(_FakeInfo(raise_perp=True)).account_value())


# ---------------------------------------------------------------------------
# Spot surface (pair resolution, mids, balances, orders, transfer, funding)
# ---------------------------------------------------------------------------

# Minimal spot metadata: one canonical pair (PURR/USDC) + one Unit-bridged pair
# whose universe name is the bare "@142" index form (UBTC/USDC).
_SPOT_META = {
    "tokens": [
        {"name": "USDC", "szDecimals": 8, "weiDecimals": 8, "index": 0, "isCanonical": True},
        {"name": "PURR", "szDecimals": 0, "weiDecimals": 5, "index": 1, "isCanonical": True},
        {"name": "UBTC", "szDecimals": 5, "weiDecimals": 8, "index": 142, "isCanonical": False},
    ],
    "universe": [
        {"name": "PURR/USDC", "tokens": [1, 0], "index": 0, "isCanonical": True},
        {"name": "@142", "tokens": [142, 0], "index": 142, "isCanonical": False},
    ],
}


class _FakeSpotInfo:
    """Network-free stand-in for the spot + funding reads."""

    def __init__(self, meta=None, ctxs=None, spot=None, funding_rows=None,
                 page_cap=500, raise_meta=False, raise_ctxs=False,
                 raise_spot=False, raise_funding=False):
        self._meta = _SPOT_META if meta is None else meta
        self._ctxs = ctxs if ctxs is not None else []
        self._spot = spot or {}
        self._funding_rows = sorted(funding_rows or [], key=lambda r: r["time"])
        self._page_cap = page_cap
        self._raise_meta = raise_meta
        self._raise_ctxs = raise_ctxs
        self._raise_spot = raise_spot
        self._raise_funding = raise_funding
        self.spot_meta_calls = 0
        self.funding_calls = []

    def spot_meta(self):
        self.spot_meta_calls += 1
        if self._raise_meta:
            raise RuntimeError("spotMeta outage")
        return self._meta

    def spot_meta_and_asset_ctxs(self):
        if self._raise_ctxs:
            raise RuntimeError("spotMetaAndAssetCtxs outage")
        return [self._meta, self._ctxs]

    def spot_user_state(self, addr):
        if self._raise_spot:
            raise RuntimeError("spot endpoint outage")
        return self._spot

    def funding_history(self, coin, startTime, endTime=None):
        if self._raise_funding:
            raise RuntimeError("fundingHistory outage")
        self.funding_calls.append((coin, startTime, endTime))
        rows = [r for r in self._funding_rows if r["time"] >= startTime
                and (endTime is None or r["time"] <= endTime)]
        return rows[: self._page_cap]


class _FakeExchange:
    """Records order/transfer calls and returns a canned response."""

    def __init__(self, raw=None, raise_exc=False):
        self.raw = raw if raw is not None else {"status": "ok"}
        self.raise_exc = raise_exc
        self.calls = []

    def market_open(self, name, is_buy, sz, px=None, slippage=0.05, cloid=None):
        self.calls.append(("market_open", name, is_buy, sz, px, slippage, cloid))
        if self.raise_exc:
            raise RuntimeError("exchange down")
        return self.raw

    def usd_class_transfer(self, amount, to_perp):
        self.calls.append(("usd_class_transfer", amount, to_perp))
        if self.raise_exc:
            raise RuntimeError("exchange down")
        return self.raw


def _spot_adapter(info, exchange=None, mode=None, address="0xMaster"):
    a = HLAdapter.__new__(HLAdapter)       # bypass __init__ (no network/meta fetch)
    a.address = address
    a.info = info
    a.exchange = exchange
    a.mode = mode if mode is not None else MODE_TESTNET
    a._spot_meta_cache = None
    return a


def _ubtc_ctxs(mid="65000.0", mark=None):
    # ctxs are indexed parallel to the universe: [PURR/USDC, @142]
    return [{"coin": "PURR/USDC", "midPx": "0.35", "markPx": "0.35"},
            {"coin": "@142", "midPx": mid, "markPx": mark}]


_FILLED_RAW = {"status": "ok", "response": {"data": {"statuses": [
    {"filled": {"totalSz": "0.1", "avgPx": "65000.0", "oid": 9}}]}}}


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestSpotPairResolution(unittest.TestCase):
    def test_unit_bridged_composed_name_resolves(self):
        # "UBTC/USDC" is NOT the universe name ("@142") — resolution must compose
        # it from the token table and carry the BASE token's szDecimals.
        a = _spot_adapter(_FakeSpotInfo())
        rec = a.resolve_spot_pair("UBTC/USDC")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["coin"], "@142")
        self.assertEqual(rec["sz_decimals"], 5)
        self.assertEqual(rec["base"], "UBTC")
        self.assertEqual(rec["quote"], "USDC")

    def test_canonical_pair_resolves_to_itself(self):
        rec = _spot_adapter(_FakeSpotInfo()).resolve_spot_pair("PURR/USDC")
        self.assertEqual(rec["coin"], "PURR/USDC")
        self.assertEqual(rec["sz_decimals"], 0)

    def test_universe_name_lookup(self):
        rec = _spot_adapter(_FakeSpotInfo()).resolve_spot_pair("@142")
        self.assertEqual(rec["base"], "UBTC")

    def test_unknown_pair_is_none(self):
        self.assertIsNone(_spot_adapter(_FakeSpotInfo()).resolve_spot_pair("DOGE/USDC"))

    def test_spot_pairs_lists_all_and_meta_is_cached(self):
        info = _FakeSpotInfo()
        a = _spot_adapter(info)
        pairs = a.spot_pairs()
        self.assertEqual(set(pairs), {"PURR/USDC", "UBTC/USDC"})
        a.spot_pairs(); a.resolve_spot_pair("UBTC/USDC")
        self.assertEqual(info.spot_meta_calls, 1)      # cached after first read

    def test_meta_outage_is_empty_not_raise(self):
        a = _spot_adapter(_FakeSpotInfo(raise_meta=True))
        self.assertEqual(a.spot_pairs(), {})
        self.assertIsNone(a.resolve_spot_pair("UBTC/USDC"))


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestSpotMids(unittest.TestCase):
    def test_keys_both_names(self):
        a = _spot_adapter(_FakeSpotInfo(ctxs=_ubtc_ctxs()))
        mids = a.spot_mids()
        self.assertAlmostEqual(mids["@142"], 65000.0)
        self.assertAlmostEqual(mids["UBTC/USDC"], 65000.0)
        self.assertAlmostEqual(mids["PURR/USDC"], 0.35)

    def test_markpx_fallback_when_no_mid(self):
        a = _spot_adapter(_FakeSpotInfo(ctxs=_ubtc_ctxs(mid=None, mark="64900.0")))
        self.assertAlmostEqual(a.spot_mids()["UBTC/USDC"], 64900.0)

    def test_outage_is_empty_dict(self):
        self.assertEqual(_spot_adapter(_FakeSpotInfo(raise_ctxs=True)).spot_mids(), {})


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestSpotBalances(unittest.TestCase):
    def test_full_list_with_free(self):
        info = _FakeSpotInfo(spot={"balances": [
            {"coin": "USDC", "total": "990.01", "hold": "197.99"},
            {"coin": "UBTC", "total": "0.05", "hold": "0.0"}]})
        b = _spot_adapter(info).spot_balances()
        self.assertAlmostEqual(b["USDC"]["free"], 792.02, places=2)
        self.assertAlmostEqual(b["UBTC"]["total"], 0.05)
        self.assertAlmostEqual(b["UBTC"]["free"], 0.05)

    def test_outage_is_none_not_flat(self):
        self.assertIsNone(_spot_adapter(_FakeSpotInfo(raise_spot=True)).spot_balances())

    def test_no_address_is_empty(self):
        self.assertEqual(_spot_adapter(_FakeSpotInfo(), address=None).spot_balances(), {})


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestSpotOrderGuards(unittest.TestCase):
    def test_mainnet_dry_refuses_and_never_routes(self):
        ex = _FakeExchange(raw=_FILLED_RAW)
        a = _spot_adapter(_FakeSpotInfo(ctxs=_ubtc_ctxs()), exchange=ex,
                          mode=MODE_MAINNET_DRY)
        with self.assertRaises(RuntimeError):
            a.spot_market_order_usd("UBTC/USDC", True, 6500.0)
        self.assertEqual(ex.calls, [])

    def test_no_wallet_refuses(self):
        a = _spot_adapter(_FakeSpotInfo(ctxs=_ubtc_ctxs()), exchange=None)
        with self.assertRaises(RuntimeError):
            a.spot_market_order_usd("UBTC/USDC", True, 6500.0)

    def test_below_min_rejected_without_routing(self):
        ex = _FakeExchange(raw=_FILLED_RAW)
        a = _spot_adapter(_FakeSpotInfo(ctxs=_ubtc_ctxs()), exchange=ex)
        r = a.spot_market_order_usd("UBTC/USDC", True, 9.99)
        self.assertFalse(r.ok)
        self.assertIn("min", r.error)
        self.assertEqual(ex.calls, [])

    def test_unknown_pair_rejected(self):
        a = _spot_adapter(_FakeSpotInfo(ctxs=_ubtc_ctxs()), exchange=_FakeExchange())
        r = a.spot_market_order_usd("DOGE/USDC", True, 100.0)
        self.assertFalse(r.ok)
        self.assertIn("unknown spot pair", r.error)

    def test_no_mid_rejected(self):
        a = _spot_adapter(_FakeSpotInfo(ctxs=[]), exchange=_FakeExchange())
        r = a.spot_market_order_usd("UBTC/USDC", True, 100.0)
        self.assertFalse(r.ok)
        self.assertIn("no spot mid", r.error)

    def test_size_rounds_to_zero_rejected(self):
        # PURR szDecimals=0 with a $30 mid: $12 → 0.4 → rounds to 0 → reject
        ctxs = [{"coin": "PURR/USDC", "midPx": "30.0", "markPx": "30.0"},
                {"coin": "@142", "midPx": "65000.0", "markPx": None}]
        a = _spot_adapter(_FakeSpotInfo(ctxs=ctxs), exchange=_FakeExchange())
        r = a.spot_market_order_usd("PURR/USDC", True, 12.0)
        self.assertFalse(r.ok)
        self.assertIn("rounds to 0", r.error)

    def test_rounding_drift_below_min_rejected(self):
        # szDecimals=0, mid $9: $12 → sz 1 → $9 rounded notional < $10 min
        ctxs = [{"coin": "PURR/USDC", "midPx": "9.0", "markPx": "9.0"},
                {"coin": "@142", "midPx": "65000.0", "markPx": None}]
        a = _spot_adapter(_FakeSpotInfo(ctxs=ctxs), exchange=_FakeExchange())
        r = a.spot_market_order_usd("PURR/USDC", True, 12.0)
        self.assertFalse(r.ok)
        self.assertIn("off-target", r.error)

    def test_happy_path_routes_universe_name_and_base_szdecimals(self):
        ex = _FakeExchange(raw=_FILLED_RAW)
        a = _spot_adapter(_FakeSpotInfo(ctxs=_ubtc_ctxs()), exchange=ex)
        r = a.spot_market_order_usd("UBTC/USDC", True, 6500.0, slippage=0.02)
        self.assertTrue(r.ok)
        self.assertAlmostEqual(r.filled_sz, 0.1)
        self.assertAlmostEqual(r.avg_px, 65000.0)
        kind, name, is_buy, sz, px, slip, cloid = ex.calls[0]
        self.assertEqual((kind, name, is_buy), ("market_open", "@142", True))
        self.assertAlmostEqual(sz, 0.1)                # 6500/65000, szDecimals=5
        self.assertAlmostEqual(slip, 0.02)

    def test_exchange_exception_is_error_result_not_raise(self):
        a = _spot_adapter(_FakeSpotInfo(ctxs=_ubtc_ctxs()),
                          exchange=_FakeExchange(raise_exc=True))
        r = a.spot_market_order_usd("UBTC/USDC", True, 6500.0)
        self.assertFalse(r.ok)
        self.assertIn("exception", r.error)


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestUsdClassTransfer(unittest.TestCase):
    def test_mainnet_dry_refuses(self):
        ex = _FakeExchange()
        a = _spot_adapter(_FakeSpotInfo(), exchange=ex, mode=MODE_MAINNET_DRY)
        with self.assertRaises(RuntimeError):
            a.usd_class_transfer(100.0, True)
        self.assertEqual(ex.calls, [])

    def test_no_wallet_refuses(self):
        a = _spot_adapter(_FakeSpotInfo(), exchange=None)
        with self.assertRaises(RuntimeError):
            a.usd_class_transfer(100.0, True)

    def test_ok_path_records_call(self):
        ex = _FakeExchange(raw={"status": "ok"})
        a = _spot_adapter(_FakeSpotInfo(), exchange=ex)
        r = a.usd_class_transfer(123.45, True)
        self.assertTrue(r["ok"])
        self.assertEqual(ex.calls, [("usd_class_transfer", 123.45, True)])

    def test_non_ok_status_is_error(self):
        ex = _FakeExchange(raw={"status": "err", "response": "denied"})
        r = _spot_adapter(_FakeSpotInfo(), exchange=ex).usd_class_transfer(50.0, False)
        self.assertFalse(r["ok"])
        self.assertIn("denied", r["error"])

    def test_nonpositive_amount_rejected_without_call(self):
        ex = _FakeExchange()
        a = _spot_adapter(_FakeSpotInfo(), exchange=ex)
        self.assertFalse(a.usd_class_transfer(0.0, True)["ok"])
        self.assertFalse(a.usd_class_transfer(-5.0, True)["ok"])
        self.assertEqual(ex.calls, [])

    def test_exception_is_error_dict(self):
        a = _spot_adapter(_FakeSpotInfo(), exchange=_FakeExchange(raise_exc=True))
        r = a.usd_class_transfer(10.0, True)
        self.assertFalse(r["ok"])
        self.assertIn("exchange down", r["error"])


@unittest.skipUnless(HAVE_SDK, "hyperliquid-python-sdk not installed")
class TestFundingHistoryPaging(unittest.TestCase):
    HOUR = 3_600_000

    def _rows(self, n, t0=1_700_000_000_000):
        return [{"coin": "BTC", "fundingRate": str(1e-5 + i * 1e-9),
                 "premium": "0.0", "time": t0 + i * self.HOUR} for i in range(n)]

    def test_pages_past_server_cap_sorted_ascending(self):
        rows = self._rows(1200)                      # 1200 hourly rows, cap 500
        info = _FakeSpotInfo(funding_rows=rows, page_cap=500)
        a = _spot_adapter(info)
        out = a.funding_history("BTC", rows[0]["time"])
        self.assertEqual(len(out), 1200)
        times = [r["time_ms"] for r in out]
        self.assertEqual(times, sorted(times))
        self.assertEqual(times[0], rows[0]["time"])
        self.assertEqual(times[-1], rows[-1]["time"])
        self.assertIsInstance(out[0]["rate"], float)
        self.assertGreaterEqual(len(info.funding_calls), 3)   # actually paged

    def test_end_ms_bounds_window(self):
        rows = self._rows(100)
        end = rows[49]["time"]
        out = _spot_adapter(_FakeSpotInfo(funding_rows=rows)).funding_history(
            "BTC", rows[0]["time"], end)
        self.assertEqual(len(out), 50)
        self.assertEqual(out[-1]["time_ms"], end)

    def test_empty_window_is_empty_list(self):
        out = _spot_adapter(_FakeSpotInfo(funding_rows=[])).funding_history("BTC", 0)
        self.assertEqual(out, [])

    def test_hard_failure_raises_not_empty(self):
        a = _spot_adapter(_FakeSpotInfo(raise_funding=True))
        with self.assertRaises(RuntimeError):
            a.funding_history("BTC", 0)

    def test_duplicate_times_deduped(self):
        rows = self._rows(10) + self._rows(10)       # server echoes a full overlap
        out = _spot_adapter(_FakeSpotInfo(funding_rows=rows)).funding_history(
            "BTC", rows[0]["time"])
        self.assertEqual(len(out), 10)


if __name__ == "__main__":
    unittest.main()
