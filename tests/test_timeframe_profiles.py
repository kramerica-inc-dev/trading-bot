#!/usr/bin/env python3
"""Tests for per-timeframe parameter profiles (step 5, v2.7).

Covers:
- load_timeframe_profiles reads a real JSON file and returns a count
- set_active_timeframe applies profile params on the next analyze() cycle
- Unknown timeframe is a no-op (base params stay)
- Regime override beats TF override on conflict (regime > TF > base)
- Bad JSON / missing file is silently handled

Run standalone:  python tests/test_timeframe_profiles.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, "..", "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from advanced_strategy import MultiIndicatorConfluence  # noqa: E402


def _minimal_strategy_config(**overrides):
    cfg = {
        "rsi_period": 14,
        "trend_strength_threshold": 0.0018,
        "efficiency_trend_threshold": 0.32,
        "min_confidence": 0.45,
        "anchor_slope_threshold": 0.0015,
        "min_votes": 3,
    }
    cfg.update(overrides)
    return cfg


class LoadTimeframeProfilesTests(unittest.TestCase):
    """Profiles can be loaded from the JSON file produced by calibration."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        self.path = self.tmp.name

    def tearDown(self):
        os.unlink(self.path)

    def _write(self, payload):
        self.tmp.write(json.dumps(payload))
        self.tmp.close()

    def test_loads_profiles_from_valid_file(self):
        self._write({
            "generated_at": "2026-04-17T00:00:00Z",
            "profiles": {
                "5m":  {"rsi_period": 14, "min_confidence": 0.45},
                "15m": {"rsi_period": 10, "min_confidence": 0.48},
                "1h":  {"rsi_period": 8,  "min_confidence": 0.55},
            },
        })
        s = MultiIndicatorConfluence(_minimal_strategy_config())
        n = s.load_timeframe_profiles(self.path)
        self.assertEqual(n, 3)
        self.assertIn("5m", s.timeframe_profiles)
        self.assertIn("15m", s.timeframe_profiles)

    def test_missing_file_returns_zero_and_does_not_raise(self):
        s = MultiIndicatorConfluence(_minimal_strategy_config())
        n = s.load_timeframe_profiles("/nonexistent/path/does/not/exist.json")
        self.assertEqual(n, 0)
        self.assertEqual(s.timeframe_profiles, {})

    def test_malformed_json_returns_zero(self):
        self.tmp.write("{ this is not valid json")
        self.tmp.close()
        s = MultiIndicatorConfluence(_minimal_strategy_config())
        n = s.load_timeframe_profiles(self.path)
        self.assertEqual(n, 0)

    def test_non_numeric_params_are_filtered(self):
        self._write({"profiles": {
            "5m": {"rsi_period": 14, "bad_param": "not_a_number",
                   "min_confidence": 0.45},
        }})
        s = MultiIndicatorConfluence(_minimal_strategy_config())
        s.load_timeframe_profiles(self.path)
        self.assertIn("rsi_period", s.timeframe_profiles["5m"])
        self.assertNotIn("bad_param", s.timeframe_profiles["5m"])

    def test_empty_profiles_key(self):
        self._write({"profiles": {}})
        s = MultiIndicatorConfluence(_minimal_strategy_config())
        n = s.load_timeframe_profiles(self.path)
        self.assertEqual(n, 0)


