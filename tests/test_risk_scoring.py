"""Fase 5 — tests for the continuous weighted risk score (scripts/risk_scoring.py)
and its wiring into advanced_strategy.MultiIndicatorConfluence.

Covers:
- compute_risk_score on representative inputs (all-high -> ~1.0, all-low -> ~0.0, mixed)
- the score->size clip behaviour at the 0.25 / 0.5 / 0.75 breakpoints
- regression: risk_scoring.enabled = false leaves the strategy's gating identical
"""

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

from risk_scoring import (  # noqa: E402
    compute_risk_score,
    compute_risk_score_components,
    score_to_size_multiplier,
    DEFAULT_QUALITY_MIN,
    DEFAULT_REGIME_CONFIDENCE_MIN,
)
from advanced_strategy import MultiIndicatorConfluence  # noqa: E402


def _signal_like(action='buy', confidence=0.8, quality_score=0.8, regime_confidence=0.8):
    return {
        'action': action,
        'confidence': confidence,
        'quality_score': quality_score,
        'regime_confidence': regime_confidence,
    }


class ComputeRiskScoreTests(unittest.TestCase):
    def test_all_high_inputs_score_near_one(self):
        sig = _signal_like(confidence=1.0, quality_score=1.0, regime_confidence=1.0)
        # htf_alignment_score = +2 for a buy => mtf component = 1.0
        score = compute_risk_score(sig, {'htf_alignment_score': 2.0})
        self.assertGreaterEqual(score, 0.99)
        self.assertLessEqual(score, 1.0)

    def test_all_low_inputs_score_near_zero(self):
        # confidence 0, quality at/below its floor, regime conf at/below its floor,
        # htf alignment -2 against a buy => mtf component = 0.0
        sig = _signal_like(confidence=0.0,
                           quality_score=DEFAULT_QUALITY_MIN,
                           regime_confidence=DEFAULT_REGIME_CONFIDENCE_MIN)
        score = compute_risk_score(sig, {'htf_alignment_score': -2.0})
        self.assertLessEqual(score, 0.01)
        self.assertGreaterEqual(score, 0.0)

    def test_mixed_inputs_score_in_between(self):
        sig = _signal_like(confidence=0.7, quality_score=0.7, regime_confidence=0.6)
        score = compute_risk_score(sig, {'htf_alignment_score': 0.0})  # mtf = 0.5
        self.assertGreater(score, 0.2)
        self.assertLess(score, 0.95)

    def test_equal_weights_are_the_default(self):
        # With all four normalized components equal to c, the score must equal c.
        sig = _signal_like(confidence=0.5, quality_score=1.0, regime_confidence=1.0)
        # normalized: conf=0.5, quality=1.0, regime=1.0; pick htf so mtf alignment
        # is also some value, then assert weighted-average == mean of components.
        comps, _ = compute_risk_score_components(sig, {'htf_alignment_score': 0.0})
        expected = sum(comps.values()) / 4.0
        self.assertAlmostEqual(compute_risk_score(sig, {'htf_alignment_score': 0.0}),
                               expected, places=9)

    def test_side_sign_flips_mtf_alignment(self):
        ctx = {'htf_alignment_score': 2.0}
        comps_buy, _ = compute_risk_score_components(_signal_like(action='buy'), ctx)
        comps_sell, _ = compute_risk_score_components(_signal_like(action='sell'), ctx)
        self.assertAlmostEqual(comps_buy['mtf_alignment'], 1.0, places=9)
        self.assertAlmostEqual(comps_sell['mtf_alignment'], 0.0, places=9)

    def test_works_with_object_signal(self):
        class _S:
            action = 'buy'
            confidence = 1.0
            quality_score = 1.0
            regime_confidence = 1.0
        self.assertGreaterEqual(compute_risk_score(_S(), {'htf_alignment_score': 2.0}), 0.99)

    def test_config_weights_normalized(self):
        sig = _signal_like(confidence=1.0, quality_score=0.0, regime_confidence=0.0)
        # weight only confidence => score == confidence component == 1.0
        cfg = {'weights': {'confidence': 1.0, 'quality_score': 0.0,
                           'regime_confidence': 0.0, 'mtf_alignment': 0.0}}
        self.assertAlmostEqual(compute_risk_score(sig, {'htf_alignment_score': -2.0}, cfg),
                               1.0, places=9)


