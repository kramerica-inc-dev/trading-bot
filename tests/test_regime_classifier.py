"""Unit tests for the Plan E regime classifier (Phase 1).

Covers feature math, target labeling, walk-forward fold construction,
binarization threshold reuse, and classifier save/load round-trip.

Runner-integration tests (compute_regime_tilt fires/skips, missing
model fails open, NaN feature path) are deferred to the integration
PR that lands the runner-side gate — the classifier module itself
has no runner dependency.
"""

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.regime_classifier_e import (
    FEATURE_NAMES,
    RegimeClassifierE,
    binarize_to_loss_tail,
    compute_basket_forward_return,
    compute_features,
)
from backtest.regime_classifier_walk_forward import build_folds


def _hourly_index(n_hours: int, start: str = "2025-01-01") -> pd.DatetimeIndex:
    return pd.date_range(start=start, periods=n_hours, freq="1h", tz="UTC")


def _const_universe(n_hours: int, prices: dict[str, float]) -> pd.DataFrame:
    idx = _hourly_index(n_hours)
    return pd.DataFrame(
        {sym: np.full(n_hours, px) for sym, px in prices.items()},
        index=idx,
    )


class TestFeatures(unittest.TestCase):
    """Each feature is exercised against a hand-rolled fixture."""

    def setUp(self):
        # 1000 hours = enough for 30d (720) MA window + 72h lookback warmup.
        self.n = 1000
        self.idx = _hourly_index(self.n)

    def test_breadth_pos_72h(self):
        """Half the universe up over 72h, half flat → breadth = 0.5."""
        # 4 assets: 2 ramping up, 2 with flat-then-down 72h block.
        ramp = np.linspace(100.0, 110.0, self.n)        # always positive 72h ret
        decay = np.linspace(110.0, 100.0, self.n)       # always negative 72h ret
        closes = pd.DataFrame({
            "BTC-USDT": ramp,
            "ETH-USDT": ramp,
            "SOL-USDT": decay,
            "XRP-USDT": decay,
        }, index=self.idx)
        feat = compute_features(closes)
        # By bar ~80 the warmup is past; check a stable region.
        self.assertAlmostEqual(feat["breadth_pos_72h"].iloc[800], 0.5, places=6)

    def test_breadth_above_sma200(self):
        """All assets monotonically rising → all above SMA-200 → breadth = 1.0."""
        rng = np.linspace(100.0, 200.0, self.n)
        closes = pd.DataFrame({
            "BTC-USDT": rng, "ETH-USDT": rng,
            "SOL-USDT": rng, "XRP-USDT": rng,
        }, index=self.idx)
        feat = compute_features(closes)
        # After the SMA warmup (200 bars), close > sma everywhere.
        sample = feat["breadth_above_sma200"].iloc[400:].dropna()
        self.assertTrue((sample == 1.0).all(), "all rising → fully above SMA200")

    def test_xs_dispersion_72h_matches_numpy(self):
        """xs_dispersion = pandas .std(axis=1) of 72h log returns."""
        rng = np.random.default_rng(42)
        closes = pd.DataFrame(
            100.0 + rng.standard_normal((self.n, 4)).cumsum(axis=0),
            index=self.idx,
            columns=["BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT"],
        )
        feat = compute_features(closes)
        log_ret = np.log(closes / closes.shift(72))
        expected = log_ret.std(axis=1)
        # Sample a few mid-range timestamps; warmup region is NaN on both sides.
        for i in [200, 500, 900]:
            self.assertAlmostEqual(
                feat["xs_dispersion_72h"].iloc[i],
                expected.iloc[i],
                places=12,
                msg=f"xs_dispersion mismatch at i={i}",
            )

    def test_btc_vol_ratio_recent_over_long(self):
        """High recent vol vs calm long-window → ratio > 1."""
        # Long stretch of low-amplitude moves, then the last 24h get large.
        rng = np.random.default_rng(0)
        log_rets = rng.standard_normal(self.n) * 0.001       # low daily vol
        log_rets[-24:] *= 10                                  # vol spike
        closes = pd.DataFrame({
            "BTC-USDT": 100.0 * np.exp(np.cumsum(log_rets)),
            "ETH-USDT": np.full(self.n, 50.0),               # placeholders
            "SOL-USDT": np.full(self.n, 30.0),
        }, index=self.idx)
        feat = compute_features(closes)
        ratio = feat["btc_vol_ratio_24_720"].iloc[-1]
        self.assertGreater(ratio, 1.5, f"vol-spike not reflected: ratio={ratio}")

    def test_btc_trend_strength_zero_when_btc_flat(self):
        """Flat BTC → 72h log return = 0 → trend strength = 0."""
        flat = np.full(self.n, 100.0)
        # Add tiny noise on other assets so dispersion math still works.
        rng = np.random.default_rng(7)
        wobble = 50.0 + rng.standard_normal(self.n) * 0.01
        closes = pd.DataFrame({
            "BTC-USDT": flat,
            "ETH-USDT": wobble,
            "SOL-USDT": wobble,
        }, index=self.idx)
        feat = compute_features(closes)
        # When sigma is ~0 we end up with NaN; when sigma is finite the
        # numerator is exactly 0. Either is acceptable — strength is not
        # informative on a fully flat asset.
        sample = feat["btc_trend_strength"].iloc[800]
        self.assertTrue(
            np.isnan(sample) or abs(sample) < 1e-9,
            f"flat BTC should give zero or NaN trend strength, got {sample}",
        )

    def test_xs_rank_autocorr_persistence_and_bounds(self):
        """
        - Constant per-asset drift → ranks stable → autocorr ≈ +1.
        - On random data autocorr stays inside [-1, 1].
        """
        # Persistence: 5 assets each with constant exponential drift; the
        # 72h-return rank vector is identical across all timestamps.
        n_assets = 5
        idx = _hourly_index(self.n)
        drifts = np.linspace(0.0001, 0.0010, n_assets)
        closes = pd.DataFrame(
            100.0 * np.exp(np.outer(np.arange(self.n), drifts)),
            index=idx,
            columns=[f"{c}-USDT" for c in ["BTC", "ETH", "SOL", "XRP", "BNB"]],
        )
        feat = compute_features(closes)
        autocorr = feat["xs_rank_autocorr_72h"].iloc[800]
        self.assertAlmostEqual(autocorr, 1.0, places=6)

        # Bounds: random walk → autocorr is a Pearson correlation of two
        # rank vectors per row; must lie in [-1, 1] (NaN allowed during
        # warmup). Construct anti-persistence is brittle to fixture, so
        # bounds-only is the reliable invariant.
        rng = np.random.default_rng(99)
        random_closes = pd.DataFrame(
            100.0 * np.exp(np.cumsum(rng.standard_normal((self.n, 5)) * 0.01, axis=0)),
            index=idx,
            columns=[f"{c}-USDT" for c in ["BTC", "ETH", "SOL", "XRP", "BNB"]],
        )
        feat_rand = compute_features(random_closes)["xs_rank_autocorr_72h"].dropna()
        self.assertGreaterEqual(feat_rand.min(), -1.0 - 1e-9)
        self.assertLessEqual(feat_rand.max(), 1.0 + 1e-9)
        # On a true random walk the long-run mean autocorr should be near 0.
        self.assertLess(abs(feat_rand.mean()), 0.5)


