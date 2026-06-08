"""Unit tests for OkxAdapter / OkxAPI.

Covers symbol normalization, response shape harmonization (vs BloFin),
factory dispatch, and signature math. Network-dependent live probes
are gated by a flag so the suite stays runnable offline.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from okx_adapter import (  # noqa: E402
    OkxAdapter,
    from_okx_symbol,
    to_okx_symbol,
)
from okx_api import OkxAPI  # noqa: E402


class TestSymbolNormalization(unittest.TestCase):

    def test_to_okx_appends_swap(self):
        self.assertEqual(to_okx_symbol("BTC-USDT"), "BTC-USDT-SWAP")
        self.assertEqual(to_okx_symbol("ETH-USDT"), "ETH-USDT-SWAP")

    def test_to_okx_idempotent(self):
        self.assertEqual(to_okx_symbol("BTC-USDT-SWAP"), "BTC-USDT-SWAP")
        self.assertEqual(to_okx_symbol("BTC-USDT-PERP"), "BTC-USDT-PERP")

    def test_from_okx_strips_suffix(self):
        self.assertEqual(from_okx_symbol("BTC-USDT-SWAP"), "BTC-USDT")
        self.assertEqual(from_okx_symbol("ETH-USDT-PERP"), "ETH-USDT")

    def test_from_okx_passes_spot(self):
        self.assertEqual(from_okx_symbol("BTC-USDT"), "BTC-USDT")

    def test_round_trip(self):
        for sym in ("BTC-USDT", "ETH-USDT", "DOGE-USDT", "LINK-USDT"):
            self.assertEqual(from_okx_symbol(to_okx_symbol(sym)), sym)


class TestSignatureAndTimestamp(unittest.TestCase):

    def test_iso_timestamp_format(self):
        ts = OkxAPI._iso_timestamp()
        # Format: 2026-05-03T15:42:01.123Z
        self.assertRegex(ts, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

    def test_signature_is_deterministic(self):
        api = OkxAPI(api_key="k", api_secret="s", passphrase="p")
        sig1 = api._sign("2026-05-03T00:00:00.000Z", "GET",
                         "/api/v5/account/balance", "")
        sig2 = api._sign("2026-05-03T00:00:00.000Z", "GET",
                         "/api/v5/account/balance", "")
        self.assertEqual(sig1, sig2)

    def test_signature_changes_with_inputs(self):
        api = OkxAPI(api_key="k", api_secret="s", passphrase="p")
        s = api._sign("2026-05-03T00:00:00.000Z", "GET", "/a", "")
        self.assertNotEqual(s, api._sign("2026-05-03T00:00:00.001Z", "GET", "/a", ""))
        self.assertNotEqual(s, api._sign("2026-05-03T00:00:00.000Z", "POST", "/a", ""))
        self.assertNotEqual(s, api._sign("2026-05-03T00:00:00.000Z", "GET", "/b", ""))


class TestAdapterCandleNormalization(unittest.TestCase):

    def setUp(self):
        self.adapter = OkxAdapter({})
        self.fake_resp = {
            "code": "0", "msg": "",
            "data": [
                ["1777615200000", "77122.9", "77143.6", "77043.6",
                 "77087", "31553", "31.55", "2432950", "1"],
                ["1777618800000", "77087", "77200", "77000",
                 "77150", "20000", "20.0", "1543000", "1"],
            ],
        }

    def test_candles_returns_data_array(self):
        with patch.object(self.adapter.api, "get_candles",
                          return_value=self.fake_resp) as mock:
            rows = self.adapter.get_candles("BTC-USDT", "1H", limit=2)
            mock.assert_called_once()
            args, kwargs = mock.call_args
            # Adapter must have translated BTC-USDT -> BTC-USDT-SWAP.
            self.assertEqual(args[0], "BTC-USDT-SWAP")
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0][0], "1777615200000")

    def test_candles_passthrough_when_already_list(self):
        with patch.object(self.adapter.api, "get_candles",
                          return_value=self.fake_resp["data"]):
            rows = self.adapter.get_candles("BTC-USDT", "1H")
            self.assertEqual(len(rows), 2)


class TestAdapterBalanceFlattening(unittest.TestCase):

    def test_okx_nested_balance_flattens_to_blofin_shape(self):
        nested = {
            "code": "0", "msg": "",
            "data": [{
                "details": [
                    {"ccy": "USDT", "availBal": "115.56", "frozenBal": "0.00"},
                    {"ccy": "BTC", "availBal": "0.001", "frozenBal": "0"},
                ],
            }],
        }
        adapter = OkxAdapter({})
        with patch.object(adapter.api, "get_balance", return_value=nested):
            r = adapter.get_balance("futures", "USDT")
        self.assertEqual(r["code"], "0")
        self.assertEqual(len(r["data"]), 1)
        self.assertEqual(r["data"][0]["currency"], "USDT")
        self.assertEqual(r["data"][0]["available"], "115.56")
        self.assertEqual(r["data"][0]["frozen"], "0.00")


class TestAdapterPositionsRenormalization(unittest.TestCase):

    def test_positions_instId_translated_back_to_blofin(self):
        adapter = OkxAdapter({})
        okx_resp = {
            "code": "0",
            "data": [
                {"instId": "BTC-USDT-SWAP", "pos": "0.5", "posSide": "long"},
                {"instId": "ETH-USDT-SWAP", "pos": "1.0", "posSide": "short"},
            ],
        }
        with patch.object(adapter.api, "get_positions", return_value=okx_resp):
            r = adapter.get_positions()
        symbols = [d["instId"] for d in r["data"]]
        self.assertEqual(symbols, ["BTC-USDT", "ETH-USDT"])

    def test_positions_filter_translates_input_symbol(self):
        adapter = OkxAdapter({})
        with patch.object(adapter.api, "get_positions",
                          return_value={"code": "0", "data": []}) as mock:
            adapter.get_positions(inst_id="BTC-USDT")
            mock.assert_called_once_with("BTC-USDT-SWAP")


class TestAdapterPlaceOrderTranslation(unittest.TestCase):

    def test_place_order_translates_symbol(self):
        adapter = OkxAdapter({})
        with patch.object(adapter.api, "place_order",
                          return_value={"code": "0"}) as mock:
            adapter.place_order(
                "BTC-USDT", side="buy", order_type="market", size="1",
            )
            kwargs = mock.call_args.kwargs
            self.assertEqual(kwargs["inst_id"], "BTC-USDT-SWAP")
            self.assertEqual(kwargs["side"], "buy")

    def test_cancel_tpsl_translates_each_inst_id(self):
        adapter = OkxAdapter({})
        with patch.object(adapter.api, "cancel_tpsl_orders",
                          return_value={"code": "0"}) as mock:
            adapter.cancel_tpsl_orders([
                {"algoId": "1", "instId": "BTC-USDT"},
                {"algoId": "2", "instId": "ETH-USDT"},
            ])
            payload = mock.call_args.args[0]
            self.assertEqual([p["instId"] for p in payload],
                             ["BTC-USDT-SWAP", "ETH-USDT-SWAP"])


@unittest.skipUnless(
    os.environ.get("OKX_LIVE_PROBE") == "1",
    "set OKX_LIVE_PROBE=1 to hit OKX public endpoints",
)
class TestLivePublicEndpoints(unittest.TestCase):
    """Sanity probes against OKX's real public market data.

    Skipped by default; opt in with OKX_LIVE_PROBE=1 when on a network
    that can reach www.okx.com.
    """

    def test_ticker_btc(self):
        api = OkxAPI()
        r = api.get_ticker("BTC-USDT-SWAP")
        self.assertEqual(r.get("code"), "0", msg=str(r))
        self.assertTrue(r.get("data"))

    def test_candles_returns_rows(self):
        api = OkxAPI()
        r = api.get_candles("BTC-USDT-SWAP", "1H", limit=5)
        self.assertEqual(r.get("code"), "0", msg=str(r))
        rows = r.get("data") or []
        self.assertGreaterEqual(len(rows), 1)
        # Each row has at least 6 columns: ts, o, h, l, c, vol
        self.assertGreaterEqual(len(rows[0]), 6)


if __name__ == "__main__":
    unittest.main()
