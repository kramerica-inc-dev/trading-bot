"""Tests for the Phase-2 event-driven async runner: nonce-safe concurrent
execution (close→barrier→open, deterministic CLOIDs, flatten-on-failure), the
WS-driven risk monitor (mark-to-market early-warning that only ACCELERATES a
real safety cycle), and the fill-driven wake. Adapter is mocked — no network."""

import asyncio
import sys
import threading
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

try:
    import hl_runner_async as A
    from hl_xs_runner import HLXSRunner
    from event_bus import InProcAsyncBus, FillEvent
    HAVE = True
except Exception:
    HAVE = False


@unittest.skipUnless(HAVE, "async runner / SDK not importable")
class TestConcurrentExecute(unittest.TestCase):
    """The concurrent _execute_live override must keep the exact contract."""

    def _stub(self, positions_before, targets, *, verify_ok=True, leg_sleep=0.05):
        calls = []
        lock = threading.Lock()

        def rec(act, coin):
            with lock:
                calls.append((act, coin))

        class Adapter:
            MIN_ORDER_USD = 10.0

            def positions(self):
                return dict(positions_before)

            def close(self, coin, cloid=None):
                rec("close", coin); time.sleep(leg_sleep)
                return types.SimpleNamespace(ok=True, error=None, filled_sz=0.0)

            def market_order_usd(self, coin, is_buy, usd, slippage=0.02, cloid=None):
                rec(("open_cloid", cloid), coin); time.sleep(leg_sleep)
                return types.SimpleNamespace(ok=True, error=None, filled_sz=usd)

            def all_mids(self):
                return {c: 100.0 for c in targets}

            def book_notional(self):
                return {}

            def make_cloid(self, seed):
                return seed                       # identity → inspectable

        stub = types.SimpleNamespace(
            cfg=types.SimpleNamespace(instance_name="t", slippage=0.02, resize_threshold=0.1),
            mode="MAINNET_LIVE", _exec_concurrency=6,
            _leg_pool=ThreadPoolExecutor(max_workers=6), adapter=Adapter())
        stub._calls = calls
        stub._run_legs = types.MethodType(A.AsyncHLXSRunner._run_legs, stub)
        stub._execute_live = types.MethodType(A.AsyncHLXSRunner._execute_live, stub)
        stub._resize_order = HLXSRunner._resize_order          # pure staticmethod
        stub._verify_book = lambda tgts: (verify_ok, set() if verify_ok else set(tgts), "book")
        stub.flatten_all = lambda: (calls.append(("flatten", None)) or [{"act": "flatten"}])
        return stub

    def test_close_batch_completes_before_open_batch(self):
        stub = self._stub({"BTC": {"szi": 0.1}, "ETH": {"szi": 0.2}},
                          {"SOL": 1000.0, "DOGE": -1000.0})
        from datetime import datetime, timezone
        res = stub._execute_live({"SOL": 1000.0, "DOGE": -1000.0}, datetime.now(timezone.utc))
        kinds = [c[0] if isinstance(c[0], str) else "open" for c in stub._calls]
        last_close = max(i for i, k in enumerate(kinds) if k == "close")
        first_open = min(i for i, k in enumerate(kinds) if k == "open")
        self.assertLess(last_close, first_open, "all closes must precede all opens (barrier)")
        self.assertTrue(res["complete"])
        self.assertEqual(sorted(res["longs"]), ["SOL"])
        self.assertEqual(sorted(res["shorts"]), ["DOGE"])

    def test_deterministic_cloids(self):
        stub = self._stub({}, {"SOL": 1000.0})
        from datetime import datetime, timezone
        now = datetime(2026, 6, 27, tzinfo=timezone.utc)
        stub._execute_live({"SOL": 1000.0}, now)
        open_cloids = [c[0][1] for c in stub._calls if isinstance(c[0], tuple)]
        self.assertEqual(open_cloids, [f"t|{now.isoformat()}|SOL|open"])

    def test_flatten_on_verify_failure(self):
        stub = self._stub({}, {"SOL": 1000.0}, verify_ok=False)
        from datetime import datetime, timezone
        res = stub._execute_live({"SOL": 1000.0}, datetime.now(timezone.utc))
        self.assertFalse(res["complete"])
        self.assertTrue(any(c[0] == "flatten" for c in stub._calls), "must flatten a bad book")

    def test_concurrency_beats_sequential_walltime(self):
        # 4 legs × 0.1s: concurrent ≈ 0.1s, sequential would be ≈ 0.4s.
        stub = self._stub({}, {"A": 1000.0, "B": 1000.0, "C": -1000.0, "D": -1000.0},
                          leg_sleep=0.1)
        from datetime import datetime, timezone
        t0 = time.monotonic()
        stub._execute_live({"A": 1000.0, "B": 1000.0, "C": -1000.0, "D": -1000.0},
                           datetime.now(timezone.utc))
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.3, f"legs did not run concurrently (took {elapsed:.2f}s)")