class TestTarget(unittest.TestCase):
    """Strategy-aligned basket forward return."""

    def test_basket_pnl_picks_correct_legs_under_REV(self):
        """signal_sign=-1: top-3 ranked = highest 72h LOSSES. If those
        losses then revert (i.e., go up over the next 24h), the basket
        return should be positive."""
        idx = _hourly_index(300)
        # 3 'losers' that drop sharply in the lookback then bounce back;
        # 3 'winners' that rise steadily then keep rising mildly.
        losers, winners = {}, {}
        for k in range(3):
            base = np.full(300, 100.0)
            base[:200] = np.linspace(110.0, 90.0, 200)        # decline through t
            base[200:] = np.linspace(90.0, 105.0, 100)        # rebound after t
            losers[f"L{k}-USDT"] = base
        for k in range(3):
            base = np.linspace(90.0, 100.0, 300)              # steady rise
            winners[f"W{k}-USDT"] = base
        # BTC required by feature pipeline; doesn't need to be in basket.
        prices = {**losers, **winners, "BTC-USDT": np.full(300, 50.0)}
        closes = pd.DataFrame(prices, index=idx)

        target = compute_basket_forward_return(closes)
        # Anchor t = 200: by t, losers have lost ~18%, winners gained ~10%.
        # Under REV (sign=-1), losers become longs (rebound up = profit),
        # winners become shorts (continue up = loss). Long-side rebound
        # dominates → positive basket return.
        sampled = target.iloc[200]
        self.assertFalse(np.isnan(sampled))
        self.assertGreater(sampled, 0.0, f"REV basket should be positive at t=200, got {sampled}")


