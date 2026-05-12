"""Tests for backtest.data_collector.fetch_funding_history parsing/pagination.

These never hit the network — the BlofinAPI client is replaced with a stub
that returns canned funding-rate-history pages.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

from backtest.data_collector import fetch_funding_history


class _FakeAPI:
    """Returns funding-history pages newest-first, paginated by `after`."""

    def __init__(self, settlements):
        # settlements: list of (fundingTime_ms, rate) ascending in time.
        self._rows = [
            {"instId": "BTC-USDT", "fundingTime": str(ft), "fundingRate": str(r)}
            for ft, r in settlements
        ]
        self.calls = 0

    def get_funding_rate_history(self, inst_id="BTC-USDT", before=None,
                                 after=None, limit=100):
        self.calls += 1
        rows = sorted(self._rows, key=lambda d: int(d["fundingTime"]),
                      reverse=True)
        if after is not None:
            rows = [d for d in rows if int(d["fundingTime"]) < int(after)]
        page = rows[:limit]
        return {"code": "0", "msg": "success", "data": page}


def _mk_settlements(n, start_ms, step_ms=8 * 3600 * 1000):
    return [(start_ms + i * step_ms, 0.0001 * (1 if i % 2 else -1))
            for i in range(n)]


class FetchFundingHistoryTests(unittest.TestCase):

    def test_single_page_parsing(self):
        import time
        now = int(time.time() * 1000)
        settlements = _mk_settlements(10, now - 9 * 8 * 3600 * 1000)
        api = _FakeAPI(settlements)
        df = fetch_funding_history(api, "BTC-USDT", days=30, page_limit=100)
        self.assertEqual(len(df), 10)
        self.assertListEqual(list(df.columns),
                             ["timestamp", "funding_rate", "funding_time",
                              "funding_interval_hours"])
        # Sorted ascending by time.
        self.assertTrue(df["funding_time"].is_monotonic_increasing)
        # Rates coerced to float.
        self.assertEqual(df["funding_rate"].dtype.kind, "f")
        # 8h interval inferred.
        self.assertAlmostEqual(df["funding_interval_hours"].iloc[0], 8.0)

    def test_pagination_across_pages(self):
        import time
        now = int(time.time() * 1000)
        # 250 settlements -> needs 3 pages at limit=100.
        settlements = _mk_settlements(250, now - 250 * 8 * 3600 * 1000)
        api = _FakeAPI(settlements)
        df = fetch_funding_history(api, "BTC-USDT", days=400, page_limit=100)
        self.assertGreaterEqual(api.calls, 3)
        self.assertEqual(len(df), 250)
        # No duplicate fundingTimes across pages.
        self.assertEqual(df["funding_time"].nunique(), len(df))

    def test_empty_response(self):
        api = _FakeAPI([])
        df = fetch_funding_history(api, "BTC-USDT", days=30)
        self.assertTrue(df.empty)
        self.assertListEqual(list(df.columns),
                             ["timestamp", "funding_rate", "funding_time",
                              "funding_interval_hours"])

    def test_api_error_breaks_cleanly(self):
        class _ErrAPI:
            def get_funding_rate_history(self, **kw):
                return {"code": "1", "msg": "rate limited", "data": []}
        df = fetch_funding_history(_ErrAPI(), "BTC-USDT", days=30)
        self.assertTrue(df.empty)

    def test_window_trims_old_rows(self):
        import time
        now = int(time.time() * 1000)
        # 100 settlements going back ~33 days; ask for only 5 days.
        settlements = _mk_settlements(100, now - 100 * 8 * 3600 * 1000)
        api = _FakeAPI(settlements)
        df = fetch_funding_history(api, "BTC-USDT", days=5, page_limit=100)
        cutoff = now - 5 * 24 * 3600 * 1000
        self.assertTrue((df["funding_time"] >= cutoff).all())
        # 5 days * 3/day ~= 15 rows, far fewer than 100.
        self.assertLess(len(df), 25)


if __name__ == "__main__":
    unittest.main()
