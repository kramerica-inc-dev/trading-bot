#!/usr/bin/env python3
"""Fase 3b — empirical analysis of the BTC funding-rate signal.

Measures whether the perpetual funding rate predicts subsequent BTC spot
returns *before* it is wired into any bot logic (Fase 5).  Specifically:

  * forward returns at 4h / 24h / 7d horizons vs. the funding rate observed
    at (or just before) the start of the horizon — strictly no lookahead;
  * funding-rate percentiles (p5/p25/p50/p75/p95) — what counts as "extreme";
  * Spearman rank correlation funding vs. forward return + scatter plots;
  * the same, split by market regime (bull / bear / sideways) using the
    Fase 1 ``classify_regimes`` labeller;
  * an in-sample / out-of-sample split (earlier half vs. later half);
  * top-decile vs. bottom-decile forward-return spread (effect size).

Run from the project root:

    python3 -m backtest.analyze_funding

Writes scatter plots to ``backtest/results/`` and prints a markdown report;
``docs/funding-analysis.md`` is the curated write-up of the verdict.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from scipy import stats  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from backtester import classify_regimes  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(__file__).resolve().parent / "data"
RESULTS_DIR = Path(__file__).resolve().parent / "results"
FUNDING_CSV = DATA_DIR / "funding_btc_usdt.csv"
FUNDING_CSV_FALLBACK = DATA_DIR / "funding_BTC-USDT.csv"
CANDLE_CSV = DATA_DIR / "BTC-USDT_1H.csv"
CANDLE_CSV_FALLBACK = DATA_DIR / "BTC-USDT_5m.csv"

HORIZONS_HOURS = {"4h": 4, "24h": 24, "7d": 24 * 7}
PERCENTILES = [5, 25, 50, 75, 95]

# Effect-size gate (from IMPROVEMENT_PLAN.md): top-decile funding -> subsequent
# 24h return spread of at least this magnitude.
MIN_DECILE_SPREAD_24H = 0.005  # 0.5%


# --------------------------------------------------------------------------- #
# Data loading
# --------------------------------------------------------------------------- #
def load_funding() -> Tuple[pd.DataFrame, str]:
    """Return (funding_df, source_path). Normalises column names."""
    path = FUNDING_CSV if FUNDING_CSV.exists() else FUNDING_CSV_FALLBACK
    df = pd.read_csv(path)
    # The collector writes funding_rate/timestamp; funding_backfill writes
    # fundingRate/timestamp.  Normalise.
    rename = {}
    if "fundingRate" in df.columns and "funding_rate" not in df.columns:
        rename["fundingRate"] = "funding_rate"
    if "fundingTime" in df.columns and "funding_time" not in df.columns:
        rename["fundingTime"] = "funding_time"
    df = df.rename(columns=rename)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("timestamp").drop_duplicates(
        subset="timestamp").reset_index(drop=True)
    return df[["timestamp", "funding_rate"]].copy(), str(path)


def load_candles() -> Tuple[pd.DataFrame, str]:
    path = CANDLE_CSV if CANDLE_CSV.exists() else CANDLE_CSV_FALLBACK
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("timestamp").drop_duplicates(
        subset="timestamp").reset_index(drop=True)
    return df[["timestamp", "close"]].copy(), str(path)


# --------------------------------------------------------------------------- #
# Feature construction (no lookahead)
# --------------------------------------------------------------------------- #
def build_panel(funding: pd.DataFrame, candles: pd.DataFrame) -> pd.DataFrame:
    """One row per funding settlement, with forward returns and a regime label.

    For settlement at time ``t`` with rate ``f``:
      * ``close_t``   = the most recent candle close at or before ``t``
                        (data <= t only — no lookahead);
      * ``ret_<h>``   = close at (t + h) / close_t - 1, using the most recent
                        candle close at or before (t + h);
      * ``regime``    = the Fase 1 regime label of the candle nearest to ``t``,
                        computed over the whole candle series (the labeller is
                        itself lookahead-free per-bar).
    Rows whose forward horizon extends past the candle data are dropped for
    that horizon (NaN), so the 7d column simply has fewer usable rows.
    """
    candles = candles.sort_values("timestamp").reset_index(drop=True)
    c_ts = candles["timestamp"].values
    c_close = candles["close"].values.astype(float)

    # Regime labels over the full candle series (per-bar, no lookahead).
    regimes = classify_regimes(c_close.tolist(),
                               candles["timestamp"].tolist())
    regimes = np.array(regimes, dtype=object)

    def close_at_or_before(ts: np.datetime64) -> Tuple[float, int]:
        idx = np.searchsorted(c_ts, ts, side="right") - 1
        if idx < 0:
            return np.nan, -1
        return c_close[idx], idx

    rows = []
    for _, r in funding.iterrows():
        t = np.datetime64(r["timestamp"])
        c0, i0 = close_at_or_before(t)
        if i0 < 0 or not np.isfinite(c0):
            continue
        row = {"timestamp": r["timestamp"],
               "funding_rate": float(r["funding_rate"]),
               "close_t": c0,
               "regime": regimes[i0]}
        for name, h in HORIZONS_HOURS.items():
            t_h = t + np.timedelta64(h, "h")
            if t_h > c_ts[-1]:
                row[f"ret_{name}"] = np.nan
                continue
            c1, i1 = close_at_or_before(t_h)
            row[f"ret_{name}"] = (c1 / c0 - 1.0) if (i1 >= 0 and c0 > 0) else np.nan
        rows.append(row)

    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #
def spearman(x: np.ndarray, y: np.ndarray) -> Tuple[float, float, int]:
    mask = np.isfinite(x) & np.isfinite(y)
    if mask.sum() < 8:
        return np.nan, np.nan, int(mask.sum())
    rho, p = stats.spearmanr(x[mask], y[mask])
    return float(rho), float(p), int(mask.sum())


def decile_spread(funding: np.ndarray, ret: np.ndarray) -> Tuple[float, float, float, int]:
    """Return (top_decile_mean_ret, bottom_decile_mean_ret, spread, n)."""
    mask = np.isfinite(funding) & np.isfinite(ret)
    f, y = funding[mask], ret[mask]
    n = len(f)
    if n < 30:
        return np.nan, np.nan, np.nan, n
    hi_thr = np.quantile(f, 0.9)
    lo_thr = np.quantile(f, 0.1)
    top = y[f >= hi_thr]
    bot = y[f <= lo_thr]
    if len(top) == 0 or len(bot) == 0:
        return np.nan, np.nan, np.nan, n
    return float(top.mean()), float(bot.mean()), float(top.mean() - bot.mean()), n


def analyse_subset(df: pd.DataFrame, label: str) -> Dict:
    out = {"label": label, "n": len(df), "horizons": {}}
    f = df["funding_rate"].values.astype(float)
    for name in HORIZONS_HOURS:
        y = df[f"ret_{name}"].values.astype(float)
        rho, p, n = spearman(f, y)
        t_top, t_bot, spread, nd = decile_spread(f, y)
        out["horizons"][name] = {
            "spearman_rho": rho, "spearman_p": p, "n": n,
            "top_decile_ret": t_top, "bottom_decile_ret": t_bot,
            "decile_spread": spread, "n_decile": nd,
        }
    return out


# --------------------------------------------------------------------------- #
# Plotting
# --------------------------------------------------------------------------- #
def make_scatter(df: pd.DataFrame, outdir: Path) -> List[Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in HORIZONS_HOURS:
        sub = df[["funding_rate", f"ret_{name}"]].dropna()
        if len(sub) < 8:
            continue
        fig, ax = plt.subplots(figsize=(6, 4.2))
        ax.scatter(sub["funding_rate"] * 1e4, sub[f"ret_{name}"] * 100,
                   s=10, alpha=0.4)
        # Robust linear fit line for visual reference.
        b, a = np.polyfit(sub["funding_rate"] * 1e4, sub[f"ret_{name}"] * 100, 1)
        xs = np.linspace((sub["funding_rate"] * 1e4).min(),
                         (sub["funding_rate"] * 1e4).max(), 50)
        ax.plot(xs, a + b * xs, color="crimson", lw=1.5)
        rho, p, n = spearman(sub["funding_rate"].values,
                             sub[f"ret_{name}"].values)
        ax.set_title(f"Funding vs. {name} fwd BTC return  "
                     f"(rho={rho:.3f}, p={p:.3g}, n={n})")
        ax.set_xlabel("funding rate (bps per 8h)")
        ax.set_ylabel(f"{name} forward return (%)")
        ax.axhline(0, color="grey", lw=0.6)
        ax.axvline(0, color="grey", lw=0.6)
        fig.tight_layout()
        path = outdir / f"funding_scatter_{name}.png"
        fig.savefig(path, dpi=110)
        plt.close(fig)
        paths.append(path)
    return paths


# --------------------------------------------------------------------------- #
# Verdict
# --------------------------------------------------------------------------- #
def decide(in_sample: Dict, oos: Dict, regime_results: List[Dict]) -> Dict:
    """Apply the Fase 3b -> 3c gate. Focus horizon = 24h (per the plan)."""
    h = "24h"
    is_h = in_sample["horizons"][h]
    oos_h = oos["horizons"][h]

    sig_is = np.isfinite(is_h["spearman_p"]) and is_h["spearman_p"] < 0.05
    sig_oos = np.isfinite(oos_h["spearman_p"]) and oos_h["spearman_p"] < 0.05
    # Same sign in both halves?
    same_sign = (np.isfinite(is_h["spearman_rho"]) and np.isfinite(oos_h["spearman_rho"])
                 and np.sign(is_h["spearman_rho"]) == np.sign(oos_h["spearman_rho"]))

    spreads = [is_h["decile_spread"], oos_h["decile_spread"]]
    material = all(np.isfinite(s) and abs(s) >= MIN_DECILE_SPREAD_24H for s in spreads)

    # Robust across regimes: significant 24h Spearman with consistent sign in
    # at least 2 of {bull, bear, sideways} that have enough samples.
    reg_ok = 0
    reg_signs = []
    for rr in regime_results:
        rh = rr["horizons"][h]
        if np.isfinite(rh["spearman_p"]) and rh["spearman_p"] < 0.05 and rh["n"] >= 30:
            reg_ok += 1
            reg_signs.append(np.sign(rh["spearman_rho"]))
    robust = reg_ok >= 2 and len(set(reg_signs)) == 1

    go = bool(sig_is and sig_oos and same_sign and material and robust)
    return {
        "horizon": h,
        "significant_in_sample": bool(sig_is),
        "significant_oos": bool(sig_oos),
        "consistent_sign": bool(same_sign),
        "material_effect": bool(material),
        "min_spread_threshold": MIN_DECILE_SPREAD_24H,
        "observed_spreads_24h": spreads,
        "robust_across_regimes": bool(robust),
        "regimes_passing": reg_ok,
        "verdict": "GO" if go else "NO-GO",
    }


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def fmt_pct(x: float, dp: int = 3) -> str:
    return "n/a" if not np.isfinite(x) else f"{x * 100:.{dp}f}%"


def print_report(funding_src: str, candle_src: str, panel: pd.DataFrame,
                 pctiles: Dict[int, float], overall: Dict, in_sample: Dict,
                 oos: Dict, regime_results: List[Dict], plot_paths: List[Path],
                 verdict: Dict) -> str:
    L: List[str] = []
    w = L.append
    w("# Funding-rate signal analysis (Fase 3b)\n")
    w(f"- funding source: `{funding_src}`")
    w(f"- candle source: `{candle_src}`")
    w(f"- funding settlements analysed: {len(panel)} "
      f"({panel['timestamp'].min()} .. {panel['timestamp'].max()})")
    w(f"- forward returns available up to the end of the candle data; the 7d "
      f"horizon therefore has fewer usable rows.\n")

    w("## Funding-rate percentiles (per 8h settlement)\n")
    w("| pctile | rate | rate (bps) |")
    w("|---|---|---|")
    for p in PERCENTILES:
        v = pctiles[p]
        w(f"| p{p} | {v:.6f} | {v * 1e4:.2f} |")
    w("")
    w(f"\"Extreme\" funding ~ outside [p5, p95] = "
      f"[{pctiles[5] * 1e4:.2f} bps, {pctiles[95] * 1e4:.2f} bps]; "
      f"the median ({pctiles[50] * 1e4:.2f} bps) is the typical carry.\n")

    def hz_table(res: Dict, title: str):
        w(f"### {title}  (n={res['n']})\n")
        w("| horizon | Spearman rho | p-value | n | top-decile ret | "
          "bottom-decile ret | decile spread |")
        w("|---|---|---|---|---|---|---|")
        for name in HORIZONS_HOURS:
            d = res["horizons"][name]
            w(f"| {name} | {d['spearman_rho']:.4f} | {d['spearman_p']:.3g} | "
              f"{d['n']} | {fmt_pct(d['top_decile_ret'])} | "
              f"{fmt_pct(d['bottom_decile_ret'])} | {fmt_pct(d['decile_spread'])} |"
              if np.isfinite(d['spearman_rho']) else
              f"| {name} | n/a | n/a | {d['n']} | n/a | n/a | n/a |")
        w("")

    w("## Correlation: funding vs. subsequent BTC return\n")
    hz_table(overall, "Full sample")
    hz_table(in_sample, "In-sample (earlier half)")
    hz_table(oos, "Out-of-sample (later half)")

    w("## Per-regime breakdown (full sample)\n")
    for rr in regime_results:
        hz_table(rr, f"Regime: {rr['label']}")

    w("## Scatter plots\n")
    for p in plot_paths:
        w(f"- `{p.relative_to(REPO_ROOT)}`")
    w("")

    w("## Decision gate (3b -> 3c)\n")
    w(f"- focus horizon: **{verdict['horizon']}**")
    w(f"- significant in-sample (p<0.05): **{verdict['significant_in_sample']}**")
    w(f"- significant out-of-sample (p<0.05): **{verdict['significant_oos']}**")
    w(f"- consistent sign in/out of sample: **{verdict['consistent_sign']}**")
    w(f"- material effect (|24h decile spread| >= "
      f"{verdict['min_spread_threshold'] * 100:.1f}% in both halves): "
      f"**{verdict['material_effect']}**  "
      f"(observed: {[fmt_pct(s) for s in verdict['observed_spreads_24h']]})")
    w(f"- robust across regimes (>=2 regimes significant, same sign): "
      f"**{verdict['robust_across_regimes']}**  "
      f"(regimes passing: {verdict['regimes_passing']})")
    w(f"\n### VERDICT: **{verdict['verdict']}** for integrating funding into "
      f"Fase 5's risk score.\n")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
def main() -> int:
    funding, funding_src = load_funding()
    candles, candle_src = load_candles()
    print(f"Loaded {len(funding)} funding rows from {funding_src}")
    print(f"Loaded {len(candles)} candles from {candle_src}")

    panel = build_panel(funding, candles)
    if panel.empty:
        print("No overlapping funding/candle data — aborting.", file=sys.stderr)
        return 1

    pctiles = {p: float(np.quantile(panel["funding_rate"], p / 100.0))
               for p in PERCENTILES}

    overall = analyse_subset(panel, "full")
    half = len(panel) // 2
    in_sample = analyse_subset(panel.iloc[:half], "in_sample")
    oos = analyse_subset(panel.iloc[half:], "oos")

    regime_results = []
    for reg in ["bull", "bear", "sideways"]:
        sub = panel[panel["regime"] == reg]
        if len(sub) >= 8:
            regime_results.append(analyse_subset(sub, reg))

    plot_paths = make_scatter(panel, RESULTS_DIR)

    verdict = decide(in_sample, oos, regime_results)

    report = print_report(funding_src, candle_src, panel, pctiles, overall,
                          in_sample, oos, regime_results, plot_paths, verdict)
    print("\n" + report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