class TestBinarization(unittest.TestCase):

    def test_threshold_is_train_quantile_when_unspecified(self):
        raw = pd.Series(np.linspace(-1.0, 1.0, 101))
        labels, thresh = binarize_to_loss_tail(raw, quantile=0.25)
        # 25th percentile of [-1..1] linspace(101) is -0.5.
        self.assertAlmostEqual(thresh, -0.5, places=6)
        # ~25% of samples below threshold.
        self.assertTrue(0.20 <= labels.mean() <= 0.30)

    def test_threshold_passed_through_to_test_fold(self):
        raw_train = pd.Series([0.0, 0.0, 0.0, 0.0, -10.0])
        _, thresh = binarize_to_loss_tail(raw_train, quantile=0.25)
        # Apply the same threshold to a different distribution.
        raw_test = pd.Series([thresh - 0.1, thresh, thresh + 0.1])
        labels_test, thresh_reused = binarize_to_loss_tail(raw_test, threshold=thresh)
        self.assertEqual(thresh, thresh_reused)
        # Only the value strictly below the threshold gets label=1.
        self.assertEqual(list(labels_test.values), [1.0, 0.0, 0.0])


class TestWalkForwardFolds(unittest.TestCase):

    def test_fold_layout_for_12_months(self):
        idx = pd.date_range("2025-01-01", "2025-12-31 23:00", freq="1h", tz="UTC")
        folds = build_folds(idx, train_months=6, test_months=3, step_months=3)
        # train 6 + test 3 = 9 month window; step 3.
        # Fold 1: 2025-01 → 2025-07 train, 2025-07 → 2025-10 test (fits)
        # Fold 2: 2025-04 → 2025-10 train, 2025-10 → 2026-01 test
        #   (test_end 2026-01 > data_end 2026-01 → BORDERLINE; build_folds
        #    requires test_end ≤ data_end)
        self.assertGreaterEqual(len(folds), 1)
        f1 = folds[0]
        self.assertEqual(f1[0], pd.Timestamp("2025-01-01", tz="UTC"))
        self.assertEqual(f1[1], pd.Timestamp("2025-07-01", tz="UTC"))
        self.assertEqual(f1[2], pd.Timestamp("2025-07-01", tz="UTC"))
        self.assertEqual(f1[3], pd.Timestamp("2025-10-01", tz="UTC"))


class TestClassifierRoundTrip(unittest.TestCase):

    def test_save_load_preserves_predictions(self):
        rng = np.random.default_rng(123)
        n = 400
        # Build a 6-feature DataFrame with a planted linear signal so the
        # model converges to non-trivial weights.
        X = pd.DataFrame(
            rng.standard_normal((n, len(FEATURE_NAMES))),
            columns=list(FEATURE_NAMES),
        )
        weights = np.array([1.0, -0.7, 0.3, 0.5, -0.4, 0.2])
        score = X.values @ weights + rng.standard_normal(n) * 0.5
        raw = pd.Series(score, name="basket_fwd_ret")

        clf = RegimeClassifierE()
        clf.fit(X, raw, train_end_ts="2025-09-30T00:00:00+00:00")
        proba_before = clf.predict_proba_loss(X)

        with tempfile.TemporaryDirectory() as tmpdir:
            artifact = Path(tmpdir) / "regime_classifier_e_test.joblib"
            clf.save(artifact)
            loaded = RegimeClassifierE.load(artifact)
            proba_after = loaded.predict_proba_loss(X)

        # Predictions are identical to floating-point precision.
        np.testing.assert_allclose(
            proba_before.values, proba_after.values, atol=1e-12,
        )
        self.assertEqual(loaded.threshold, clf.threshold)
        self.assertEqual(loaded.train_end_ts, clf.train_end_ts)


if __name__ == "__main__":
    unittest.main()