class ScoreToSizeTests(unittest.TestCase):
    def test_breakpoint_quarter_is_zero(self):
        self.assertAlmostEqual(score_to_size_multiplier(0.25), 0.0, places=9)

    def test_below_quarter_clips_to_zero(self):
        self.assertEqual(score_to_size_multiplier(0.1), 0.0)
        self.assertEqual(score_to_size_multiplier(0.0), 0.0)

    def test_breakpoint_half_is_half(self):
        self.assertAlmostEqual(score_to_size_multiplier(0.5), 0.5, places=9)

    def test_breakpoint_three_quarters_is_one(self):
        self.assertAlmostEqual(score_to_size_multiplier(0.75), 1.0, places=9)

    def test_above_three_quarters_clips_to_one(self):
        self.assertEqual(score_to_size_multiplier(0.9), 1.0)
        self.assertEqual(score_to_size_multiplier(1.0), 1.0)

    def test_slope_intercept_tunable(self):
        cfg = {'size_slope': 1.0, 'size_intercept': 0.0}
        self.assertAlmostEqual(score_to_size_multiplier(0.4, cfg), 0.4, places=9)
        self.assertEqual(score_to_size_multiplier(-1.0, cfg), 0.0)


class _CandleMixin:
    def _make_candles(self, n=400, base_price=50000.0, volatility=0.006, seed=7):
        random.seed(seed)
        candles = []
        price = base_price
        ts = 1700000000000
        for i in range(n):
            open_p = price
            change = random.gauss(0, volatility * price)
            close_p = max(open_p + change, 1.0)
            high_p = max(open_p, close_p) * (1 + random.uniform(0, volatility))
            low_p = min(open_p, close_p) * (1 - random.uniform(0, volatility))
            vol = random.uniform(100, 1000)
            candles.append([str(ts), str(open_p), str(high_p),
                            str(low_p), str(close_p), str(vol)])
            price = close_p
            ts += 300000
        return candles


class RiskScoringDisabledIsRegressionTests(_CandleMixin, unittest.TestCase):
    """risk_scoring.enabled = false (the default) must produce byte-for-byte
    identical strategy output / gating to the legacy behaviour."""

    def _signals(self, config):
        strat = MultiIndicatorConfluence(config)
        candles = self._make_candles()
        out = []
        for i in range(120, len(candles)):
            window = candles[:i]
            sig = strat.analyze(window, float(candles[i - 1][4]))
            out.append((sig.action, round(float(sig.confidence), 10),
                        round(float(getattr(sig, 'risk_multiplier', 0.0) or 0.0), 10),
                        getattr(sig, 'regime', None)))
        return out, dict(strat.rejection_stats)

    def test_default_config_matches_explicit_disabled(self):
        sigs_default, rej_default = self._signals({'multi_timeframe': {'enabled': False}})
        sigs_disabled, rej_disabled = self._signals({
            'multi_timeframe': {'enabled': False},
            'risk_scoring': {'enabled': False},
        })
        self.assertEqual(sigs_default, sigs_disabled)
        self.assertEqual(rej_default, rej_disabled)

    def test_disabled_never_sets_risk_score(self):
        strat = MultiIndicatorConfluence({
            'multi_timeframe': {'enabled': False},
            'risk_scoring': {'enabled': False},
        })
        candles = self._make_candles()
        for i in range(120, len(candles)):
            sig = strat.analyze(candles[:i], float(candles[i - 1][4]))
            self.assertIsNone(getattr(sig, 'risk_score', None))


class RiskScoringEnabledTests(_CandleMixin, unittest.TestCase):
    def test_enabled_attaches_risk_score_to_entry_signals(self):
        strat = MultiIndicatorConfluence({
            'multi_timeframe': {
                'enabled': False,
                'require_15m_confirmation': False,
                'require_1h_alignment': False,
                'require_4h_alignment': False,
            },
            'risk_scoring': {'enabled': True},
            # widen the world so at least something tries to enter
            'trade_quality': {'min_score': 0.0, 'min_regime_confidence': 0.0},
        })
        candles = self._make_candles()
        seen_entry = False
        for i in range(120, len(candles)):
            sig = strat.analyze(candles[:i], float(candles[i - 1][4]))
            if sig.action != 'hold':
                seen_entry = True
                self.assertIsNotNone(sig.risk_score)
                self.assertGreaterEqual(sig.risk_score, 0.0)
                self.assertLessEqual(sig.risk_score, 1.0)
                self.assertTrue(set(sig.risk_score_components) >= {
                    'confidence', 'quality_score', 'regime_confidence', 'mtf_alignment'})
                # size multiplier > 0 since it was not filtered
                self.assertGreater(float(sig.risk_multiplier), 0.0)
        # not strictly required, but the synthetic series should yield >=1 entry
        # when gates are loosened; if not, the test above is vacuous — assert it.
        self.assertTrue(seen_entry, "expected at least one entry signal with loosened gates")


if __name__ == '__main__':
    unittest.main()