class SetActiveTimeframeTests(unittest.TestCase):
    """set_active_timeframe + _apply_timeframe_profile change strategy params."""

    def test_switching_to_15m_applies_15m_params(self):
        s = MultiIndicatorConfluence(_minimal_strategy_config(
            rsi_period=14, min_confidence=0.45))
        s.timeframe_profiles = {
            "5m":  {"rsi_period": 14, "min_confidence": 0.45},
            "15m": {"rsi_period": 10, "min_confidence": 0.55},
        }
        s.set_active_timeframe("15m")
        s._apply_timeframe_profile()
        self.assertEqual(s.rsi_period, 10)
        self.assertAlmostEqual(s.min_confidence, 0.55)

    def test_unknown_timeframe_is_noop(self):
        s = MultiIndicatorConfluence(_minimal_strategy_config(
            rsi_period=14, min_confidence=0.45))
        s.timeframe_profiles = {
            "5m": {"rsi_period": 14, "min_confidence": 0.45},
        }
        s.set_active_timeframe("4h")   # not in profiles
        s._apply_timeframe_profile()
        # Base params unchanged
        self.assertEqual(s.rsi_period, 14)
        self.assertAlmostEqual(s.min_confidence, 0.45)
        self.assertIsNone(s._active_calibrated_timeframe)

    def test_none_timeframe_clears_active(self):
        s = MultiIndicatorConfluence(_minimal_strategy_config())
        s.timeframe_profiles = {"5m": {"rsi_period": 20}}
        s.set_active_timeframe("5m")
        self.assertEqual(s._active_calibrated_timeframe, "5m")
        s.set_active_timeframe(None)
        self.assertIsNone(s._active_calibrated_timeframe)

    def test_all_five_whitelisted_params_apply(self):
        s = MultiIndicatorConfluence(_minimal_strategy_config())
        s.timeframe_profiles = {"1h": {
            "rsi_period": 8,
            "trend_strength_threshold": 0.0008,
            "efficiency_trend_threshold": 0.26,
            "min_confidence": 0.55,
            "anchor_slope_threshold": 0.0008,
        }}
        s.set_active_timeframe("1h")
        s._apply_timeframe_profile()
        self.assertEqual(s.rsi_period, 8)
        self.assertAlmostEqual(s.trend_strength_threshold, 0.0008)
        self.assertAlmostEqual(s.efficiency_trend_threshold, 0.26)
        self.assertAlmostEqual(s.min_confidence, 0.55)
        self.assertAlmostEqual(s.anchor_slope_threshold, 0.0008)

    def test_unknown_params_in_profile_are_ignored(self):
        """The whitelist must not let random params mutate the strategy."""
        s = MultiIndicatorConfluence(_minimal_strategy_config())
        s.timeframe_profiles = {"15m": {
            "rsi_period": 10,
            "some_random_attribute": "malicious_value",
            "macd_fast": 99,  # not in whitelist
        }}
        s.set_active_timeframe("15m")
        s._apply_timeframe_profile()
        self.assertEqual(s.rsi_period, 10)
        # macd_fast should stay at default (12) — not overridden
        self.assertEqual(s.macd_fast, 12)
        self.assertFalse(hasattr(s, "some_random_attribute"))


class ProfilePrecedenceTests(unittest.TestCase):
    """When both a TF profile and a regime live-profile change the same
    param, the regime-level override must win (regime > TF > base).
    """

    def test_regime_profile_overrides_tf_profile_on_conflict(self):
        s = MultiIndicatorConfluence(_minimal_strategy_config(
            rsi_period=14, min_confidence=0.45))

        # TF profile for 15m wants min_confidence=0.50
        s.timeframe_profiles = {"15m": {"min_confidence": 0.50}}
        # Regime live-profile for bull_trend wants min_confidence=0.60
        s.regime_live_profiles = {"bull_trend": {"min_confidence": 0.60}}

        s.set_active_timeframe("15m")
        # _apply_live_profile first calls _apply_timeframe_profile
        # (sets 0.50), then regime override (sets 0.60)
        s._apply_live_profile("bull_trend")
        self.assertAlmostEqual(s.min_confidence, 0.60)

    def test_tf_profile_applies_when_no_regime_override(self):
        s = MultiIndicatorConfluence(_minimal_strategy_config(
            rsi_period=14, min_confidence=0.45))
        s.timeframe_profiles = {"15m": {"min_confidence": 0.50}}
        s.regime_live_profiles = {}  # no regime overrides

        s.set_active_timeframe("15m")
        s._apply_live_profile("bull_trend")
        # No regime override, so TF profile value wins
        self.assertAlmostEqual(s.min_confidence, 0.50)

    def test_base_applies_when_neither_present(self):
        s = MultiIndicatorConfluence(_minimal_strategy_config(
            rsi_period=14, min_confidence=0.45))
        s.timeframe_profiles = {}
        s.regime_live_profiles = {}

        s.set_active_timeframe("15m")  # not in profiles, no-op
        s._apply_live_profile("bull_trend")
        self.assertAlmostEqual(s.min_confidence, 0.45)


if __name__ == "__main__":
    unittest.main(verbosity=2)
