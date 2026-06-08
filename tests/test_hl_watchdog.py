"""Tests for the external Hyperliquid runner watchdog (C).

Covers stale-detection (and the low-equity floor), the ALERT-ONLY default (no
auto-flatten), and the opt-in --flatten-on-stale path — with the adapter and the
filesystem mocked, so nothing networked and no real config is touched.
"""

import json
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import hl_watchdog as W  # noqa: E402


def _iso(dt):
    return dt.isoformat()


class TestEvaluate(unittest.TestCase):
    """The pure decision function — no filesystem, no adapter."""

    def setUp(self):
        self.hp = Path(tempfile.mkdtemp()) / "health.json"

    def _now(self):
        return time.time()

    def test_fresh_healthy_no_alerts(self):
        h = {"ts": _iso(datetime.now(timezone.utc)), "equity": 100.0}
        alerts = W.evaluate(h, self.hp, stale_after_sec=900, min_equity_usd=40, now=self._now())
        self.assertEqual(alerts, [])

    def test_stale_ts_alerts(self):
        old = datetime.now(timezone.utc) - timedelta(seconds=1200)
        h = {"ts": _iso(old), "equity": 100.0}
        alerts = W.evaluate(h, self.hp, stale_after_sec=900, min_equity_usd=40, now=self._now())
        self.assertTrue(any(a["kind"] == "stale" for a in alerts))

    def test_missing_health_is_stale(self):
        alerts = W.evaluate(None, self.hp, stale_after_sec=900, min_equity_usd=40)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["kind"], "stale")

    def test_low_equity_alerts(self):
        h = {"ts": _iso(datetime.now(timezone.utc)), "equity": 30.0}
        alerts = W.evaluate(h, self.hp, stale_after_sec=900, min_equity_usd=40, now=self._now())
        self.assertTrue(any(a["kind"] == "low_equity" for a in alerts))
        self.assertFalse(any(a["kind"] == "stale" for a in alerts))

    def test_no_min_equity_means_no_floor_alert(self):
        h = {"ts": _iso(datetime.now(timezone.utc)), "equity": 1.0}
        alerts = W.evaluate(h, self.hp, stale_after_sec=900, min_equity_usd=None, now=self._now())
        self.assertEqual(alerts, [])

    def test_unparseable_ts_falls_back_to_mtime(self):
        # write a real file so mtime is fresh; bad ts must not crash, uses mtime
        hp = Path(tempfile.mkdtemp()) / "health.json"
        hp.write_text(json.dumps({"ts": "not-a-date", "equity": 100.0}))
        h = json.loads(hp.read_text())
        alerts = W.evaluate(h, hp, stale_after_sec=900, min_equity_usd=40)
        self.assertEqual([a for a in alerts if a["kind"] == "stale"], [])


class TestWatchdogAlertOnly(unittest.TestCase):
    """The default: alerts are logged but the book is NEVER flattened."""

    def _wd(self, health, *, flatten=False, min_eq=40.0):
        root = Path(tempfile.mkdtemp())
        d = root / "mainnet"
        d.mkdir(parents=True)
        if health is not None:
            (d / "health.json").write_text(json.dumps(health))
        return W.Watchdog("mainnet", stale_after_sec=900, min_equity_usd=min_eq,
                          flatten_on_stale=flatten, state_root=root)

    def test_stale_logs_event_but_no_flatten(self):
        old = datetime.now(timezone.utc) - timedelta(seconds=2000)
        wd = self._wd({"ts": _iso(old), "equity": 100.0}, flatten=False)
        r = wd.check_once()
        self.assertFalse(r["ok"])
        self.assertTrue(any(a["kind"] == "stale" for a in r["alerts"]))
        self.assertIsNone(r["flatten"])                       # alert-only -> no action
        # the event was appended to the JSONL log
        lines = wd.events_path.read_text().strip().splitlines()
        self.assertTrue(any("stale" in ln for ln in lines))

    def test_healthy_is_ok_no_events(self):
        wd = self._wd({"ts": _iso(datetime.now(timezone.utc)), "equity": 100.0})
        r = wd.check_once()
        self.assertTrue(r["ok"])
        self.assertEqual(r["alerts"], [])
        self.assertFalse(wd.events_path.exists())             # nothing written

    def test_missing_health_alerts_alert_only(self):
        wd = self._wd(None, flatten=False)
        r = wd.check_once()
        self.assertFalse(r["ok"])
        self.assertIsNone(r["flatten"])


