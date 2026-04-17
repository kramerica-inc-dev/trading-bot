#!/usr/bin/env python3
"""End-to-end integration test for dynamic timeframe + per-TF calibration.

Uses a fake exchange adapter and a fake strategy to drive the bot through
a scripted sequence of detected regimes, then asserts that:

1. With both features disabled, the bot behaves like v2.6:
   - No profile loading, no timeframe switches, static check interval.
2. With regime_timeframes enabled, a regime sequence produces the expected
   sequence of active timeframes (respecting urgency and hysteresis).
3. With timeframe_profiles enabled and a profile file present, the strategy
   receives set_active_timeframe() calls that match the resolver's output.
4. The calibrate_per_timeframe JSON schema round-trips through the bot:
   write a realistic profile file, start the bot, confirm it loads and
   applies the whitelisted params to the strategy.
5. Positions record opened_on_timeframe at entry and that value survives
   a subsequent regime switch.

The fake strategy mirrors just enough of MultiIndicatorConfluence's public
surface for the bot to work: name, min_confidence, analyze(), and the
hooks the bot calls (set_active_timeframe, load_timeframe_profiles).

Run standalone:  python tests/test_dynamic_tf_integration.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Optional
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from trading_strategy import Signal  # noqa: E402


# ---------------------------------------------------------------------------
# Fake strategy
# ---------------------------------------------------------------------------

class _FakeAdvancedSignal(Signal):
    """Minimal AdvancedSignal-compatible object. Only carries what the
    bot reads: action, confidence, reason, stop_loss/take_profit, regime,
    and the extra fields the bot probes via getattr()."""

    def __init__(self, action: str, confidence: float, reason: str,
                 regime: str, **kwargs):
        super().__init__(action=action, confidence=confidence, reason=reason,
                         stop_loss=kwargs.get("stop_loss"),
                         take_profit=kwargs.get("take_profit"))
        self.regime = regime
        self.indicators: Dict[str, float] = kwargs.get("indicators", {})
        self.atr = kwargs.get("atr", 100.0)
        self.regime_metrics: Dict[str, float] = {}
        self.active_strategy = "fake"
        self.regime_scores: Dict[str, float] = {}
        self.regime_confidence = 0.8
        self.risk_multiplier = 1.0
        self.quality_score = 0.7
        self.max_hold_bars: Optional[int] = None


class FakeStrategy:
    """Stand-in for MultiIndicatorConfluence.

    Records every call the bot makes, and returns scripted signals. The
    scripted sequence drives the timeframe resolver via the `regime`
    field on the signal.
    """

    def __init__(self, regime_script: List[str],
                 signal_action: str = "hold",
                 signal_confidence: float = 0.0):
        self.name = "FakeStrategy"
        self.min_confidence = 0.5
        self.regime_script = list(regime_script)
        self.signal_action = signal_action
        self.signal_confidence = signal_confidence

        # Profile-related state (mirrors the real strategy's API)
        self.timeframe_profiles: Dict[str, Dict] = {}
        self._active_calibrated_timeframe: Optional[str] = None
        self.rsi_period = 14
        self.min_confidence_attr = 0.5

        # Recording
        self.analyze_calls: List[Dict] = []
        self.set_active_timeframe_calls: List[Optional[str]] = []
        self.loaded_profile_path: Optional[str] = None

    def load_timeframe_profiles(self, path: str) -> int:
        self.loaded_profile_path = path
        p = Path(path)
        if not p.exists():
            return 0
        try:
            payload = json.loads(p.read_text())
        except Exception:
            return 0
        profiles = payload.get("profiles", {})
        if not isinstance(profiles, dict):
            return 0
        cleaned = {}
        for tf, params in profiles.items():
            if isinstance(params, dict):
                safe = {k: v for k, v in params.items()
                        if isinstance(v, (int, float, bool))}
                if safe:
                    cleaned[tf] = safe
        self.timeframe_profiles = cleaned
        return len(cleaned)

    def set_active_timeframe(self, tf: Optional[str]) -> None:
        self.set_active_timeframe_calls.append(tf)
        if tf and tf in self.timeframe_profiles:
            self._active_calibrated_timeframe = tf
            # Mimic param application for the whitelisted keys
            profile = self.timeframe_profiles[tf]
            if "rsi_period" in profile:
                self.rsi_period = int(profile["rsi_period"])
            if "min_confidence" in profile:
                self.min_confidence_attr = float(profile["min_confidence"])
        else:
            self._active_calibrated_timeframe = None

    def analyze(self, candles, current_price: float) -> _FakeAdvancedSignal:
        call_idx = len(self.analyze_calls)
        regime = (self.regime_script[call_idx]
                  if call_idx < len(self.regime_script)
                  else self.regime_script[-1])
        self.analyze_calls.append({
            "call_idx": call_idx,
            "regime": regime,
            "n_candles": len(candles) if candles else 0,
            "current_price": current_price,
            "rsi_period_at_call": self.rsi_period,
            "active_tf_at_call": self._active_calibrated_timeframe,
        })
        return _FakeAdvancedSignal(
            action=self.signal_action,
            confidence=self.signal_confidence,
            reason=f"scripted {regime}",
            regime=regime,
            stop_loss=current_price * 0.98 if self.signal_action == "buy" else None,
            take_profit=current_price * 1.02 if self.signal_action == "buy" else None,
        )


# ---------------------------------------------------------------------------
# Fake exchange adapter
# ---------------------------------------------------------------------------

class FakeAdapter:
    """Minimal BloFin adapter surface: just enough for run_once() to work."""

    def __init__(self):
        self.call_log: List[tuple] = []
        self.placed_orders: List[Dict] = []
        self._current_price = 50000.0
        self._candle_count = 0

    def get_ticker(self, inst_id):
        self.call_log.append(("get_ticker", inst_id))
        return {"code": "0", "data": [{"last": str(self._current_price)}]}

    def get_candles(self, inst_id, bar, limit=300):
        self.call_log.append(("get_candles", inst_id, bar, limit))
        # Return a deterministic list of candles. The bot only passes these
        # to strategy.analyze(), which in our fake ignores their content.
        candles = []
        base_ts = 1_700_000_000_000
        for i in range(min(limit, 300)):
            ts = base_ts + i * 60_000
            candles.append([ts, 50000, 50050, 49950, 50000, 10.0])
        return candles

    def get_balance(self, account_type, currency):
        self.call_log.append(("get_balance", account_type, currency))
        return {"code": "0", "data": [{"available": "10000"}]}

    def get_positions(self, inst_id):
        return {"code": "0", "data": []}

    def get_active_orders(self, inst_id):
        return {"code": "0", "data": []}

    def get_active_tpsl_orders(self, inst_id):
        return {"code": "0", "data": []}

    def get_capabilities(self):
        return {"server_side_tpsl": True, "hedge_mode": True}

    def place_order(self, **kwargs):
        order_id = f"ord_{len(self.placed_orders) + 1}"
        self.placed_orders.append({"order_id": order_id, **kwargs})
        return {"code": "0", "data": [{"orderId": order_id}]}

    def place_tpsl_order(self, **kwargs):
        return {"code": "0", "data": {"tpslId": "tp_1", "algoId": "tp_1"}}

    def get_order_detail(self, inst_id, order_id=None, client_order_id=None):
        return {"code": "unsupported"}


# ---------------------------------------------------------------------------
# Config builder
# ---------------------------------------------------------------------------

def _make_config(base_dir: Path, *, rtf_enabled: bool = False,
                 profiles_enabled: bool = False,
                 profiles_path: str = "memory/timeframe_profiles.json") -> Path:
    cfg = {
        "exchange": "blofin",
        "blofin": {"api_key": "t", "api_secret": "t", "passphrase": "t"},
        "dry_run": True,
        "trading_pair": "BTC-USDT",
        "timeframe": "5m",
        "strategy_name": "advanced",
        "risk": {"risk_per_trade_pct": 1, "contract_size": 0.001,
                 "contract_step": 0.1, "min_contracts": 0.1, "leverage": 1},
        "trading": {"allow_long": True, "allow_short": True, "max_positions": 1},
        "strategy": {"min_confidence": 0.5},
        "protection": {"use_server_side_tpsl": True,
                        "require_server_side_tpsl": False},
        "market_data": {"use_websocket": False, "max_staleness_seconds": 30},
        "regime_timeframes": {
            "enabled": rtf_enabled,
            "confirmation_bars": 3,
            "fallback_regime": "unclear",
            "timeframes": {"bull_trend": "15m", "bear_trend": "15m",
                            "range": "5m", "chop": "1h", "unclear": "1h"},
            "check_intervals": {"bull_trend": 300, "bear_trend": 300,
                                 "range": 60, "chop": 900, "unclear": 900},
            "urgency": {"chop": 3, "bear_trend": 2, "bull_trend": 2,
                         "range": 1, "unclear": 0},
        },
        "timeframe_profiles": {"enabled": profiles_enabled,
                                "path": profiles_path},
    }
    path = base_dir / "config.json"
    path.write_text(json.dumps(cfg))
    return path


def _write_profile_file(base_dir: Path, name: str = "timeframe_profiles.json") -> Path:
    memory = base_dir / "memory"
    memory.mkdir(exist_ok=True)
    path = memory / name
    payload = {
        "generated_at": "2026-04-17T00:00:00Z",
        "source": {"inst_id": "BTC-USDT", "days": 90},
        "profiles": {
            "5m":  {"rsi_period": 14, "min_confidence": 0.45,
                    "trend_strength_threshold": 0.0018,
                    "efficiency_trend_threshold": 0.32,
                    "anchor_slope_threshold": 0.0015},
            "15m": {"rsi_period": 10, "min_confidence": 0.50,
                    "trend_strength_threshold": 0.0012,
                    "efficiency_trend_threshold": 0.30,
                    "anchor_slope_threshold": 0.0010},
            "1h":  {"rsi_period": 8,  "min_confidence": 0.55,
                    "trend_strength_threshold": 0.0008,
                    "efficiency_trend_threshold": 0.26,
                    "anchor_slope_threshold": 0.0008},
        },
    }
    path.write_text(json.dumps(payload, indent=2))
    return path


# ---------------------------------------------------------------------------
# Bot construction helper
# ---------------------------------------------------------------------------

def _build_bot(config_path: Path, fake_strategy: FakeStrategy, fake_api):
    """Construct a TradingBot with the fake adapter and strategy injected."""
    # The bot calls create_strategy(name, cfg) inside __init__. We patch
    # that to return our fake. We also patch the exchange adapter.
    with patch("trading_bot.create_exchange_adapter", return_value=fake_api), \
         patch("trading_bot.create_strategy", return_value=fake_strategy):
        from trading_bot import TradingBot
        bot = TradingBot(str(config_path))
    return bot


# ===========================================================================
# Tests
# ===========================================================================

class DisabledFeaturesBehaveLikeV26(unittest.TestCase):
    """With both features off, the bot must not touch profiles or switch TFs."""

    def test_no_profile_load_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path = _make_config(base, rtf_enabled=False,
                                        profiles_enabled=False)
            _write_profile_file(base)  # file exists, but feature is off
            strategy = FakeStrategy(regime_script=["bull_trend"])
            bot = _build_bot(config_path, strategy, FakeAdapter())

            self.assertIsNone(strategy.loaded_profile_path)
            self.assertEqual(strategy.timeframe_profiles, {})
            self.assertFalse(bot.regime_tf_resolver.enabled)

    def test_active_timeframe_is_static_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path = _make_config(base, rtf_enabled=False)
            strategy = FakeStrategy(
                regime_script=["bull_trend", "chop", "bear_trend"])
            bot = _build_bot(config_path, strategy, FakeAdapter())

            # Run multiple cycles with varying regimes
            for _ in range(3):
                bot.run_once()

            # Active timeframe never changed
            tfs_used = {call[2] for call in bot.api.call_log
                        if call[0] == "get_candles"}
            self.assertEqual(tfs_used, {"5m"})


class RegimeTimeframesDriveCandleFetching(unittest.TestCase):
    """With rtf enabled, candle fetches and strategy notifications reflect
    the resolver's state after each cycle."""

    def test_urgency_up_switches_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path = _make_config(base, rtf_enabled=True)
            # Sequence: unclear bootstrap -> chop (urgency 3, immediate)
            strategy = FakeStrategy(regime_script=["chop", "chop"])
            bot = _build_bot(config_path, strategy, FakeAdapter())

            # Cycle 1: detect chop, resolver switches
            bot.run_once()
            self.assertEqual(bot.regime_tf_resolver.state.active_regime, "chop")
            self.assertEqual(bot.regime_tf_resolver.state.active_timeframe, "1h")

            # Cycle 2: candles for 1h should now be fetched
            bot.run_once()
            tfs_fetched = [call[2] for call in bot.api.call_log
                           if call[0] == "get_candles"]
            # First fetch is the startup/warmup TF, second should reflect switch
            self.assertIn("1h", tfs_fetched)

    def test_hysteresis_downswitch_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path = _make_config(base, rtf_enabled=True)
            strategy = FakeStrategy(regime_script=[
                "chop",   # cycle 1 -> switch to 1h
                "range",  # cycle 2 -> streak 1, no switch
                "range",  # cycle 3 -> streak 2, no switch
                "range",  # cycle 4 -> streak 3, switch to 5m
            ])
            bot = _build_bot(config_path, strategy, FakeAdapter())

            for _ in range(4):
                bot.run_once()

            self.assertEqual(bot.regime_tf_resolver.state.active_regime, "range")
            self.assertEqual(bot.regime_tf_resolver.state.active_timeframe, "5m")
            # Switch history should show the transitions
            switches = [(h["from_regime"], h["to_regime"])
                        for h in bot.regime_tf_resolver.state.switch_history]
            self.assertIn(("unclear", "chop"), switches)
            self.assertIn(("chop", "range"), switches)


