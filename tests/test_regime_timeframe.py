#!/usr/bin/env python3
"""Unit tests for regime_timeframe.RegimeTimeframeResolver.

Covers:
- Disabled resolver is a no-op
- Hysteresis: lower urgency regimes require N confirmed bars
- Urgency-up: higher urgency regimes switch immediately
- Streak resets on flipping candidate
- resolve_for_regime() returns per-regime TF/interval independent of state
- Switch history is bounded and records transitions
- Fallback regime is honored at construction

Run standalone:  python -m pytest tests/test_regime_timeframe.py -v
or:              python tests/test_regime_timeframe.py
"""

from __future__ import annotations

import os
import sys
import unittest

# Make scripts/ importable when running as a plain script
HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, "..", "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from regime_timeframe import (  # noqa: E402
    DEFAULT_CHECK_INTERVALS,
    DEFAULT_TIMEFRAMES,
    DEFAULT_URGENCY,
    RegimeTimeframeResolver,
)


def _enabled_config(**overrides):
    cfg = {
        "enabled": True,
        "confirmation_bars": 3,
        "fallback_regime": "unclear",
        "timeframes": dict(DEFAULT_TIMEFRAMES),
        "check_intervals": dict(DEFAULT_CHECK_INTERVALS),
        "urgency": dict(DEFAULT_URGENCY),
    }
    cfg.update(overrides)
    return cfg


class DisabledResolverTests(unittest.TestCase):
    """When enabled=False, the resolver must never mutate active state."""

    def test_disabled_ignores_regime_updates(self):
        r = RegimeTimeframeResolver({"enabled": False})
        initial_tf = r.state.active_timeframe
        initial_regime = r.state.active_regime

        for regime in ["bull_trend", "chop", "range", "bear_trend"]:
            switched, reason = r.update(regime)
            self.assertFalse(switched)
            self.assertIn("disabled", reason)

        self.assertEqual(r.state.active_timeframe, initial_tf)
        self.assertEqual(r.state.active_regime, initial_regime)
        self.assertEqual(r.state.switch_history, [])

    def test_disabled_still_cycles_index(self):
        """Cycle counter should tick regardless, for diagnostics."""
        r = RegimeTimeframeResolver({"enabled": False})
        r.update("bull_trend")
        r.update("bull_trend")
        self.assertEqual(r.state.cycle_index, 2)


class UrgencyUpImmediateSwitchTests(unittest.TestCase):
    """Switching to a higher-urgency regime must happen on the first detection."""

    def test_range_to_chop_is_immediate(self):
        r = RegimeTimeframeResolver(_enabled_config())
        # Bootstrap into 'range'
        for _ in range(3):
            r.update("range")
        self.assertEqual(r.state.active_regime, "range")

        switched, reason = r.update("chop")
        self.assertTrue(switched)
        self.assertIn("urgency_up", reason)
        self.assertEqual(r.state.active_regime, "chop")
        self.assertEqual(r.state.active_timeframe, DEFAULT_TIMEFRAMES["chop"])
        self.assertEqual(r.state.active_check_interval, DEFAULT_CHECK_INTERVALS["chop"])

    def test_unclear_to_bull_is_immediate(self):
        r = RegimeTimeframeResolver(_enabled_config())
        switched, _ = r.update("bull_trend")
        # unclear(0) -> bull_trend(2): urgency up, immediate
        self.assertTrue(switched)
        self.assertEqual(r.state.active_regime, "bull_trend")

    def test_equal_urgency_trend_flip_is_immediate(self):
        """bull_trend (2) -> bear_trend (2): not higher urgency, but
        the resolver treats equal urgency as 'immediate' too, because
        a trend reversal shouldn't wait for hysteresis."""
        r = RegimeTimeframeResolver(_enabled_config())
        for _ in range(3):
            r.update("bull_trend")
        switched, reason = r.update("bear_trend")
        self.assertTrue(switched)
        self.assertEqual(r.state.active_regime, "bear_trend")
        self.assertIn("urgency_up", reason)