class TestWatchdogFlattenOptIn(unittest.TestCase):
    """--flatten-on-stale: only flattens when stale AND positions exist; the
    adapter is mocked so no network/credentials are needed."""

    def _wd(self, health, positions, *, flatten=True):
        root = Path(tempfile.mkdtemp())
        d = root / "mainnet"
        d.mkdir(parents=True)
        if health is not None:
            (d / "health.json").write_text(json.dumps(health))
        wd = W.Watchdog("mainnet", stale_after_sec=900, min_equity_usd=40,
                        flatten_on_stale=flatten, state_root=root)
        closes = []
        remaining = dict(positions)
        def _close(coin):
            closes.append(coin)
            remaining.pop(coin, None)                  # a real close removes the position
            return types.SimpleNamespace(ok=True, error=None)
        fake_adapter = types.SimpleNamespace(
            positions=lambda: dict(remaining), close=_close)
        wd._make_adapter = lambda: fake_adapter
        wd._closed = closes
        return wd

    def test_flatten_verifies_book_is_flat(self):
        old = datetime.now(timezone.utc) - timedelta(seconds=2000)
        wd = self._wd({"ts": _iso(old), "equity": 50.0},
                      positions={"BTC": {"szi": 0.01}, "ETH": {"szi": -0.1}})
        r = wd.check_once()
        self.assertTrue(r["flatten"]["verified_flat"])      # confirmed flat after close

    def test_flatten_when_stale_and_positions(self):
        old = datetime.now(timezone.utc) - timedelta(seconds=2000)
        wd = self._wd({"ts": _iso(old), "equity": 50.0},
                      positions={"BTC": {"szi": 0.01}, "ETH": {"szi": -0.1}})
        r = wd.check_once()
        self.assertIsNotNone(r["flatten"])
        self.assertTrue(r["flatten"]["acted"])
        self.assertEqual(sorted(wd._closed), ["BTC", "ETH"])

    def test_no_flatten_when_stale_but_flat_book(self):
        old = datetime.now(timezone.utc) - timedelta(seconds=2000)
        wd = self._wd({"ts": _iso(old), "equity": 50.0}, positions={})
        r = wd.check_once()
        self.assertIsNotNone(r["flatten"])
        self.assertFalse(r["flatten"]["acted"])               # nothing to close
        self.assertEqual(wd._closed, [])                      # no false-positive flatten

    def test_no_flatten_when_only_low_equity_not_stale(self):
        # low-equity alone (fresh health) is NOT a stale -> never flattens
        wd = self._wd({"ts": _iso(datetime.now(timezone.utc)), "equity": 5.0},
                      positions={"BTC": {"szi": 0.01}})
        r = wd.check_once()
        self.assertTrue(any(a["kind"] == "low_equity" for a in r["alerts"]))
        self.assertIsNone(r["flatten"])                       # only stale triggers flatten
        self.assertEqual(wd._closed, [])

    def test_flatten_disabled_by_default_flag(self):
        old = datetime.now(timezone.utc) - timedelta(seconds=2000)
        wd = self._wd({"ts": _iso(old), "equity": 50.0},
                      positions={"BTC": {"szi": 0.01}}, flatten=False)
        r = wd.check_once()
        self.assertIsNone(r["flatten"])
        self.assertEqual(wd._closed, [])


if __name__ == "__main__":
    unittest.main()
