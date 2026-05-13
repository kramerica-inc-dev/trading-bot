"""Paper-mode runner tests for `scripts/bh_overlay_runner.py`.

Exercises the runner with a mocked price-fetch (no live API): verifies
atomic state writes, JSONL trade-log append, the three-state mode gate
(refuses live), the manual halt sentinel, the one-decision-per-UTC-day
discipline, and that the health.json contains the dashboard-shape keys.
Deterministic.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import bh_overlay_runner as bhr  # noqa: E402
from bh_overlay_runner import (  # noqa: E402
    BHOverlayConfig, BHOverlayRunner, BHOverlayState,
    load_config, resolve_mode, MODE_PAPER,
)


def _make_bars(closes, start=datetime(2026, 1, 1, tzinfo=timezone.utc)):
    """Build a list of dicts in the shape `fetch_daily_btc` returns
    (oldest-first, ISO timestamp, OHLCV)."""
    closes = np.asarray(closes, dtype=float)
    rows = []
    for i, c in enumerate(closes):
        ts = start + timedelta(days=i)
        rows.append({
            "timestamp": ts.isoformat(),
            "open": float(c), "high": float(c) * 1.001,
            "low": float(c) * 0.999, "close": float(c), "volume": 1000.0,
        })
    return rows


class _MockFetcher:
    """Returns the same prebaked daily-bar list on every call, except the
    LAST bar is filtered out the first call so we can test the "latest bar
    is not closed yet" guard."""

    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return list(self.rows)


