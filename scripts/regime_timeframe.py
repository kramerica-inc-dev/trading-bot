#!/usr/bin/env python3
"""Dynamic timeframe selection based on market regime (v2.6, step 3).

This module resolves which base timeframe and check interval the bot should
use, based on the current market regime detected by the strategy.

Design:
- Hybrid switch logic: regimes have an "urgency" score. Switching to a
  higher-urgency regime (e.g. chop) happens immediately; switching to a
  lower-urgency regime (e.g. range) requires N confirmed bars.
- Running positions are NOT affected. They keep the timeframe they were
  opened on via position["opened_on_timeframe"], and their exit logic
  (time_exit, trailing_stop) uses that stored value.
- Only new entries are evaluated against the currently active timeframe.

Urgency defaults (higher = more urgent):
    chop        = 3   # defensive: always switch immediately
    bear_trend  = 2   # trending, act on it
    bull_trend  = 2
    range       = 1   # requires hysteresis confirmation from trend
    unclear     = 0   # always wait, never rush

If the feature is disabled in config, resolve_active_timeframe() simply
returns the bot's static global timeframe, so the bot behaves exactly as
before.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple


DEFAULT_URGENCY = {
    "chop": 3,
    "bear_trend": 2,
    "bull_trend": 2,
    "range": 1,
    "unclear": 0,
}

DEFAULT_TIMEFRAMES = {
    "bull_trend": "15m",
    "bear_trend": "15m",
    "range": "5m",
    "chop": "1h",
    "unclear": "1h",
}

DEFAULT_CHECK_INTERVALS = {
    # Seconds between main-loop cycles. Trending regimes need less frequent
    # checks; range needs more frequent; chop needs the least.
    "bull_trend": 300,   # 5 min
    "bear_trend": 300,
    "range": 60,
    "chop": 900,         # 15 min
    "unclear": 900,
}

SUPPORTED_TIMEFRAMES = {"1m", "5m", "15m", "1h", "4h", "1d"}


@dataclass
class RegimeTimeframeState:
    """Mutable state tracked across bot cycles.

    Keeps a rolling count of how long the *detected* regime has been
    stable, so the resolver can decide whether to switch down (hysteresis)
    or up (immediate).

    Attributes:
        active_regime: Regime currently driving timeframe/interval selection.
        active_timeframe: Timeframe resolved from active_regime.
        active_check_interval: Check interval resolved from active_regime.
        candidate_regime: Most recently detected regime (may differ from active).
        candidate_streak: How many consecutive cycles candidate_regime was detected.
        last_switch_cycle: Cycle index of the most recent switch (for logging).
        cycle_index: Monotonic counter of resolver invocations.
    """
    active_regime: str = "unclear"
    active_timeframe: str = "5m"
    active_check_interval: int = 60
    candidate_regime: str = "unclear"
    candidate_streak: int = 0
    last_switch_cycle: int = 0
    cycle_index: int = 0
    switch_history: list = field(default_factory=list)

    def snapshot(self) -> Dict:
        """Return a JSON-serializable snapshot for logging/diagnostics."""
        return {
            "active_regime": self.active_regime,
            "active_timeframe": self.active_timeframe,
            "active_check_interval": self.active_check_interval,
            "candidate_regime": self.candidate_regime,
            "candidate_streak": self.candidate_streak,
            "cycle_index": self.cycle_index,
            "last_switch_cycle": self.last_switch_cycle,
        }


class RegimeTimeframeResolver:
    """Resolves active timeframe and check interval from detected regime.

    The resolver is stateful: it tracks how long a new regime has been
    detected and applies hysteresis before switching down in urgency.
    Higher-urgency regimes are adopted immediately (asymmetric hysteresis).

    Example:
        resolver = RegimeTimeframeResolver(config["regime_timeframes"])
        switched, reason = resolver.update(detected_regime="bull_trend")
        tf = resolver.state.active_timeframe           # "15m"
        interval = resolver.state.active_check_interval  # 300
    """

    def __init__(self, config: Optional[Dict] = None) -> None:
        config = dict(config or {})
        self.enabled: bool = bool(config.get("enabled", False))
        self.confirmation_bars: int = max(
            int(config.get("confirmation_bars", 3)), 1)

        self.timeframes: Dict[str, str] = dict(DEFAULT_TIMEFRAMES)
        self.timeframes.update(config.get("timeframes", {}) or {})

        self.check_intervals: Dict[str, int] = dict(DEFAULT_CHECK_INTERVALS)
        self.check_intervals.update(config.get("check_intervals", {}) or {})

        self.urgency: Dict[str, int] = dict(DEFAULT_URGENCY)
        self.urgency.update(config.get("urgency", {}) or {})

        fallback_regime = str(config.get("fallback_regime", "unclear"))
        fallback_tf = self.timeframes.get(fallback_regime, "5m")
        fallback_interval = int(self.check_intervals.get(fallback_regime, 60))

        self.state = RegimeTimeframeState(
            active_regime=fallback_regime,
            active_timeframe=fallback_tf,
            active_check_interval=fallback_interval,
            candidate_regime=fallback_regime,
        )
        self._history_cap: int = int(config.get("history_cap", 50))

    # ------------------------------ public API ------------------------------

    def update(self, detected_regime: str) -> Tuple[bool, str]:
        """Evaluate a newly detected regime and decide whether to switch.

        Args:
            detected_regime: The regime returned by strategy.detect_market_regime().

        Returns:
            (switched, reason)
            switched: True if active_regime changed in this call.
            reason:   Human-readable explanation (for logs).
        """
        self.state.cycle_index += 1

        if not self.enabled:
            # Feature off: no-op. State stays at fallback.
            return False, "dynamic_timeframe_disabled"

        detected = str(detected_regime or "unclear")
        if detected not in self.urgency:
            detected = "unclear"

        # Track streak of detected regime
        if detected == self.state.candidate_regime:
            self.state.candidate_streak += 1
        else:
            self.state.candidate_regime = detected
            self.state.candidate_streak = 1

        # Already in this regime -> nothing to do
        if detected == self.state.active_regime:
            return False, f"no_change ({detected} sustained)"

        current_urgency = self.urgency.get(self.state.active_regime, 0)
        new_urgency = self.urgency.get(detected, 0)

        # Asymmetric hysteresis:
        # - switch up in urgency (or equal urgency for trend flips): immediate
        # - switch down in urgency: require N confirmed bars
        if new_urgency >= current_urgency:
            return self._perform_switch(
                detected,
                f"urgency_up ({self.state.active_regime}->{detected}, "
                f"{current_urgency}->{new_urgency})")

        if self.state.candidate_streak >= self.confirmation_bars:
            return self._perform_switch(
                detected,
                f"hysteresis_confirmed ({detected} for "
                f"{self.state.candidate_streak} bars)")

        return False, (
            f"awaiting_confirmation ({detected} streak "
            f"{self.state.candidate_streak}/{self.confirmation_bars})")

    # ----------------------------- introspection ----------------------------

    def resolve_for_regime(self, regime: str) -> Tuple[str, int]:
        """Look up the timeframe and check interval for a given regime.

        Useful for exit-path code that needs the timeframe a position was
        opened on, independent of the currently active regime.
        """
        tf = self.timeframes.get(regime, self.state.active_timeframe)
        interval = int(
            self.check_intervals.get(regime, self.state.active_check_interval))
        return tf, interval

    # ------------------------------ internals -------------------------------

    def _perform_switch(self, new_regime: str, reason: str) -> Tuple[bool, str]:
        old_regime = self.state.active_regime
        new_tf = self.timeframes.get(new_regime, self.state.active_timeframe)
        new_interval = int(
            self.check_intervals.get(new_regime, self.state.active_check_interval))

        self.state.active_regime = new_regime
        self.state.active_timeframe = new_tf
        self.state.active_check_interval = new_interval
        self.state.last_switch_cycle = self.state.cycle_index

        # Bounded history for diagnostics
        self.state.switch_history.append({
            "cycle": self.state.cycle_index,
            "from_regime": old_regime,
            "to_regime": new_regime,
            "timeframe": new_tf,
            "check_interval": new_interval,
            "reason": reason,
        })
        if len(self.state.switch_history) > self._history_cap:
            self.state.switch_history = self.state.switch_history[-self._history_cap:]

        return True, reason