class HysteresisDownswitchTests(unittest.TestCase):
    """Switching to a lower-urgency regime requires N confirmed bars."""

    def test_chop_to_range_requires_confirmation(self):
        cfg = _enabled_config(confirmation_bars=3)
        r = RegimeTimeframeResolver(cfg)
        # Force into chop first (higher urgency, immediate)
        r.update("chop")
        self.assertEqual(r.state.active_regime, "chop")

        # First two 'range' detections should NOT switch
        switched, reason = r.update("range")
        self.assertFalse(switched)
        self.assertIn("awaiting_confirmation", reason)
        self.assertEqual(r.state.active_regime, "chop")

        switched, reason = r.update("range")
        self.assertFalse(switched)
        self.assertEqual(r.state.active_regime, "chop")

        # Third detection triggers the switch
        switched, reason = r.update("range")
        self.assertTrue(switched)
        self.assertIn("hysteresis_confirmed", reason)
        self.assertEqual(r.state.active_regime, "range")

    def test_flipping_candidate_resets_streak(self):
        """If the detected regime flips mid-confirmation, the streak must reset."""
        r = RegimeTimeframeResolver(_enabled_config(confirmation_bars=3))
        r.update("chop")  # baseline

        r.update("range")       # streak=1
        r.update("range")       # streak=2
        r.update("unclear")     # interrupts -- streak reset
        r.update("range")       # streak=1 again
        switched, _ = r.update("range")  # streak=2, not enough
        self.assertFalse(switched)
        self.assertEqual(r.state.active_regime, "chop")

        switched, _ = r.update("range")  # streak=3, now switch
        self.assertTrue(switched)
        self.assertEqual(r.state.active_regime, "range")

    def test_confirmation_bars_one_behaves_reactively(self):
        """confirmation_bars=1 should effectively disable hysteresis."""
        r = RegimeTimeframeResolver(_enabled_config(confirmation_bars=1))
        r.update("chop")
        switched, _ = r.update("range")
        self.assertTrue(switched)
        self.assertEqual(r.state.active_regime, "range")


class StateAndHistoryTests(unittest.TestCase):
    def test_resolve_for_regime_is_stateless(self):
        r = RegimeTimeframeResolver(_enabled_config())
        # Active regime is 'unclear' (fallback); query something else
        tf, interval = r.resolve_for_regime("bull_trend")
        self.assertEqual(tf, DEFAULT_TIMEFRAMES["bull_trend"])
        self.assertEqual(interval, DEFAULT_CHECK_INTERVALS["bull_trend"])
        # Active state unchanged
        self.assertEqual(r.state.active_regime, "unclear")

    def test_resolve_for_unknown_regime_falls_back_to_active(self):
        r = RegimeTimeframeResolver(_enabled_config())
        tf, interval = r.resolve_for_regime("nonsense")
        self.assertEqual(tf, r.state.active_timeframe)
        self.assertEqual(interval, r.state.active_check_interval)

    def test_switch_history_bounded(self):
        r = RegimeTimeframeResolver(_enabled_config(history_cap=3))
        # Force a series of up-switches (immediate) so we get multiple
        # history entries without needing hysteresis confirmation.
        # unclear(0) -> range(1) -> bull_trend(2) -> chop(3) are all
        # urgency-up, each is an immediate switch.
        r.update("range")        # switch 1: unclear -> range
        r.update("bull_trend")   # switch 2: range -> bull_trend
        r.update("bear_trend")   # switch 3: equal-urgency trend flip
        r.update("chop")         # switch 4: urgency-up, drops oldest
        self.assertLessEqual(len(r.state.switch_history), 3)
        # Most recent entry should be the last switch
        self.assertEqual(r.state.switch_history[-1]["to_regime"], "chop")

    def test_unknown_regime_normalized_to_unclear(self):
        r = RegimeTimeframeResolver(_enabled_config())
        switched, _ = r.update("totally_made_up")
        # unclear -> unclear: no change
        self.assertFalse(switched)
        self.assertEqual(r.state.candidate_regime, "unclear")

    def test_fallback_regime_honored(self):
        r = RegimeTimeframeResolver(_enabled_config(fallback_regime="range"))
        self.assertEqual(r.state.active_regime, "range")
        self.assertEqual(r.state.active_timeframe, DEFAULT_TIMEFRAMES["range"])

    def test_snapshot_contains_expected_keys(self):
        r = RegimeTimeframeResolver(_enabled_config())
        snap = r.state.snapshot()
        for key in (
            "active_regime", "active_timeframe", "active_check_interval",
            "candidate_regime", "candidate_streak", "cycle_index",
            "last_switch_cycle",
        ):
            self.assertIn(key, snap)


class CustomOverrideTests(unittest.TestCase):
    """User overrides from config must override defaults, not merge incorrectly."""

    def test_custom_timeframe_override(self):
        cfg = _enabled_config(timeframes={
            "bull_trend": "1h",
            "bear_trend": "1h",
            "range": "15m",
            "chop": "4h",
            "unclear": "4h",
        })
        r = RegimeTimeframeResolver(cfg)
        r.update("bull_trend")
        self.assertEqual(r.state.active_timeframe, "1h")

    def test_custom_urgency_override(self):
        # Swap: range now higher-urgency than chop
        cfg = _enabled_config(urgency={
            "chop": 1, "bear_trend": 2, "bull_trend": 2,
            "range": 3, "unclear": 0,
        })
        r = RegimeTimeframeResolver(cfg)
        # Force into chop (urgency 1 in this custom setup)
        r.update("chop")
        # range (urgency 3) should now be an immediate up-switch
        switched, reason = r.update("range")
        self.assertTrue(switched)
        self.assertIn("urgency_up", reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