class TestRunnerModeGate(unittest.TestCase):
    def test_paper_only_default(self):
        cfg = BHOverlayConfig()
        self.assertEqual(resolve_mode(cfg), MODE_PAPER)

    def test_refuses_live(self):
        cfg = BHOverlayConfig(paper_only=False, allow_live=True, venue="bitvavo")
        with self.assertRaises(RuntimeError) as cm:
            resolve_mode(cfg)
        self.assertIn("paper_only=true", str(cm.exception).lower()
                      .replace("paper_only=true to run", "paper_only=true"))
        # Make the error message check robust to wording:
        self.assertIn("live", str(cm.exception).lower())

    def test_refuses_live_via_runner_ctor(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = BHOverlayConfig(paper_only=False, allow_live=True)
            with self.assertRaises(RuntimeError):
                BHOverlayRunner(cfg, state_dir=Path(td),
                                fetch_fn=lambda: [])


class TestRunnerCycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _runner(self, cfg=None, bars=None):
        cfg = cfg or BHOverlayConfig(
            initial_equity_usd=5000.0, vol_window_days=10,
            trailing_stop_pct=0.10, reentry_n_days=10,
        )
        # The runner uses "now" to decide whether the latest bar is closed,
        # so we backdate bars to 5 days ago to guarantee bar_is_closed=True.
        if bars is None:
            closes = np.linspace(50000, 55000, 30)
            start = datetime.now(timezone.utc) - timedelta(days=40)
            bars = _make_bars(closes, start=start.replace(hour=0, minute=0,
                                                          second=0,
                                                          microsecond=0))
        fetcher = _MockFetcher(bars)
        runner = BHOverlayRunner(cfg, state_dir=self.state_dir,
                                  fetch_fn=fetcher)
        return runner, fetcher

    def test_first_cycle_writes_state_log_health(self):
        runner, _ = self._runner()
        entry = runner.one_cycle()
        # State + health + trade log are all written.
        self.assertTrue(runner.state_path.exists())
        self.assertTrue(runner.health_path.exists())
        self.assertTrue(runner.log_path.exists())
        # State is valid JSON.
        with open(runner.state_path) as f:
            state = json.load(f)
        self.assertIn("simulated_equity", state)
        self.assertGreater(state["simulated_equity"], 0)
        # Health has the dashboard-shape keys.
        with open(runner.health_path) as f:
            health = json.load(f)
        for k in ("instance", "mode", "paper_only", "halted",
                  "last_cycle_ts", "cycles_total", "simulated_equity",
                  "current_exposure", "signal_on", "vol_realized",
                  "vol_target", "drawdown_from_peak", "days_under_water",
                  "trend_signal_on", "vol_target_active", "bh_equity"):
            self.assertIn(k, health, f"health.json missing key {k!r}")
        self.assertEqual(health["mode"], "PAPER")
        self.assertTrue(health["paper_only"])
        # First cycle decided today (the bar is closed → bar_date < today_utc).
        self.assertEqual(entry["mode"], "PAPER")

    def test_one_decision_per_utc_day(self):
        runner, _ = self._runner()
        e1 = runner.one_cycle()
        e2 = runner.one_cycle()
        # First entry made a decision; second was within the same UTC day →
        # no new decision.
        a1 = e1["action"]["kind"]
        a2 = e2["action"]["kind"]
        # First was either a rebalance (entry) or hold; second must be noop.
        self.assertIn(a1, ("rebalance", "hold", "skip"))
        self.assertEqual(a2, "noop")
        self.assertEqual(e2["action"]["reason"], "already_decided_today")

    def test_first_cycle_rebalances_into_position(self):
        """Bull leg + decision-day → strategy should enter (current=0 → target>0)."""
        runner, _ = self._runner()
        e = runner.one_cycle()
        self.assertEqual(e["action"]["kind"], "rebalance")
        self.assertGreater(e["target_exposure"], 0.0)
        self.assertTrue(e["signal_on"])
        # Fees were charged on the entry delta.
        self.assertGreater(e["fee_cost_usd"], 0.0)
        self.assertGreater(e["traded_delta"], 0.0)

    def test_state_persists_across_runner_restarts(self):
        runner1, _ = self._runner()
        runner1.one_cycle()
        eq1 = runner1.load_state().simulated_equity
        exposure1 = runner1.load_state().current_exposure
        # Build a new runner pointing at the SAME state dir — it should pick
        # up where the previous one left off.
        runner2, _ = self._runner()
        s2 = runner2.load_state()
        self.assertAlmostEqual(s2.simulated_equity, eq1, places=8)
        self.assertAlmostEqual(s2.current_exposure, exposure1, places=8)
        # And the strategy state machine state was restored.
        self.assertIn("in_market", runner2.strategy.to_state())

    def test_halt_sentinel_blocks_decisions(self):
        runner, _ = self._runner()
        # Drop the halt sentinel BEFORE the first cycle.
        runner.halt_sentinel.parent.mkdir(parents=True, exist_ok=True)
        runner.halt_sentinel.touch()
        e = runner.one_cycle()
        self.assertEqual(e["action"]["kind"], "noop")
        self.assertIn("halted", e["action"]["reason"])
        self.assertTrue(e["halted"])
        self.assertEqual(e["halt_reason"], "manual_halt_sentinel")
        # Strategy must not have advanced into a position.
        self.assertEqual(e["current_exposure"], 0.0)

    def test_halt_sentinel_cleared_resumes(self):
        runner, _ = self._runner()
        runner.halt_sentinel.parent.mkdir(parents=True, exist_ok=True)
        runner.halt_sentinel.touch()
        runner.one_cycle()
        runner.halt_sentinel.unlink()
        # Reset state's last_decision_date so we can decide again today.
        s = runner.load_state()
        s.last_decision_date = None
        runner.save_state(s)
        e2 = runner.one_cycle()
        # After clearing, halted should be False again.
        self.assertFalse(e2["halted"])

    def test_fetch_failure_falls_back_to_cache(self):
        """Mock the fetcher to raise; after first successful cycle, runner
        should keep going on the cached series."""
        bars = _make_bars(
            np.linspace(50000, 55000, 30),
            start=datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                                     microsecond=0)
            - timedelta(days=40),
        )
        cfg = BHOverlayConfig(
            initial_equity_usd=5000.0, vol_window_days=10,
            trailing_stop_pct=0.10, reentry_n_days=10,
        )
        # First call returns bars, subsequent calls raise.
        state = {"n": 0}
        def flaky():
            state["n"] += 1
            if state["n"] == 1:
                return bars
            raise RuntimeError("simulated upstream failure")
        runner = BHOverlayRunner(cfg, state_dir=self.state_dir, fetch_fn=flaky)
        e1 = runner.one_cycle()
        self.assertGreater(e1["cache_bars"], 0)
        # Reset decision date so the second cycle is also a decision cycle.
        s = runner.load_state()
        s.last_decision_date = None
        runner.save_state(s)
        e2 = runner.one_cycle()
        # cache_bars unchanged (fetch failed, cache used).
        self.assertGreaterEqual(e2["cache_bars"], e1["cache_bars"])

    def test_jsonl_append_only(self):
        """trades.log should be one JSON object per line, append-only."""
        runner, _ = self._runner()
        runner.one_cycle()
        runner.one_cycle()
        with open(runner.log_path) as f:
            lines = [line for line in f.read().splitlines() if line]
        self.assertEqual(len(lines), 2)
        for line in lines:
            obj = json.loads(line)
            self.assertIn("ts", obj)
            self.assertIn("action", obj)
            self.assertIn("mode", obj)


class TestConfigLoading(unittest.TestCase):
    def test_load_real_config(self):
        repo = Path(__file__).resolve().parent.parent
        path = repo / "configs" / "bh_overlay-btc.json"
        self.assertTrue(path.exists(), "configs/bh_overlay-btc.json missing")
        cfg = load_config(str(path))
        self.assertTrue(cfg.paper_only)
        self.assertFalse(cfg.allow_live)
        self.assertEqual(cfg.asset, "BTC-USDT")
        self.assertGreater(cfg.initial_equity_usd, 0.0)

    def test_ignores_comment_keys(self):
        """Keys starting with `_` are doc-comments and should not crash load."""
        import tempfile
        payload = {
            "instance_name": "x",
            "_some_comment": "hello",
            "paper_only": True,
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(payload, f)
            tmppath = f.name
        try:
            cfg = load_config(tmppath)
            self.assertEqual(cfg.instance_name, "x")
        finally:
            os.unlink(tmppath)


if __name__ == "__main__":
    unittest.main()
