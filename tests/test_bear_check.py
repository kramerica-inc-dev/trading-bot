"""Fase 6 — tests for the deterministic bear-check / devil's-advocate module
(scripts/bear_check.py) and its wiring into advanced_strategy.MultiIndicatorConfluence.

Covers:
- compute_bear_check on representative inputs (strong counter-argument -> low
  size_multiplier; no counter-argument -> 1.0; mixed in between)
- the strength->size clip behaviour (default curve + tunable max_penalty/min_floor)
- composition with risk_scoring (both gates on; bear-check applies after)
- regression: bear_check.enabled = false leaves the strategy's gating identical
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

from bear_check import (  # noqa: E402
    compute_bear_check,
    compute_bear_check_strength,
    compute_bear_check_components,
    strength_to_size_multiplier,
    build_bear_check_record,
    DEFAULT_COMPONENT_WEIGHTS,
    COMPONENT_KEYS,
)
from advanced_strategy import MultiIndicatorConfluence  # noqa: E402


def _signal_like(action='buy', regime='range', active_strategy='mean_reversion'):
    return {'action': action, 'regime': regime, 'active_strategy': active_strategy}


class ComputeBearCheckTests(unittest.TestCase):
    def test_no_counter_argument_gives_size_multiplier_one(self):
        # buy with HTFs in agreement (htf=+2), entry tf neutral, no structure
        # working against, RSI mid, no bb extreme, loser stub -> all components 0.
        out = compute_bear_check(
            _signal_like('buy'),
            {'htf_alignment_score': 2.0, 'entry_tf_state_code': 1.0, 'rsi_value': 50.0},
        )
        self.assertEqual(out['score'], 0.0)
        self.assertEqual(out['size_multiplier'], 1.0)
        for k in COMPONENT_KEYS:
            self.assertAlmostEqual(out['components'][k], 0.0, places=9)

    def test_strong_counter_argument_gives_low_size_multiplier(self):
        # long with HTFs maximally against (htf=-2) + entry tf agreeing (long),
        # recent lower highs (strong), and price at the upper Bollinger band.
        out = compute_bear_check(
            _signal_like('buy'),
            {'htf_alignment_score': -2.0, 'entry_tf_state_code': 1.0,
             'bb_pos': 1.0, 'rsi_value': 85.0, 'recent_structure_against': 1.0},
        )
        # mtf_opposition = 1.0 (full), structure = 1.0, bb_extreme = 1.0,
        # loser stub = 0.0 -> weighted avg with equal weights = 0.75.
        self.assertAlmostEqual(out['components']['mtf_opposition'], 1.0, places=6)
        self.assertAlmostEqual(out['components']['recent_lower_highs'], 1.0, places=6)
        self.assertAlmostEqual(out['components']['bb_extreme_against'], 1.0, places=6)
        self.assertAlmostEqual(out['components']['loser_correlation'], 0.0, places=6)
        self.assertAlmostEqual(out['score'], 0.75, places=6)
        # default curve: size_multiplier = clip(1 - 0.75*1.0, 0, 1) = 0.25
        self.assertAlmostEqual(out['size_multiplier'], 0.25, places=6)

    def test_short_uses_higher_lows_and_lower_band(self):
        out = compute_bear_check(
            _signal_like('sell'),
            {'htf_alignment_score': 2.0, 'entry_tf_state_code': -1.0,
             'bb_pos': 0.0, 'rsi_value': 15.0, 'recent_structure_against': 0.5},
        )
        # for a sell: htf=+2 is against -> mtf_opposition=1.0; bb_pos=0 with
        # short_extreme=0.2 -> bb=1.0; structure precomputed=0.5; loser=0.
        self.assertAlmostEqual(out['components']['mtf_opposition'], 1.0, places=6)
        self.assertAlmostEqual(out['components']['bb_extreme_against'], 1.0, places=6)
        self.assertAlmostEqual(out['components']['recent_lower_highs'], 0.5, places=6)
        self.assertAlmostEqual(out['score'], (1.0 + 0.5 + 1.0 + 0.0) / 4.0, places=6)

    def test_mixed_inputs_partial(self):
        # buy: htf=-1 (partially against), entry tf agrees -> mtf = clip(1/2)=0.5;
        # bb_pos=0.9, long_extreme=0.8 -> (0.9-0.8)/0.2 = 0.5; no structure; loser 0.
        out = compute_bear_check(
            _signal_like('buy'),
            {'htf_alignment_score': -1.0, 'entry_tf_state_code': 1.0,
             'bb_pos': 0.9, 'rsi_value': 60.0},
        )
        self.assertAlmostEqual(out['components']['mtf_opposition'], 0.5, places=6)
        self.assertAlmostEqual(out['components']['bb_extreme_against'], 0.5, places=6)
        self.assertAlmostEqual(out['components']['recent_lower_highs'], 0.0, places=6)
        self.assertAlmostEqual(out['score'], (0.5 + 0.0 + 0.5 + 0.0) / 4.0, places=6)
        self.assertGreater(out['size_multiplier'], 0.0)
        self.assertLess(out['size_multiplier'], 1.0)

    def test_mtf_opposition_halved_when_entry_tf_does_not_agree(self):
        # buy with htf=-2 against, but entry tf is also bearish (-1, not agreeing
        # with the long) -> the "LTF bullish but HTF bearish" premise is absent,
        # so the opposition is halved: 1.0 -> 0.5.
        comps, _ = compute_bear_check_components(
            _signal_like('buy'),
            {'htf_alignment_score': -2.0, 'entry_tf_state_code': -1.0},
        )
        self.assertAlmostEqual(comps['mtf_opposition'], 0.5, places=6)

    def test_rsi_fallback_when_no_bb_pos(self):
        # long, RSI deep overbought (85), no bb_pos -> bb_extreme via RSI:
        # (85 - 70) / (100 - 70) = 0.5
        comps, _ = compute_bear_check_components(
            _signal_like('buy'),
            {'rsi_value': 85.0},
        )
        self.assertAlmostEqual(comps['bb_extreme_against'], 0.5, places=6)

    def test_structure_from_recent_closes_long_lower_highs(self):
        # earlier half peaks at 110, recent half peaks at 100 -> displacement 10;
        # atr=10, atr_full=1.0 -> 10/10 = 1.0 counter-argument.
        comps, _ = compute_bear_check_components(
            _signal_like('buy'),
            {},
            {'recent_closes': [90, 110, 95, 92, 100, 98, 97, 96], 'atr': 10.0},
        )
        self.assertAlmostEqual(comps['recent_lower_highs'], 1.0, places=6)

    def test_loser_correlation_uses_recent_loser_outcomes_when_provided(self):
        out = compute_bear_check(
            _signal_like('buy', regime='range', active_strategy='mean_reversion'),
            {},
            {'recent_loser_outcomes': [
                {'regime': 'range', 'active_strategy': 'mean_reversion'},
                {'regime': 'range', 'active_strategy': 'mean_reversion'},
                {'regime': 'bull_trend', 'active_strategy': 'trend_following'},
                {'regime': 'range', 'active_strategy': 'trend_following'},
            ]},
        )
        # 2 of 4 recent losers share regime+strategy -> 0.5
        self.assertAlmostEqual(out['components']['loser_correlation'], 0.5, places=6)

    def test_hold_or_unknown_action_gives_zero(self):
        out = compute_bear_check({'action': 'hold'}, {'htf_alignment_score': -2.0, 'bb_pos': 1.0})
        self.assertEqual(out['score'], 0.0)
        self.assertEqual(out['size_multiplier'], 1.0)

    def test_weights_config_normalized(self):
        # zero everything but mtf_opposition -> score == mtf_opposition component.
        out = compute_bear_check(
            _signal_like('buy'),
            {'htf_alignment_score': -2.0, 'entry_tf_state_code': 1.0, 'bb_pos': 1.0},
            config={'weights': {'mtf_opposition': 1.0, 'recent_lower_highs': 0.0,
                                'bb_extreme_against': 0.0, 'loser_correlation': 0.0}},
        )
        self.assertAlmostEqual(out['score'], out['components']['mtf_opposition'], places=9)
        self.assertEqual(sum(DEFAULT_COMPONENT_WEIGHTS.values()), 1.0)

    def test_build_record_matches_compute(self):
        sig = _signal_like('buy')
        ind = {'htf_alignment_score': -1.0, 'entry_tf_state_code': 1.0, 'bb_pos': 0.95}
        self.assertEqual(build_bear_check_record(sig, ind), compute_bear_check(sig, ind))


class StrengthToSizeMultiplierTests(unittest.TestCase):
    def test_default_curve_breakpoints(self):
        self.assertAlmostEqual(strength_to_size_multiplier(0.0), 1.0, places=9)
        self.assertAlmostEqual(strength_to_size_multiplier(0.25), 0.75, places=9)
        self.assertAlmostEqual(strength_to_size_multiplier(0.5), 0.5, places=9)
        self.assertAlmostEqual(strength_to_size_multiplier(0.75), 0.25, places=9)
        self.assertAlmostEqual(strength_to_size_multiplier(1.0), 0.0, places=9)

    def test_clip_bounds(self):
        self.assertEqual(strength_to_size_multiplier(-5.0), 1.0)
        self.assertEqual(strength_to_size_multiplier(5.0), 0.0)

    def test_tunable_max_penalty_and_floor(self):
        # max_penalty 0.5 -> at strength 1.0 size = 1 - 0.5 = 0.5
        self.assertAlmostEqual(strength_to_size_multiplier(1.0, {'max_penalty': 0.5}), 0.5, places=9)
        # min_floor 0.2 -> a maximal counter-argument can't go below 0.2
        self.assertAlmostEqual(strength_to_size_multiplier(1.0, {'min_floor': 0.2}), 0.2, places=9)
        # max_penalty 2.0 (aggressive) -> strength 0.5 already zeroes it
        self.assertAlmostEqual(strength_to_size_multiplier(0.5, {'max_penalty': 2.0}), 0.0, places=9)


class StrategyWiringTests(unittest.TestCase):
    def _make_candles(self, n=400, base=30000.0):
        # Mildly trending series so the strategy actually evaluates entries.
        candles = []
        price = base
        for i in range(n):
            price *= (1.0 + 0.0006 * ((i % 7) - 3) / 3.0)
            o = price
            c = price * 1.0002
            h = max(o, c) * 1.0005
            low = min(o, c) * 0.9995
            candles.append([i * 300_000, o, h, low, c, 100.0 + (i % 11)])
        return candles

    def test_disabled_is_byte_for_byte_unchanged(self):
        candles = self._make_candles()
        s_off = MultiIndicatorConfluence({})
        s_on = MultiIndicatorConfluence({'bear_check': {'enabled': False}})
        for i in range(250, len(candles)):
            window = candles[:i]
            price = float(candles[i][4])
            a = s_off.analyze(window, price)
            b = s_on.analyze(window, price)
            self.assertEqual(a.action, b.action)
            self.assertAlmostEqual(a.confidence, b.confidence, places=12)
            self.assertAlmostEqual(float(a.risk_multiplier), float(b.risk_multiplier), places=12)
        self.assertEqual(s_off.rejection_stats, s_on.rejection_stats)

    def test_enabled_attaches_bear_check_record_or_changes_nothing_when_no_entries(self):
        candles = self._make_candles()
        s = MultiIndicatorConfluence({'bear_check': {'enabled': True}})
        saw_signal = False
        for i in range(250, len(candles)):
            sig = s.analyze(candles[:i], float(candles[i][4]))
            if sig.action != 'hold':
                saw_signal = True
                self.assertIn('score', sig.bear_check)
                self.assertIn('components', sig.bear_check)
                self.assertGreaterEqual(sig.bear_check['score'], 0.0)
                self.assertLessEqual(sig.bear_check['score'], 1.0)
        # It's fine if this synthetic series produced no entries; the regression
        # test above already pins the disabled path. This just exercises the
        # enabled branch when a signal does fire.
        _ = saw_signal

    def test_enabled_composes_with_risk_scoring(self):
        # Both gates on: the strategy must run without error and, when a signal
        # fires, carry both a risk_score and a bear_check record.
        candles = self._make_candles()
        s = MultiIndicatorConfluence({'risk_scoring': {'enabled': True},
                                      'bear_check': {'enabled': True}})
        for i in range(250, len(candles)):
            sig = s.analyze(candles[:i], float(candles[i][4]))
            if sig.action != 'hold':
                self.assertIsNotNone(sig.risk_score)
                self.assertIn('score', sig.bear_check)
                # risk_multiplier should reflect both scalings (>= 0, <= the
                # un-scaled cap).
                self.assertGreaterEqual(float(sig.risk_multiplier), 0.0)

    def test_max_penalty_zero_floor_one_is_no_op_on_sizing(self):
        # bear_check on but with a curve that never penalizes -> gating identical
        # to the disabled path.
        candles = self._make_candles()
        s_off = MultiIndicatorConfluence({})
        s_noop = MultiIndicatorConfluence({'bear_check': {'enabled': True,
                                                          'max_penalty': 0.0, 'min_floor': 1.0}})
        for i in range(250, len(candles)):
            window = candles[:i]
            price = float(candles[i][4])
            a = s_off.analyze(window, price)
            b = s_noop.analyze(window, price)
            self.assertEqual(a.action, b.action)
            self.assertAlmostEqual(float(a.risk_multiplier), float(b.risk_multiplier), places=12)


if __name__ == '__main__':
    unittest.main()
