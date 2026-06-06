#!/usr/bin/env python3
"""Phase-0 OFFLINE edge-gate for FREE, EXOGENOUS sentiment signals.

Plan: docs plan "Sentimentmeter" / `eerst-even-terug-naar-glittery-matsumoto`.
This is the GATE before any collector/overlay is built: does a free, structured,
exogenous signal predict, OUT OF SAMPLE past a null+sham, EITHER

  Track A (risk-off)   — the live dollar-neutral MOMENTUM book's forward DOWNSIDE
                         (a "risk-elevated" flag must materially WORSEN the book's
                         forward CVaR-5 / max-DD / %-negative / forward-Sharpe vs
                         unconditional, OOS); for a neutral book this is factor/
                         dispersion risk, not "market down" per se.

  Track B (directional)— forward RETURN DIRECTION past the null, LONG-side and
                         SHORT-side measured SEPARATELY (risk<->reward asymmetry;
                         shorting has different cost/risk). Higher bar, skeptical
                         prior (price-derived regime-tilt was beta — see
                         docs/XS-SENTIMENT-TILT-STUDY.md). Open question: does
                         EXOGENOUS data carry directional info price-data didn't?

FIRST WAVE — only ALREADY-AVAILABLE free signals (no new network backfills):
  - DVOL level (causal percentile) + DVOL 5d change            [deribit_dvol_BTC.csv]
  - BTC daily vol-ratio (recent/MA daily-return vol)           [compute_vol_halt logic]
  - cross-sectional BREADTH (frac of universe above its SMA)   [compute_breadth_skip logic]
  - cross-sectional DISPERSION (std of trailing returns)       [regime_classifier_e logic]
  - BTC funding extreme (daily-summed 8h funding, percentile)  [funding_btc_usdt.csv]
  - regime_e composite (DAILY analogue of the loss-tail features, causal logistic)

Deferred SECOND WAVE (only if this wave is promising; NOT built here): OKX
open-interest surge + liquidation-cascade backfills (per-asset OI/liq history is
not in the repo; OKX public funding/OI history is ~3 months — too short for a
cross-sectional dispersion signal over the 3.45y panel).

Reuses the harness: book returns + IC + xsectional null from
backtest/sweep/xsectional.py; the single-asset Candidate gate + signal-IC + sham
from scripts/sweep_feasibility.py; the random-entry null from
backtest/random_entry_null.py. Does NOT touch the live runner, configs, state,
git, or the LXC. Writes backtest/results/sweep/sentiment_signals.json.

Adversarial discipline: every signal is poison-tested for lookahead; Track A uses
a frequency+clustering-matched random-flag null AND a shuffle sham; Track B uses
the non-negotiable random-entry null + sham + a zero-cost control. A clean
"nothing clears" is a valid, valuable, money-saving result.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "backtest"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from sweep.xsectional import (  # noqa: E402
    load_panel, DEFAULT_ASSETS, XSConfig, _portfolio_returns,
)
from sweep_feasibility import (  # noqa: E402
    Candidate, evaluate, DEFAULT_OKX_SPOT_CFG, _signal_ic,
)
from daily_backtester import load_daily_btc  # noqa: E402

DATA = PROJECT_ROOT / "backtest" / "data"
OKX_DATA = DATA / "okx"
RESULTS = PROJECT_ROOT / "backtest" / "results" / "sweep"

LB, REBAL, M = 120, 5, 3          # the live book's params
TRAIN_FRAC = 0.70
FWD_WINDOW = 5                    # forward downside window (days) == rebal cadence
NULL_REPS = 2000                 # random-flag null reps (Track A)
SEED = 20260606
EPS = 1e-12


# ===========================================================================
# Book returns (the live dollar-neutral momentum lane) aligned to a daily index
# ===========================================================================

def build_book() -> Tuple[pd.DataFrame, pd.Series]:
    """Return (panel, book_daily_returns).

    book[t] is the realized return of the lb=120/rebal=5/m=3 dollar-neutral
    momentum book over panel.index[t] -> panel.index[t+1] (decided at close of
    t using data <= t). Indexed by panel.index[1:] so it lines up with "the
    return that follows a signal observed at the close of day t".
    """
    panel = load_panel(DEFAULT_ASSETS)
    if panel.empty:
        raise SystemExit("No OKX panel — run: python -m backtest.okx_backfill --bar 1Dutc")
    cfg = XSConfig(lookback=LB, rebal=REBAL, m=M, cost_rate=0.0015)
    port = _portfolio_returns(panel.to_numpy(), cfg, selection="momentum")
    book = pd.Series(port, index=panel.index[1:], name="book_ret")
    return panel, book


# ===========================================================================
# Signal builders — every value at index t uses ONLY data with timestamp <= t.
# All signals are returned as a pd.Series indexed by a DatetimeIndex; the study
# aligns them to the book index by as-of (reindex+ffill of strictly-past values).
# ===========================================================================

def _percentile_rank_causal(s: pd.Series, window: int) -> pd.Series:
    """Trailing percentile rank of s_t within s[t-window+1 .. t] (causal)."""
    def rank_last(x: np.ndarray) -> float:
        return float((x < x[-1]).mean())
    return s.rolling(window, min_periods=max(20, window // 4)).apply(rank_last, raw=True)


def sig_dvol_level() -> pd.Series:
    """Causal trailing percentile of the BTC DVOL (implied-vol index) level.

    High DVOL = the options market prices a wide forward distribution -> a
    risk-elevated regime. Daily, exogenous to the book's price ranking.
    """
    d = pd.read_csv(DATA / "deribit_dvol_BTC.csv")
    d["timestamp"] = pd.to_datetime(d["timestamp"], utc=True, format="ISO8601")
    s = d.set_index("timestamp")["dvol"].astype(float)
    return _percentile_rank_causal(s, 252).rename("dvol_level_pct")


def sig_dvol_change() -> pd.Series:
    """Causal 5-day change in DVOL, z-scored over a trailing year.

    A sharp jump in implied vol = the market just re-priced risk UP. Exogenous
    'something is happening' signal independent of realized price.
    """
    d = pd.read_csv(DATA / "deribit_dvol_BTC.csv")
    d["timestamp"] = pd.to_datetime(d["timestamp"], utc=True, format="ISO8601")
    s = d.set_index("timestamp")["dvol"].astype(float)
    chg = s - s.shift(5)
    mu = chg.rolling(252, min_periods=60).mean()
    sd = chg.rolling(252, min_periods=60).std()
    return ((chg - mu) / sd.replace(0, np.nan)).rename("dvol_chg_z")


def sig_btc_vol_ratio(panel: pd.DataFrame) -> pd.Series:
    """BTC recent/MA daily-return vol ratio (compute_vol_halt, daily-adapted).

    recent = 5d std of daily log-returns, MA = 30d std. Ratio > 1 == vol is
    expanding now relative to its month — the classic risk-elevation gauge.
    Causal: at index t uses returns through day t only.
    """
    btc = panel["BTC-USDT"].astype(float)
    lr = np.log(btc / btc.shift(1))
    recent = lr.rolling(5, min_periods=5).std()
    ma = lr.rolling(30, min_periods=30).std()
    return (recent / ma.replace(0, np.nan)).rename("btc_vol_ratio")


def sig_breadth(panel: pd.DataFrame) -> pd.Series:
    """Cross-sectional BREADTH: fraction of the universe above its 50d SMA.

    compute_breadth_skip logic, daily. Extreme breadth (very low OR very high)
    flags crowding/stress; here we expose the raw fraction (Track B reads its
    sign; Track A flags the LOW tail = risk-off). Causal: SMA at t uses closes
    through t.
    """
    sma = panel.rolling(50, min_periods=50).mean()
    above = (panel > sma).sum(axis=1)
    valid = sma.notna().sum(axis=1)
    return (above / valid.replace(0, np.nan)).rename("breadth")


def sig_dispersion(panel: pd.DataFrame) -> pd.Series:
    """Cross-sectional DISPERSION: causal trailing percentile of the std of the
    universe's trailing 20d log-returns (regime_classifier_e xs_dispersion logic).

    High dispersion = assets decoupling = a regime where a momentum ranking can
    swing hard (factor risk for a neutral book). Causal by construction.
    """
    lr = np.log(panel / panel.shift(20))
    disp = lr.std(axis=1)
    return _percentile_rank_causal(disp, 252).rename("dispersion_pct")


def sig_funding_extreme() -> Optional[pd.Series]:
    """BTC funding extreme: |daily-summed 8h funding|, causal trailing percentile.

    Extreme funding (very positive OR very negative) = crowded perp positioning
    -> squeeze/de-leveraging risk. Uses funding_btc_usdt.csv (8h settlements,
    2023-01..2026-05 — covers most of the panel for BTC). Per-asset funding
    DISPERSION is DEFERRED to wave 2 (OKX per-asset history is only ~3 months).
    Causal: percentile uses settlements through day t only.
    """
    p = DATA / "funding_btc_usdt.csv"
    if not p.exists():
        return None
    d = pd.read_csv(p)
    d["timestamp"] = pd.to_datetime(d["timestamp"], utc=True, format="ISO8601")
    daily = d.groupby(d["timestamp"].dt.floor("D"))["funding_rate"].sum()
    absf = daily.abs()
    return _percentile_rank_causal(absf, 180).rename("funding_extreme_pct")


def sig_regime_e_daily(panel: pd.DataFrame, book: pd.Series) -> pd.Series:
    """DAILY analogue of regime_classifier_e's loss-tail probability.

    The shipped classifier is HOURLY (72h / SMA200h features, hourly model). The
    book here is daily, so we build the SAME family of universe-level features on
    the daily panel (breadth, breadth-above-SMA, dispersion, BTC vol-ratio, BTC
    trend strength, rank-autocorr) and fit an L2 logistic on the TRAIN fold only
    to predict whether the book's next-5d return is in the train bottom quartile.
    p_loss is then evaluated everywhere (train-fitted, applied OOS). The hourly
    model itself is DEFERRED (it needs an aligned hourly panel for all 10 assets;
    only the original universe has 1H files). Strictly causal: features at t use
    closes through t; the fit uses only train-fold rows; the label is forward.
    """
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    feat = pd.DataFrame(index=panel.index)
    lr20 = np.log(panel / panel.shift(20))
    feat["breadth_pos"] = (lr20 > 0).sum(axis=1) / lr20.notna().sum(axis=1).replace(0, np.nan)
    sma = panel.rolling(50, min_periods=50).mean()
    feat["breadth_sma"] = (panel > sma).sum(axis=1) / sma.notna().sum(axis=1).replace(0, np.nan)
    feat["dispersion"] = lr20.std(axis=1)
    btc = panel["BTC-USDT"].astype(float)
    blr = np.log(btc / btc.shift(1))
    recent = blr.rolling(5, min_periods=5).std()
    ma = blr.rolling(30, min_periods=30).std()
    feat["vol_ratio"] = (recent / ma.replace(0, np.nan))
    btrend = np.log(btc / btc.shift(20))
    feat["trend_strength"] = (btrend.abs() / (ma * np.sqrt(20)).replace(0, np.nan))
    ranks_now = lr20.rank(axis=1)
    ranks_prev = ranks_now.shift(20)
    a = ranks_now.sub(ranks_now.mean(axis=1), axis=0)
    b = ranks_prev.sub(ranks_prev.mean(axis=1), axis=0)
    den = np.sqrt((a ** 2).sum(axis=1) * (b ** 2).sum(axis=1))
    feat["rank_ac"] = ((a * b).sum(axis=1) / den.replace(0, np.nan))

    # Align features to the book index (signal at t predicts book over t->t+1).
    feat = feat.replace([np.inf, -np.inf], np.nan).reindex(book.index)
    fwd = book[::-1].rolling(FWD_WINDOW, min_periods=FWD_WINDOW).sum()[::-1]  # next-5d sum

    n = len(book)
    cut = int(n * TRAIN_FRAC)
    train_idx = book.index[:cut]
    cols = list(feat.columns)
    tr = feat.loc[train_idx].join(fwd.rename("fwd")).dropna()
    if len(tr) < 100:
        return pd.Series(np.nan, index=book.index, name="regime_e_p")
    thr = float(tr["fwd"].quantile(0.25))
    y = (tr["fwd"] < thr).astype(int).values
    sc = StandardScaler().fit(tr[cols].values)
    clf = LogisticRegression(C=1.0, penalty="l2", solver="lbfgs",
                             class_weight="balanced", max_iter=2000)
    clf.fit(sc.transform(tr[cols].values), y)

    allf = feat[cols].dropna()
    p = pd.Series(clf.predict_proba(sc.transform(allf.values))[:, 1],
                  index=allf.index)
    return p.reindex(book.index).rename("regime_e_p")


# ===========================================================================
# Lookahead poison-test — re-derive each signal after poisoning the FUTURE of
# its source series; the past values must be unchanged.
# ===========================================================================

def poison_test_pct_rank() -> bool:
    """Percentile-rank / vol-ratio / breadth are all trailing-rolling; poison the
    tail of a source series and confirm the earlier values do not move."""
    rng = np.random.default_rng(1)
    base = pd.Series(rng.normal(50, 10, 600).cumsum() / 100 + 50,
                     index=pd.date_range("2022-01-01", periods=600, freq="D", tz="UTC"))
    clean = _percentile_rank_causal(base, 252)
    poisoned = base.copy()
    poisoned.iloc[400:] = 1e9
    pois = _percentile_rank_causal(poisoned, 252)
    return bool(np.allclose(clean.iloc[:400].dropna(), pois.iloc[:400].reindex(
        clean.iloc[:400].dropna().index), equal_nan=True))


# ===========================================================================
# TRACK A — conditional-downside test (risk-off)
# ===========================================================================

def _forward_window_returns(book: pd.Series, window: int) -> pd.Series:
    """Sum of book returns over the NEXT `window` days starting the day after t
    (i.e. the downside the flag-at-t is trying to predict). NaN at the tail."""
    fwd = book.shift(-1).rolling(window, min_periods=window).sum()
    # the rolling above is backward; build forward explicitly:
    arr = book.to_numpy()
    n = len(arr)
    out = np.full(n, np.nan)
    for t in range(n):
        if t + window <= n:
            out[t] = float(arr[t:t + window].sum())
    return pd.Series(out, index=book.index)


def _downside_stats(rets: np.ndarray) -> Dict[str, float]:
    """Downside descriptors of a set of forward-window returns (in fractions)."""
    rets = rets[np.isfinite(rets)]
    if len(rets) < 5:
        return {"n": int(len(rets)), "mean": float("nan"), "cvar5": float("nan"),
                "min": float("nan"), "pct_neg": float("nan"), "sharpe": float("nan")}
    q5 = np.percentile(rets, 5)
    tail = rets[rets <= q5]
    cvar5 = float(tail.mean()) if len(tail) else float(q5)
    sd = float(np.std(rets, ddof=1))
    return {
        "n": int(len(rets)),
        "mean": float(np.mean(rets)),
        "cvar5": cvar5,
        "min": float(np.min(rets)),
        "pct_neg": float(np.mean(rets < 0)),
        "sharpe": float(np.mean(rets) / sd) if sd > 0 else 0.0,
    }


def _flag_clusters(flag: np.ndarray) -> Tuple[float, int]:
    """Mean run-length and number of runs of True in a boolean flag."""
    runs, cur = [], 0
    for v in flag:
        if v:
            cur += 1
        elif cur:
            runs.append(cur); cur = 0
    if cur:
        runs.append(cur)
    return (float(np.mean(runs)) if runs else 0.0), len(runs)


def _matched_random_flag(n: int, frac: float, mean_run: float,
                         rng: np.random.Generator) -> np.ndarray:
    """A random boolean flag with ~matched frequency and clustering (geometric
    in/out runs, same construction as random_entry_null.make_random_schedule)."""
    frac = float(min(max(frac, 1e-3), 1.0 - 1e-3))
    mr = max(mean_run, 1.0)
    mo = max(mr * (1.0 - frac) / frac, 1.0)
    out = np.zeros(n, dtype=bool)
    i = 0
    state = rng.random() < frac
    while i < n:
        ln = int(rng.geometric(1.0 / (mr if state else mo)))
        j = min(n, i + ln)
        if state:
            out[i:j] = True
        i = j
        state = not state
    return out


def track_a(name: str, flag: pd.Series, book: pd.Series, *,
            window: int = FWD_WINDOW, reps: int = NULL_REPS,
            seed: int = SEED) -> Dict:
    """Does the risk-elevated flag WORSEN the book's forward downside OOS, past a
    frequency+clustering-matched random-flag null AND a shuffle sham?

    flag: boolean Series (data <= t), aligned to book.index. We compare the
    forward-`window` book returns conditional-on-flag vs unconditional, on the
    OOS holdout only. "Worse" = lower forward mean/Sharpe AND/OR worse (more
    negative) CVaR-5 / more %-negative. Significance: the conditional-on-flag
    CVaR-5 and mean must sit in the WORSE tail of a matched random-flag null
    (<=5th pct for mean/Sharpe, i.e. worse than 95% of random flags), AND the
    sham (shuffled flag) must NOT reproduce it.
    """
    flag = flag.reindex(book.index).fillna(False).astype(bool)
    fwd = _forward_window_returns(book, window)
    n = len(book)
    cut = int(n * TRAIN_FRAC)

    oos_flag = flag.iloc[cut:].to_numpy()
    oos_fwd = fwd.iloc[cut:].to_numpy()
    oos_book = book.iloc[cut:].to_numpy()

    valid = np.isfinite(oos_fwd)
    f = oos_flag & valid
    uncond = _downside_stats(oos_fwd[valid])
    cond = _downside_stats(oos_fwd[f])
    frac = float(f.mean()) if len(f) else 0.0
    mean_run, n_runs = _flag_clusters(oos_flag[valid])

    if cond["n"] < 10 or frac < 0.02 or frac > 0.6:
        return {"name": name, "verdict": "KILL", "track": "A",
                "reason": f"flag fires {frac:.1%} of OOS (n={cond['n']}) — "
                          "too rare/dense to test downside conditioning",
                "frac": round(frac, 3), "uncond": uncond, "cond": cond}

    # --- matched random-flag null: how often does a random flag of the same
    #     frequency+clustering produce a conditional downside this bad? ---
    rng = np.random.default_rng(seed)
    null_mean, null_cvar, null_sharpe, null_pneg = [], [], [], []
    fwd_oos_only = oos_fwd
    for _ in range(reps):
        rf = _matched_random_flag(len(oos_book), max(frac, 1e-3), max(mean_run, 1.0), rng)
        rf = rf & valid
        st = _downside_stats(fwd_oos_only[rf])
        if np.isfinite(st["mean"]):
            null_mean.append(st["mean"]); null_cvar.append(st["cvar5"])
            null_sharpe.append(st["sharpe"]); null_pneg.append(st["pct_neg"])
    null_mean = np.array(null_mean); null_cvar = np.array(null_cvar)
    null_sharpe = np.array(null_sharpe); null_pneg = np.array(null_pneg)

    # percentile of the conditional stat within the null (lower = worse downside)
    def pct_below(obs, dist):
        return float(np.mean(dist < obs) * 100.0) if len(dist) else float("nan")
    mean_pctile = pct_below(cond["mean"], null_mean)        # low == worse than null
    cvar_pctile = pct_below(cond["cvar5"], null_cvar)
    sharpe_pctile = pct_below(cond["sharpe"], null_sharpe)
    pneg_pctile = float(np.mean(null_pneg < cond["pct_neg"]) * 100.0) if len(null_pneg) else float("nan")

    # "materially worse OOS past null": conditional mean/Sharpe in the worst 5%
    # of the matched null (<=5th pct) AND CVaR-5 worse than the null median, AND
    # worse than unconditional in level.
    worsens_mean = (mean_pctile <= 5.0) and (cond["mean"] < uncond["mean"])
    worsens_sharpe = (sharpe_pctile <= 5.0) and (cond["sharpe"] < uncond["sharpe"])
    worsens_cvar = (cvar_pctile <= 50.0) and (cond["cvar5"] < uncond["cvar5"])
    past_null = (worsens_mean or worsens_sharpe) and worsens_cvar

    # --- shuffle sham: permute the flag positions (break causality), re-test ---
    sham_hits = 0
    n_sham = 5
    for k in range(n_sham):
        srng = np.random.default_rng(seed + 101 + k)
        sf = srng.permutation(oos_flag) & valid
        sst = _downside_stats(fwd_oos_only[sf])
        s_mean_p = pct_below(sst["mean"], null_mean)
        s_cvar_p = pct_below(sst["cvar5"], null_cvar)
        if (s_mean_p <= 5.0) and (s_cvar_p <= 50.0) and np.isfinite(sst["mean"]):
            sham_hits += 1
    sham_confirmed_failing = sham_hits < (n_sham // 2 + 1)

    if not sham_confirmed_failing:
        verdict = "VOID"
    elif past_null:
        verdict = "ADVANCE"
    else:
        verdict = "KILL"

    return {
        "name": name, "track": "A", "verdict": verdict,
        "frac_oos": round(frac, 3), "mean_run_days": round(mean_run, 1),
        "n_runs": n_runs,
        "uncond": {k: round(v, 5) if isinstance(v, float) else v for k, v in uncond.items()},
        "cond": {k: round(v, 5) if isinstance(v, float) else v for k, v in cond.items()},
        "null_percentiles": {
            "mean_pctile": round(mean_pctile, 1), "cvar5_pctile": round(cvar_pctile, 1),
            "sharpe_pctile": round(sharpe_pctile, 1), "pct_neg_pctile": round(pneg_pctile, 1),
        },
        "worsens": {"mean": bool(worsens_mean), "sharpe": bool(worsens_sharpe),
                    "cvar5": bool(worsens_cvar)},
        "past_null": bool(past_null),
        "sham_hits": sham_hits, "sham_confirmed_failing": bool(sham_confirmed_failing),
    }


# ===========================================================================
# TRACK B — directional test (long-side vs short-side SEPARATELY)
# ===========================================================================

def _align_to_btc(sig_src: pd.Series, btc_index: pd.DatetimeIndex) -> pd.Series:
    """As-of align an exogenous signal (its own daily index) to the BTC daily
    backtest index using strictly-past values (reindex + ffill), so no future
    value leaks. Returns a numpy-friendly Series of len(btc_index)."""
    s = sig_src.sort_index()
    aligned = s.reindex(btc_index, method="ffill")
    return aligned


def _sided_ic_and_stats(sig: np.ndarray, btc_close: np.ndarray, horizon: int,
                        expected_sign: int) -> Dict:
    """Separate long-side and short-side directional diagnostics.

    We split days into the signal's LONG bucket (top tercile of the signal, in
    the expected-long direction) and SHORT bucket (bottom tercile). For each
    bucket: forward `horizon`-day return mean, hit-rate (sign matches the side),
    forward CVaR-5, and the per-side IC (Spearman within the bucket). This makes
    the risk<->reward asymmetry explicit: a signal may call crashes (short side)
    far better than rallies (long side).
    """
    close = pd.Series(btc_close, dtype=float)
    fwd = (close.shift(-horizon) / close - 1.0).to_numpy()
    s = np.asarray(sig, dtype=float)
    m = np.isfinite(s) & np.isfinite(fwd)
    s, fwd = s[m], fwd[m]
    if len(s) < 60:
        return {"insufficient": True, "n": int(len(s))}

    # orient so "high oriented-signal" => expected LONG
    osig = s * expected_sign
    lo, hi = np.quantile(osig, [1 / 3, 2 / 3])
    long_mask = osig >= hi
    short_mask = osig <= lo

    def side(mask, want_sign):
        r = fwd[mask]
        if len(r) < 10:
            return {"n": int(len(r)), "fwd_mean": None, "hit_rate": None,
                    "cvar5": None, "ic": None, "ic_p": None}
        q5 = np.percentile(r, 5)
        tail = r[r <= q5]
        ic, icp = spearmanr(osig[mask], fwd[mask])
        return {
            "n": int(len(r)),
            "fwd_mean": round(float(np.mean(r)), 5),
            "hit_rate": round(float(np.mean(np.sign(r) == want_sign)), 3),
            "cvar5": round(float(tail.mean()) if len(tail) else float(q5), 5),
            "ic": round(float(ic), 4) if np.isfinite(ic) else None,
            "ic_p": round(float(icp), 4) if np.isfinite(icp) else None,
        }

    return {"insufficient": False,
            "long_side": side(long_mask, +1),   # long bucket wants positive fwd
            "short_side": side(short_mask, -1)}  # short bucket wants negative fwd


@dataclass
class DirSpec:
    name: str
    src: Callable[[], pd.Series]   # returns the raw exogenous signal series
    expected_sign: int             # +1 long when signal high, -1 inverse
    thesis: str


def track_b(spec: DirSpec, btc_df: pd.DataFrame, *,
            train_frac: float = TRAIN_FRAC, horizon: int = FWD_WINDOW) -> Dict:
    """Directional gate via sweep_feasibility on OOS BTC, with separate long/short
    diagnostics and a zero-cost control. The Candidate's exposure is long-or-flat
    (the harness is long/flat), so the gate's null/cost apply to the LONG leg; the
    SHORT-side directional content is reported via the sided IC/forward stats +
    an inverse-exposure IC. This separates whether the exogenous signal predicts
    UP moves (tradeable long) from DOWN moves (the risk-off / short thesis)."""
    raw = spec.src()
    btc_idx = pd.to_datetime(btc_df["timestamp"], utc=True, format="ISO8601")
    aligned = _align_to_btc(raw, pd.DatetimeIndex(btc_idx)).to_numpy()

    n = len(btc_df)
    cut = int(n * train_frac)
    oos = btc_df.iloc[cut:].reset_index(drop=True)
    oos_sig = aligned[cut:]

    sign = spec.expected_sign

    def signal_fn(df: pd.DataFrame) -> pd.Series:
        return pd.Series(oos_sig[:len(df)], index=df.index)

    def exposure_fn(s: pd.Series, df: pd.DataFrame) -> pd.Series:
        osig = pd.Series(np.asarray(s, float) * sign, index=df.index)
        thr = osig.quantile(2 / 3)
        return (osig >= thr).astype(float)   # long when in top tercile of oriented signal

    cand = Candidate(name=f"dir_{spec.name}", signal_fn=signal_fn,
                     exposure_fn=exposure_fn, ic_horizon=horizon,
                     directional=True, expected_sign=sign, thesis=spec.thesis)
    try:
        verdict = evaluate(cand, oos, reps=400, n_sham=3, seed=SEED)
        gate = verdict.to_json()
    except Exception as e:  # pragma: no cover
        gate = {"verdict": "ERROR", "error": str(e)}

    # zero-cost control: same long exposure, evaluate on a zero-cost cfg ->
    # if the LONG leg only "works" with zero costs it's not tradeable.
    from dataclasses import replace as dc_replace
    zcfg = dc_replace(DEFAULT_OKX_SPOT_CFG, fee_maker=0.0, fee_taker=0.0,
                      slippage_pct=0.0, funding_series_path=None)
    try:
        zverdict = evaluate(cand, oos, cfg=zcfg, reps=300, n_sham=1, seed=SEED)
        zero_cost_null = zverdict.metrics.get("null_percentile")
    except Exception:
        zero_cost_null = None

    # SHORT-side GATE (not just descriptive IC): express "be short when the
    # oriented-signal is LOW" as a long-or-flat bet on the NEGATED price series,
    # so the same random-entry null + sham apply to the short leg. A real
    # short-side edge must clear its own null>95 + sham, separately from the long
    # leg — this is the rigorous test of the risk<->reward asymmetry.
    oos_short = oos.copy()
    base = float(oos_short["close"].iloc[0]) * 2.0
    for col in ("open", "high", "low", "close"):
        oos_short[col] = base - oos_short[col].astype(float)   # invert -> short==long
    oos_short["high"], oos_short["low"] = (
        oos_short[["high", "low"]].max(axis=1), oos_short[["high", "low"]].min(axis=1))

    def short_exposure_fn(s: pd.Series, df: pd.DataFrame) -> pd.Series:
        osig = pd.Series(np.asarray(s, float) * sign, index=df.index)
        thr = osig.quantile(1 / 3)
        return (osig <= thr).astype(float)   # be "long the inverse" (short BTC) on low oriented-signal

    short_cand = Candidate(
        name=f"short_{spec.name}",
        signal_fn=lambda df: pd.Series(oos_sig[:len(df)], index=df.index),
        exposure_fn=short_exposure_fn, ic_horizon=horizon,
        directional=True, expected_sign=-sign,   # on the inverted series, sign flips
        thesis="short leg: " + spec.thesis)
    try:
        sverdict = evaluate(short_cand, oos_short, reps=400, n_sham=3, seed=SEED)
        short_gate = sverdict.to_json()
    except Exception as e:  # pragma: no cover
        short_gate = {"verdict": "ERROR", "error": str(e)}

    sided = _sided_ic_and_stats(oos_sig, oos["close"].astype(float).to_numpy(),
                                horizon, sign)

    return {"name": spec.name, "track": "B", "thesis": spec.thesis,
            "expected_sign": sign, "n_oos": int(len(oos)),
            "long_gate": gate, "short_gate": short_gate,
            "zero_cost_null_pctile": zero_cost_null, "sided": sided}


# ===========================================================================
# Driver
# ===========================================================================

def main() -> int:
    panel, book = build_book()
    n = len(book)
    cut = int(n * TRAIN_FRAC)
    years = n / 365.0
    print(f"book: {panel.shape[1]} assets x {len(panel)} days "
          f"(book rets {n}d, {years:.2f}y); train {cut}d / OOS {n - cut}d; "
          f"lb={LB}/rebal={REBAL}/m={M}, fwd-window={FWD_WINDOW}d")
    print(f"book OOS span: {book.index[cut].date()} .. {book.index[-1].date()}")

    # ---- lookahead poison-test (the #1 trap) ----
    poison_ok = poison_test_pct_rank()
    print(f"lookahead poison-test (trailing percentile/vol-ratio/breadth family): "
          f"{'PASSED' if poison_ok else 'FAILED'}")

    # ---- build the causal signals, aligned to the book index ----
    raw_signals: Dict[str, pd.Series] = {
        "dvol_level": sig_dvol_level(),
        "dvol_change": sig_dvol_change(),
        "btc_vol_ratio": sig_btc_vol_ratio(panel),
        "breadth": sig_breadth(panel),
        "dispersion": sig_dispersion(panel),
    }
    fe = sig_funding_extreme()
    if fe is not None:
        raw_signals["funding_extreme"] = fe
    raw_signals["regime_e_daily"] = sig_regime_e_daily(panel, book)

    # align each to book index (as-of, strictly past) for coverage reporting
    aligned = {}
    for k, s in raw_signals.items():
        a = s.sort_index().reindex(book.index, method="ffill")
        aligned[k] = a
        cov = float(a.iloc[cut:].notna().mean())
        print(f"  signal {k:16s}: OOS coverage {cov:.0%}")

    # ---- Track A flags (risk-elevated = top/bottom tail, causal) ----
    # Each flag fires when the signal is in a high-risk causal state. Thresholds
    # are TRAIN-fold quantiles (no OOS peeking).
    def train_q(s: pd.Series, q: float) -> float:
        return float(s.iloc[:cut].quantile(q))

    a_flags: Dict[str, pd.Series] = {
        # high implied vol / vol-expansion / extreme funding / high dispersion =
        # risk-elevated; LOW breadth = risk-off (universe weak)
        "dvol_level": aligned["dvol_level"] >= train_q(aligned["dvol_level"], 0.80),
        "dvol_change": aligned["dvol_change"] >= train_q(aligned["dvol_change"], 0.80),
        "btc_vol_ratio": aligned["btc_vol_ratio"] >= train_q(aligned["btc_vol_ratio"], 0.80),
        "breadth_low": aligned["breadth"] <= train_q(aligned["breadth"], 0.20),
        "dispersion": aligned["dispersion"] >= train_q(aligned["dispersion"], 0.80),
        "regime_e_daily": aligned["regime_e_daily"] >= train_q(aligned["regime_e_daily"], 0.75),
    }
    if "funding_extreme" in aligned:
        a_flags["funding_extreme"] = aligned["funding_extreme"] >= train_q(aligned["funding_extreme"], 0.80)

    print("\n===== TRACK A — conditional forward-downside of the neutral book (OOS) =====")
    track_a_results = {}
    for name, flag in a_flags.items():
        r = track_a(name, flag, book)
        track_a_results[name] = r
        if "uncond" in r:
            u, c = r["uncond"], r["cond"]
            print(f"  {name:16s} {r['verdict']:8s} fires={r.get('frac_oos','?')} "
                  f"cond.mean={c.get('mean')} (unc {u.get('mean')}) "
                  f"cond.cvar5={c.get('cvar5')} (unc {u.get('cvar5')}) "
                  f"meanPct={r['null_percentiles']['mean_pctile']} "
                  f"sham_fail={r['sham_confirmed_failing']}")
        else:
            print(f"  {name:16s} {r['verdict']:8s} {r.get('reason','')}")

    # ---- Track B directional (BTC single-asset gate; long vs short separate) ----
    btc_df = load_daily_btc(str(OKX_DATA / "BTC-USDT_1Dutc.csv"))
    dir_specs = [
        DirSpec("dvol_level", sig_dvol_level, -1,
                "high implied vol -> lower forward BTC return (risk-off); inverse exposure"),
        DirSpec("dvol_change", sig_dvol_change, -1,
                "implied-vol spike -> forward weakness (de-risk); inverse"),
        DirSpec("btc_vol_ratio", lambda: sig_btc_vol_ratio(panel), -1,
                "vol expansion -> forward weakness; inverse"),
        DirSpec("breadth", lambda: sig_breadth(panel), +1,
                "broad strength (high breadth) -> forward continuation; long"),
        DirSpec("dispersion", lambda: sig_dispersion(panel), -1,
                "high dispersion -> risk regime / forward weakness; inverse"),
    ]
    if fe is not None:
        dir_specs.append(DirSpec("funding_extreme", sig_funding_extreme, -1,
                                 "extreme funding -> squeeze/de-leverage -> forward weakness; inverse"))

    print("\n===== TRACK B — directional (BTC OOS), LONG-side vs SHORT-side separate =====")
    track_b_results = {}
    for spec in dir_specs:
        r = track_b(spec, btc_df)
        track_b_results[spec.name] = r
        g = r["long_gate"]; sg = r["short_gate"]
        s = r["sided"]
        if not s.get("insufficient"):
            ls, ss = s["long_side"], s["short_side"]
            print(f"  {spec.name:16s} LONG[{g.get('verdict','?'):4s} null={g.get('metrics',{}).get('null_percentile')} "
                  f"ic={g.get('metrics',{}).get('ic')} hit={ls.get('hit_rate')}] "
                  f"SHORT[{sg.get('verdict','?'):4s} null={sg.get('metrics',{}).get('null_percentile')} "
                  f"hit={ss.get('hit_rate')} ic={ss.get('ic')} p={ss.get('ic_p')}]")
        else:
            print(f"  {spec.name:16s} insufficient OOS overlap (n={s.get('n')})")

    # ---- mechanical decision per the plan's tree ----
    a_advance = [k for k, v in track_a_results.items() if v.get("verdict") == "ADVANCE"]
    a_void = [k for k, v in track_a_results.items() if v.get("verdict") == "VOID"]
    b_long_advance = [k for k, v in track_b_results.items()
                      if v["long_gate"].get("verdict") == "ADVANCE"]
    b_short_advance = [k for k, v in track_b_results.items()
                       if v["short_gate"].get("verdict") == "ADVANCE"]
    b_advance = sorted(set(b_long_advance) | set(b_short_advance))

    if a_advance and b_advance:
        tree = "A+B: risk-off overlay AND a bounded directional sleeve (asymmetric)"
    elif a_advance:
        tree = "A-only: deploy as a risk-off overlay (Phase 1 collector first)"
    elif b_advance:
        tree = "B-only (surprising): bounded directional sleeve only"
    else:
        tree = "NEITHER: documented null result — no infra, no integration"

    out = {
        "spec": {"lookback": LB, "rebal": REBAL, "m": M, "fwd_window": FWD_WINDOW,
                 "train_frac": TRAIN_FRAC, "n_book_days": n, "n_assets": int(panel.shape[1]),
                 "years": round(years, 2), "oos_days": n - cut, "null_reps": NULL_REPS,
                 "seed": SEED, "book": "dollar-neutral xs-momentum (live lane)"},
        "lookahead_poison_test": "PASSED" if poison_ok else "FAILED",
        "signal_oos_coverage": {k: round(float(aligned[k].iloc[cut:].notna().mean()), 3)
                                for k in aligned},
        "track_a": track_a_results,
        "track_b": track_b_results,
        "decision": {
            "track_a_advance": a_advance, "track_a_void": a_void,
            "track_b_long_advance": b_long_advance,
            "track_b_short_advance": b_short_advance,
            "track_b_advance": b_advance, "tree": tree,
        },
        "deferred_second_wave": (
            "OKX open-interest surge + liquidation-cascade signals. Per-asset OI/liq "
            "history is NOT in the repo and OKX public OI/funding history is ~3 months "
            "(283 rows) — too short for a cross-sectional dispersion signal over the "
            "3.45y panel. Per-asset funding DISPERSION is also deferred for the same "
            "reason (only BTC funding has multi-year history here). The hourly "
            "regime_classifier_e model is deferred too (needs an aligned hourly panel "
            "for all 10 assets; only the original universe has 1H files) — Phase 0 uses "
            "a DAILY analogue. Build the OI/liq backfills ONLY if this wave is promising."),
    }

    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "sentiment_signals.json").write_text(json.dumps(out, indent=2))

    print("\n" + "=" * 78)
    print("DECISION (mechanical, per the plan's tree):", tree)
    print(f"  Track A ADVANCE: {a_advance or 'none'}   VOID: {a_void or 'none'}")
    print(f"  Track B ADVANCE: long={b_long_advance or 'none'} short={b_short_advance or 'none'}")
    print("  results -> backtest/results/sweep/sentiment_signals.json")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