@unittest.skipUnless(HAVE, "async runner / SDK not importable")
class TestRiskMonitor(unittest.TestCase):
    """The MtM early-warning only wakes a real safety cycle; it never flattens."""

    def _stub(self, book, base_mids, now_mids, *, equity=1000.0,
              halted=False, intracycle=0.08, frac=0.6, cooldown=0.0, last_trigger=0.0):
        stub = types.SimpleNamespace(
            live_trading=True,
            _mtm_baseline={"equity": equity, "book": book, "mids": base_mids},
            cfg=types.SimpleNamespace(catastrophe_intracycle_pct=intracycle),
            _risk_trigger_frac=frac, _risk_cooldown_sec=cooldown, _last_risk_trigger=last_trigger,
            _risk_triggers=0, _last_risk_drop=0.0,
            _ws=types.SimpleNamespace(snapshot=lambda: dict(now_mids)),
            _latest_health={"cb_state": "halted" if halted else "normal"},
            _wake=asyncio.Event(), log=lambda e: None)
        stub._cb_halted = types.MethodType(A.AsyncHLXSRunner._cb_halted, stub)
        stub._eval_risk = types.MethodType(A.AsyncHLXSRunner._eval_risk, stub)
        return stub

    def test_long_book_drop_triggers(self):
        # $1000 long BTC, price 100 -> 92 (-8%) -> est drop 8% > threshold 4.8%
        stub = self._stub({"BTC": 1000.0}, {"BTC": 100.0}, {"BTC": 92.0})
        stub._eval_risk()
        self.assertTrue(stub._wake.is_set())
        self.assertEqual(stub._risk_triggers, 1)

    def test_short_book_adverse_move_triggers(self):
        # $1000 short BTC, price 100 -> 108 (+8%) -> short loses 8% -> trigger
        stub = self._stub({"BTC": -1000.0}, {"BTC": 100.0}, {"BTC": 108.0})
        stub._eval_risk()
        self.assertTrue(stub._wake.is_set())

    def test_small_move_no_trigger(self):
        stub = self._stub({"BTC": 1000.0}, {"BTC": 100.0}, {"BTC": 99.0})   # -1% < 4.8%
        stub._eval_risk()
        self.assertFalse(stub._wake.is_set())
        self.assertEqual(stub._risk_triggers, 0)

    def test_halted_never_triggers(self):
        stub = self._stub({"BTC": 1000.0}, {"BTC": 100.0}, {"BTC": 80.0}, halted=True)
        stub._eval_risk()
        self.assertFalse(stub._wake.is_set())

    def test_cooldown_suppresses_repeat(self):
        stub = self._stub({"BTC": 1000.0}, {"BTC": 100.0}, {"BTC": 90.0},
                          cooldown=999.0, last_trigger=time.time())
        stub._eval_risk()
        self.assertFalse(stub._wake.is_set())     # within cooldown → no re-trigger


@unittest.skipUnless(HAVE, "async runner / SDK not importable")
class TestFillConsumer(unittest.IsolatedAsyncioTestCase):
    async def test_fill_event_counts_and_wakes(self):
        stub = types.SimpleNamespace(_bus=InProcAsyncBus(), _fills_consumed=0,
                                     _last_fill_seen=0.0, _wake=asyncio.Event())
        stub._fill_consumer_task = types.MethodType(A.AsyncHLXSRunner._fill_consumer_task, stub)
        task = asyncio.create_task(stub._fill_consumer_task())
        await asyncio.sleep(0.05)
        stub._bus.publish(FillEvent(ts=1.0, coin="BTC", sz=0.1, px=100.0))
        await asyncio.sleep(0.05)
        task.cancel()
        self.assertEqual(stub._fills_consumed, 1)
        self.assertTrue(stub._wake.is_set())


if __name__ == "__main__":
    unittest.main()
