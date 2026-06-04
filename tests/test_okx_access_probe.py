"""Unit tests for the OKX access-verification probe (M0) pure logic.

Network is never touched: we exercise the order-response classifier, the
perp-permission inference, and the decision-tree synthesizer with stub
dicts. These are the functions the entire candidate-menu branches on, so
they're the high-value cheap check to keep under the lighter regime.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from okx_access_probe import (  # noqa: E402
    _classify_order_response,
    _perp_permitted,
    synthesize,
)


def _ok_order(ord_id="123"):
    return {"code": "0", "msg": "", "data": [{"sCode": "0", "sMsg": "", "ordId": ord_id}]}


def _rejected(top_code="1", top_msg="", s_code="51010", s_msg=""):
    return {"code": top_code, "msg": top_msg,
            "data": [{"sCode": s_code, "sMsg": s_msg, "ordId": ""}]}


class TestClassifyOrderResponse(unittest.TestCase):

    def test_accepted_is_permitted(self):
        c = _classify_order_response(_ok_order("abc"), acct_lv=3)
        self.assertEqual(c["verdict"], "permitted")
        self.assertTrue(c["accepted"])
        self.assertEqual(c["ord_id"], "abc")

    def test_permission_message_is_blocked(self):
        c = _classify_order_response(
            _rejected(s_msg="Operation is not supported under current account mode"),
            acct_lv=1)
        self.assertEqual(c["verdict"], "blocked")
        self.assertFalse(c["accepted"])

    def test_economic_rejection_is_permitted(self):
        # Engine reached, rejected on balance → surface IS tradable.
        c = _classify_order_response(
            _rejected(s_code="51008", s_msg="Insufficient account balance"),
            acct_lv=3)
        self.assertEqual(c["verdict"], "permitted")

    def test_unknown_falls_back_to_acctlv_block_when_simple(self):
        c = _classify_order_response(_rejected(s_msg="weird unmapped error"), acct_lv=1)
        self.assertEqual(c["verdict"], "blocked")

    def test_unknown_stays_unknown_when_acctlv_high(self):
        c = _classify_order_response(_rejected(s_msg="weird unmapped error"), acct_lv=3)
        self.assertEqual(c["verdict"], "unknown")


class TestPerpPermitted(unittest.TestCase):

    def test_confirmed_by_test_order(self):
        res = {"perp_order_test": {"verdict": "permitted"}, "acct_lv": 1}
        self.assertTrue(_perp_permitted(res))  # test order overrides acctLv inference

    def test_blocked_by_test_order(self):
        res = {"perp_order_test": {"verdict": "blocked"}, "acct_lv": 3}
        self.assertFalse(_perp_permitted(res))

    def test_inferred_from_acctlv_when_untested(self):
        self.assertTrue(_perp_permitted({"acct_lv": 2}))
        self.assertFalse(_perp_permitted({"acct_lv": 1}))
        self.assertIsNone(_perp_permitted({"acct_lv": None}))


class TestSynthesize(unittest.TestCase):

    def test_spot_only_branch(self):
        results = {
            "live": {"available": True, "acct_lv": 1},
            "demo": {"available": True, "acct_lv": 1,
                     "perp_order_test": {"verdict": "blocked"}},
        }
        v = synthesize(results)
        self.assertFalse(v["live_perp_enabled"])
        self.assertFalse(v["demo_perp_confirmed"])
        self.assertIn("Spot-only", v["headline"])
        self.assertIn("BLOCKED", v["family_routing"]["B1 cash-and-carry"])

    def test_perp_unlocked_both(self):
        results = {
            "live": {"available": True, "acct_lv": 3},
            "demo": {"available": True, "acct_lv": 3,
                     "perp_order_test": {"verdict": "permitted"}},
        }
        v = synthesize(results)
        self.assertTrue(v["live_perp_enabled"])
        self.assertTrue(v["demo_perp_confirmed"])
        self.assertTrue(v["carry_revivable_live"])
        self.assertIn("demo-paper-OK", v["family_routing"]["B1 cash-and-carry"])
        self.assertNotIn("NOT live-deployable", v["family_routing"]["B2 perp funding-timing"])

    def test_demo_perp_but_live_capped(self):
        results = {
            "live": {"available": True, "acct_lv": 1},
            "demo": {"available": True, "acct_lv": 3,
                     "perp_order_test": {"verdict": "permitted"}},
        }
        v = synthesize(results)
        self.assertTrue(v["demo_perp_confirmed"])
        self.assertFalse(v["carry_revivable_live"])
        self.assertIn("NOT live-deployable", v["family_routing"]["B2 perp funding-timing"])

    def test_inconclusive_without_creds(self):
        v = synthesize({"live": {"available": False}, "demo": {"available": False}})
        self.assertIn("Inconclusive", v["headline"])


if __name__ == "__main__":
    unittest.main()