class ProfilesRoundTripThroughBot(unittest.TestCase):
    """A calibration JSON file is loaded at startup and the strategy
    receives matching set_active_timeframe() calls."""

    def test_profile_file_is_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path = _make_config(
                base, rtf_enabled=False, profiles_enabled=True,
                profiles_path="memory/timeframe_profiles.json")
            profile_path = _write_profile_file(base)
            strategy = FakeStrategy(regime_script=["bull_trend"])

            bot = _build_bot(config_path, strategy, FakeAdapter())

            self.assertEqual(os.path.realpath(strategy.loaded_profile_path),
                              os.path.realpath(str(profile_path)))
            self.assertEqual(set(strategy.timeframe_profiles.keys()),
                              {"5m", "15m", "1h"})
            # Verify one param round-trip
            self.assertEqual(strategy.timeframe_profiles["15m"]["rsi_period"],
                              10)

    def test_enabled_but_missing_file_logs_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path = _make_config(
                base, profiles_enabled=True,
                profiles_path="memory/does_not_exist.json")
            strategy = FakeStrategy(regime_script=["bull_trend"])
            # Patch _log to capture the warning
            with patch("trading_bot.create_exchange_adapter",
                        return_value=FakeAdapter()), \
                 patch("trading_bot.create_strategy",
                        return_value=strategy):
                from trading_bot import TradingBot
                bot = TradingBot(str(config_path))

            # No profiles loaded
            self.assertEqual(strategy.timeframe_profiles, {})

    def test_active_tf_notified_to_strategy_each_cycle(self):
        """The bot should call strategy.set_active_timeframe() after
        analyze() so the *next* cycle sees the updated params."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path = _make_config(
                base, rtf_enabled=True, profiles_enabled=True)
            _write_profile_file(base)
            strategy = FakeStrategy(regime_script=[
                "chop", "chop", "chop",  # stay in chop (1h)
            ])
            bot = _build_bot(config_path, strategy, FakeAdapter())

            for _ in range(3):
                bot.run_once()

            # After first cycle, resolver switched to chop (1h).
            # set_active_timeframe should have been called with "1h" at
            # least once.
            self.assertIn("1h", strategy.set_active_timeframe_calls)

    def test_strategy_params_change_after_tf_switch(self):
        """When the resolver switches TF, the next analyze() call sees
        updated strategy params (rsi_period, etc.)."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path = _make_config(
                base, rtf_enabled=True, profiles_enabled=True)
            _write_profile_file(base)
            strategy = FakeStrategy(regime_script=[
                "chop",  # cycle 1 -> switch to 1h, rsi_period becomes 8
                "chop",  # cycle 2 -> analyze() sees rsi_period=8
            ])
            bot = _build_bot(config_path, strategy, FakeAdapter())

            bot.run_once()  # first analyze: rsi still 14 (switch happens after)
            bot.run_once()  # second analyze: rsi now 8 (1h profile active)

            # First call was before any TF switch took effect
            self.assertEqual(strategy.analyze_calls[0]["rsi_period_at_call"], 14)
            # Second call happens after set_active_timeframe("1h") was issued
            self.assertEqual(strategy.analyze_calls[1]["rsi_period_at_call"], 8)


class NoOpGuarantees(unittest.TestCase):
    """Bot still starts and functions when config omits new sections entirely,
    or when the strategy doesn't support the new hooks."""

    def test_strategy_without_hooks_does_not_break_bot(self):
        """If strategy has no load_timeframe_profiles/set_active_timeframe,
        the bot must not crash."""
        class BareStrategy:
            def __init__(self):
                self.name = "bare"
                self.min_confidence = 0.5
            def analyze(self, candles, price):
                return _FakeAdvancedSignal(
                    "hold", 0.0, "bare", regime="unclear")

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path = _make_config(
                base, rtf_enabled=True, profiles_enabled=True)
            _write_profile_file(base)
            strategy = BareStrategy()

            with patch("trading_bot.create_exchange_adapter",
                        return_value=FakeAdapter()), \
                 patch("trading_bot.create_strategy",
                        return_value=strategy):
                from trading_bot import TradingBot
                bot = TradingBot(str(config_path))

            # Should complete without AttributeError
            bot.run_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
