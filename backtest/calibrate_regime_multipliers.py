#!/usr/bin/env python3
"""Fase 4 — walk-forward calibration of the regime risk-multipliers.

Calibrates the ``bull_trend`` / ``bear_trend`` / ``range`` regime risk
multipliers of the advanced strategy by grid-searching a coarse 5x5x5 grid
under walk-forward validation, optimizing for **Calmar ratio** (not raw
return).  ``chop`` and ``unclear`` stay at their config defaults (0.0) and are
out of scope of this phase.

Multiple-testing discipline (López de Prado / Bailey & López de Prado 2014):
- coarse grid only — NO iterative refinement around the winner
- the last 20% of the data is reserved as an untouched holdout and is
  evaluated exactly once at the very end
- the deflated Sharpe ratio is reported alongside the raw Sharpe, accounting
  for the number of trials
- the spread of Calmar/Sharpe across all grid combos is tabulated so a winner
  that is merely a noise outlier is visible as such

Structure mirrors ``backtest/calibrate_per_timeframe.py``: 3 walk-forward
splits, 70/30 train/test.

Usage (from project root):
    python3 -m backtest.calibrate_regime_multipliers
    python3 -m backtest.calibrate_regime_multipliers --csv backtest/data/BTC-USDT_5m.csv
    python3 -m backtest.calibrate_regime_multipliers --quick   # reduced grid, for smoke testing

Output: a JSON report under backtest/results/ and a human-readable summary on
stdout.  Wiring of the recommended multipliers into the bot lives behind the
``regime_multipliers`` config section (default ``enabled: false``).
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))
sys.path.insert(0, PROJECT_ROOT)

from backtest.backtester import Backtester, BacktestConfig
from trading_strategy import create_strategy


# ---------------------------------------------------------------------------
# Grid (coarse — do NOT refine around the winner; that overfits)
# ---------------------------------------------------------------------------

GRID_VALUES = [0.5, 0.75, 1.0, 1.25, 1.5]
QUICK_GRID_VALUES = [0.5, 1.0, 1.5]
REGIME_KEYS = ["bull_trend", "bear_trend", "range"]

# Current production defaults (advanced_strategy.MultiIndicatorConfluence).
CURRENT_MULTIPLIERS = {"bull_trend": 1.0, "bear_trend": 0.8, "range": 0.55}

DEFAULT_CSV = os.path.join("backtest", "data", "BTC-USDT_5m.csv")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_candles(csv_path: str) -> pd.DataFrame:
    full = csv_path if os.path.isabs(csv_path) else os.path.join(PROJECT_ROOT, csv_path)
    df = pd.read_csv(full, parse_dates=["timestamp"])
    return df.sort_values("timestamp").reset_index(drop=True)


def _load_base_strategy_cfg(config_path: Optional[str]) -> Dict:
    """Load the `strategy` section from the bot config so the calibration runs
    against the same indicator parameters production uses.  Missing file ->
    empty dict (strategy defaults)."""
    if not config_path:
        return {}
    full = config_path if os.path.isabs(config_path) else os.path.join(PROJECT_ROOT, config_path)
    if not os.path.exists(full):
        return {}
    try:
        with open(full) as f:
            cfg = json.load(f)
        return dict(cfg.get("strategy", {}) or {})
    except Exception:
        return {}


def _make_bt_config(risk_per_trade_pct: float = 5.0) -> BacktestConfig:
    # Note: the backtester's position sizing uses leverage=1.0 and a 100%
    # notional cap (matching the production bot's risk config).  At
    # risk_per_trade_pct=5 this means the risk-based size frequently exceeds
    # the notional cap, so it gets capped to the same value regardless of the
    # regime risk-multiplier — multipliers 0.5..1.5 then produce *identical*
    # backtests.  Lower risk_per_trade_pct (e.g. 1.0) keeps the risk-based size
    # below the cap so the multipliers actually bite; the calibrator warns when
    # the grid degenerates to a single value.
    return BacktestConfig(
        initial_balance=10000.0,
        fee_rate=0.0006,
        slippage_pct=0.05,
        risk_per_trade_pct=float(risk_per_trade_pct),
        min_confidence=0.45,
        allow_shorts=True,
        lookback_candles=200,
        contract_value=0.001,
        use_risk_multiplier=True,
        use_time_exits=True,
    )


def _strategy_config(base_strategy_cfg: Dict, multipliers: Dict[str, float]) -> Dict:
    cfg = dict(base_strategy_cfg)
    # MTF resampling needs HTF datasets we are not feeding here — disable it so
    # the strategy stays consistent across all grid runs.
    mtf = dict(cfg.get("multi_timeframe", {}) or {})
    mtf["enabled"] = False
    cfg["multi_timeframe"] = mtf
    cfg["regime_multipliers"] = {
        "enabled": True,
        "bull_trend": float(multipliers["bull_trend"]),
        "bear_trend": float(multipliers["bear_trend"]),
        "range": float(multipliers["range"]),
    }
    return cfg


def _run_backtest(df: pd.DataFrame, base_strategy_cfg: Dict,
                  multipliers: Dict[str, float], bt_config: BacktestConfig) -> Dict:
    """Run one backtest with the given regime multipliers; return key metrics."""
    strategy = create_strategy("advanced", _strategy_config(base_strategy_cfg, multipliers))
    backtester = Backtester(strategy, bt_config)
    res = backtester.run(df)
    return {
        "calmar": float(res.calmar_ratio),
        "sharpe_bars": float(res.sharpe_ratio_bars),
        "sharpe_trades": float(res.sharpe_ratio),
        "max_dd_pct": float(res.max_drawdown_pct),
        "total_return_pct": float(res.total_roi),
        "alpha_vs_bh_pct": float(res.alpha_vs_benchmark_pct),
        "trades": int(res.total_trades),
        "cagr": float(res.cagr),
    }


def _deflated_sharpe(observed_sr: float, sr_variance: float, n_trials: int,
                     n_obs: int, skew: float = 0.0, kurt: float = 3.0) -> float:
    """Deflated Sharpe ratio (Bailey & López de Prado, 2014).

    observed_sr   : the (non-annualized) Sharpe of the selected strategy
    sr_variance   : variance of the Sharpe estimates across the N trials
    n_trials      : number of independent trials (here: grid size)
    n_obs         : number of return observations the SR was estimated from
    skew, kurt    : skewness and (non-excess) kurtosis of the return series

    Returns the probability that the true SR is > 0 after accounting for
    selection under multiple testing.  A value < ~0.95 means the result is
    not significant at conventional thresholds.
    """
    if n_trials < 1 or n_obs < 2 or sr_variance <= 0:
        return float("nan")
    # Expected maximum of N iid standard-normal draws (Bailey & LdP eq. for E[max]).
    emc = 0.5772156649015329  # Euler-Mascheroni
    z1 = _norm_ppf(1.0 - 1.0 / n_trials)
    z2 = _norm_ppf(1.0 - 1.0 / (n_trials * math.e))
    expected_max_z = (1.0 - emc) * z1 + emc * z2
    sr0 = math.sqrt(sr_variance) * expected_max_z  # benchmark SR under H0 (the deflation)
    # Standard error of the Sharpe estimator (Lo 2002, with higher moments).
    denom = 1.0 - skew * observed_sr + (kurt - 1.0) / 4.0 * observed_sr ** 2
    if denom <= 0:
        return float("nan")
    se = math.sqrt(denom / (n_obs - 1.0))
    if se <= 0:
        return float("nan")
    return _norm_cdf((observed_sr - sr0) / se)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Inverse standard-normal CDF (Acklam's rational approximation)."""
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1.0 - 0.02425
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1.0)
    if p > phigh:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1.0)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1.0)


def _equity_return_stats(df: pd.DataFrame, base_strategy_cfg: Dict,
                         multipliers: Dict[str, float], bt_config: BacktestConfig):
    """Return (per-bar non-annualized SR, n_obs, skew, kurt) for a run — used
    for the deflated Sharpe.  Reconstructs the per-bar equity returns."""
    strategy = create_strategy("advanced", _strategy_config(base_strategy_cfg, multipliers))
    backtester = Backtester(strategy, bt_config)
    res = backtester.run(df)
    eq = np.asarray(res.equity_curve, dtype=float)
    if len(eq) < 3:
        return 0.0, len(eq), 0.0, 3.0
    with np.errstate(divide="ignore", invalid="ignore"):
        rets = np.where(eq[:-1] != 0, eq[1:] / eq[:-1] - 1.0, 0.0)
    rets = np.nan_to_num(rets)
    sd = float(np.std(rets))
    if sd <= 0:
        return 0.0, len(rets), 0.0, 3.0
    sr = float(np.mean(rets) / sd)
    mu = float(np.mean(rets))
    skew = float(np.mean(((rets - mu) / sd) ** 3))
    kurt = float(np.mean(((rets - mu) / sd) ** 4))
    return sr, len(rets), skew, kurt


# ---------------------------------------------------------------------------
# Walk-forward driver
# ---------------------------------------------------------------------------

def calibrate(csv_path: str, n_splits: int, train_pct: float,
              holdout_pct: float, min_trades: int, quick: bool,
              out_path: Optional[str], config_path: Optional[str] = "config.json",
              risk_per_trade_pct: float = 5.0) -> Dict:
    df_all = _load_candles(csv_path)
    n_total = len(df_all)
    if n_total < 2000:
        raise RuntimeError(f"Not enough candles ({n_total}) for a meaningful calibration")

    bt_config = _make_bt_config(risk_per_trade_pct=risk_per_trade_pct)
    print(f"Backtest sizing: risk_per_trade_pct={risk_per_trade_pct}, leverage=1, notional_cap=100% "
          f"(at risk_per_trade_pct=5 the notional cap binds and multipliers 0.5..1.5 are degenerate; "
          f"use --risk-pct 1.0 for a non-degenerate grid)")
    base_strategy_cfg = _load_base_strategy_cfg(config_path)
    if base_strategy_cfg:
        print(f"Using strategy params from {config_path}: {sorted(base_strategy_cfg.keys())}")
    else:
        print("No config strategy section found — using strategy defaults")

    # --- Holdout split: last `holdout_pct` of the data, untouched until the end.
    holdout_start = int(n_total * (1.0 - holdout_pct))
    df_search = df_all.iloc[:holdout_start].reset_index(drop=True)
    df_holdout = df_all.iloc[holdout_start:].reset_index(drop=True)
    print(f"Data: {n_total} bars total | search = {len(df_search)} bars "
          f"({df_search['timestamp'].iloc[0]} -> {df_search['timestamp'].iloc[-1]}) | "
          f"holdout = {len(df_holdout)} bars "
          f"({df_holdout['timestamp'].iloc[0]} -> {df_holdout['timestamp'].iloc[-1]})")

    grid_values = QUICK_GRID_VALUES if quick else GRID_VALUES
    combos = [dict(zip(REGIME_KEYS, c)) for c in itertools.product(grid_values, repeat=len(REGIME_KEYS))]
    n_trials = len(combos)
    print(f"Grid: {grid_values} per multiplier -> {n_trials} combos "
          f"({'QUICK reduced grid' if quick else 'full 5x5x5'}), "
          f"{n_splits} splits, {train_pct:.0%}/{1-train_pct:.0%} train/test\n")

    # --- Walk-forward over the search portion ---
    window_size = len(df_search) // n_splits
    split_records: List[Dict] = []
    # Accumulate train Calmar per combo across splits (averaged) -> overall winner.
    combo_train_calmar: Dict[Tuple, List[float]] = {tuple(c.values()): [] for c in combos}
    combo_train_sharpe: Dict[Tuple, List[float]] = {tuple(c.values()): [] for c in combos}

    for split in range(n_splits):
        start = split * window_size
        end = len(df_search) if split == n_splits - 1 else min(start + window_size, len(df_search))
        seg = df_search.iloc[start:end].reset_index(drop=True)
        cut = int(len(seg) * train_pct)
        train_df = seg.iloc[:cut].reset_index(drop=True)
        test_df = seg.iloc[cut:].reset_index(drop=True)
        if len(train_df) < bt_config.lookback_candles + 50 or len(test_df) < bt_config.lookback_candles + 20:
            print(f"Split {split+1}/{n_splits}: too small (train={len(train_df)}, test={len(test_df)}) — skipping")
            continue
        print(f"Split {split+1}/{n_splits}: train={len(train_df)} bars, test={len(test_df)} bars")

        best = None  # (calmar, combo, metrics)
        for combo in combos:
            m = _run_backtest(train_df, base_strategy_cfg, combo, bt_config)
            combo_train_calmar[tuple(combo.values())].append(m["calmar"])
            combo_train_sharpe[tuple(combo.values())].append(m["sharpe_bars"])
            if m["trades"] < min_trades:
                continue
            if best is None or m["calmar"] > best[0]:
                best = (m["calmar"], combo, m)

        if best is None:
            print(f"  no combo met min_trades={min_trades} on train — skipping split\n")
            continue

        _, win_combo, train_m = best
        test_m = _run_backtest(test_df, base_strategy_cfg, win_combo, bt_config)
        cur_train = _run_backtest(train_df, base_strategy_cfg, CURRENT_MULTIPLIERS, bt_config)
        cur_test = _run_backtest(test_df, base_strategy_cfg, CURRENT_MULTIPLIERS, bt_config)
        print(f"  split winner: {win_combo}")
        print(f"    train Calmar {train_m['calmar']:+.3f} (Sharpe {train_m['sharpe_bars']:+.2f}, "
              f"maxDD {train_m['max_dd_pct']:.2f}%, ret {train_m['total_return_pct']:+.2f}%, "
              f"alpha {train_m['alpha_vs_bh_pct']:+.2f}%, trades {train_m['trades']})")
        print(f"    test  Calmar {test_m['calmar']:+.3f} (Sharpe {test_m['sharpe_bars']:+.2f}, "
              f"maxDD {test_m['max_dd_pct']:.2f}%, ret {test_m['total_return_pct']:+.2f}%, "
              f"alpha {test_m['alpha_vs_bh_pct']:+.2f}%, trades {test_m['trades']})")
        print(f"    current-multipliers baseline:  train Calmar {cur_train['calmar']:+.3f} | "
              f"test Calmar {cur_test['calmar']:+.3f}\n")
        split_records.append({
            "split": split + 1,
            "winner": win_combo,
            "train": train_m,
            "test": test_m,
            "current_baseline_train": cur_train,
            "current_baseline_test": cur_test,
        })

    # --- Overall winner: combo with highest mean train Calmar across splits ---
    mean_calmar = {k: (float(np.mean(v)) if v else float("-inf")) for k, v in combo_train_calmar.items()}
    overall_key = max(mean_calmar, key=mean_calmar.get)
    overall_combo = dict(zip(REGIME_KEYS, overall_key))
    overall_mean_train_calmar = mean_calmar[overall_key]

    # Spread of train Calmar / Sharpe across all combos (averaged over splits).
    all_mean_calmar = np.array([v for v in mean_calmar.values() if math.isfinite(v)])
    all_mean_sharpe = np.array([float(np.mean(v)) for v in combo_train_sharpe.values() if v])
    calmar_mu, calmar_sd = float(np.mean(all_mean_calmar)), float(np.std(all_mean_calmar))
    sharpe_mu, sharpe_sd = float(np.mean(all_mean_sharpe)), float(np.std(all_mean_sharpe))
    winner_z_calmar = (overall_mean_train_calmar - calmar_mu) / calmar_sd if calmar_sd > 1e-9 else 0.0
    grid_degenerate = calmar_sd < 1e-9
    if grid_degenerate:
        print("\n*** WARNING: every grid combo produced an (essentially) IDENTICAL backtest "
              "(field σ≈0). At risk_per_trade_pct=5 the position notional cap binds, so the "
              "regime multipliers between 0.5 and 1.5 have no effect. The grid is uninformative "
              "here — re-run with --risk-pct 1.0 (or with leverage > 1) for a meaningful "
              "calibration. Verdict will be DO-NOT-ACTIVATE regardless. ***\n")

    # --- Deflated Sharpe for the overall winner, on the full search data ---
    sr_obs, n_obs, skew, kurt = _equity_return_stats(df_search, base_strategy_cfg, overall_combo, bt_config)
    # Variance of (non-annualized, per-bar) Sharpe estimates across trials.
    full_search_combo_sr = []
    for combo in combos:
        sr_c, _, _, _ = _equity_return_stats(df_search, base_strategy_cfg, combo, bt_config)
        full_search_combo_sr.append(sr_c)
    sr_variance = float(np.var(full_search_combo_sr)) if len(full_search_combo_sr) > 1 else 0.0
    dsr = _deflated_sharpe(sr_obs, sr_variance, n_trials, n_obs, skew, kurt)

    # --- Winner & current-multipliers on the full search data + holdout ---
    winner_search = _run_backtest(df_search, base_strategy_cfg, overall_combo, bt_config)
    current_search = _run_backtest(df_search, base_strategy_cfg, CURRENT_MULTIPLIERS, bt_config)
    winner_holdout = _run_backtest(df_holdout, base_strategy_cfg, overall_combo, bt_config)  # <-- evaluated ONCE
    current_holdout = _run_backtest(df_holdout, base_strategy_cfg, CURRENT_MULTIPLIERS, bt_config)

    # 1-sigma band of the winner's train Calmar across splits.
    winner_train_calmars = combo_train_calmar[overall_key]
    train_calmar_mu = float(np.mean(winner_train_calmars)) if winner_train_calmars else 0.0
    train_calmar_sd = float(np.std(winner_train_calmars)) if len(winner_train_calmars) > 1 else 0.0
    holdout_within_1sigma = (
        train_calmar_sd > 0
        and (train_calmar_mu - train_calmar_sd) <= winner_holdout["calmar"] <= (train_calmar_mu + train_calmar_sd)
    )

    # --- Verdict ---
    # Gate: "Na Fase 4 holdout: regime-multiplier winner buiten 1σ van train?
    #        Niet activeren, multipliers ongewijzigd."
    beats_current_holdout = winner_holdout["calmar"] > current_holdout["calmar"]
    winner_is_noise_outlier = abs(winner_z_calmar) > 3.0 or calmar_sd == 0.0
    activate = bool(holdout_within_1sigma and beats_current_holdout
                    and not winner_is_noise_outlier and not grid_degenerate)

    print("=" * 70)
    print("OVERALL WINNER (highest mean train Calmar across splits)")
    print(f"  multipliers: {overall_combo}")
    print(f"  mean train Calmar across splits: {overall_mean_train_calmar:+.3f} "
          f"(z vs {n_trials}-combo field: {winner_z_calmar:+.2f}σ; field μ={calmar_mu:+.3f}, σ={calmar_sd:.3f})")
    print(f"  full search-data: Calmar {winner_search['calmar']:+.3f}, Sharpe(bar) {winner_search['sharpe_bars']:+.2f}, "
          f"maxDD {winner_search['max_dd_pct']:.2f}%, ret {winner_search['total_return_pct']:+.2f}%, "
          f"alpha {winner_search['alpha_vs_bh_pct']:+.2f}%, trades {winner_search['trades']}")
    print(f"  current ({CURRENT_MULTIPLIERS}) search-data: Calmar {current_search['calmar']:+.3f}, "
          f"ret {current_search['total_return_pct']:+.2f}%")
    print(f"  Sharpe (non-annualized, per-bar): {sr_obs:.4f} over {n_obs} obs | "
          f"Deflated Sharpe (P[SR>0] after {n_trials} trials): "
          f"{'n/a' if math.isnan(dsr) else f'{dsr:.3f}'}")
    print(f"  HOLDOUT (evaluated once): winner Calmar {winner_holdout['calmar']:+.3f}, "
          f"Sharpe(bar) {winner_holdout['sharpe_bars']:+.2f}, maxDD {winner_holdout['max_dd_pct']:.2f}%, "
          f"ret {winner_holdout['total_return_pct']:+.2f}%, alpha {winner_holdout['alpha_vs_bh_pct']:+.2f}%, "
          f"trades {winner_holdout['trades']}")
    print(f"  HOLDOUT current-multipliers: Calmar {current_holdout['calmar']:+.3f}, "
          f"ret {current_holdout['total_return_pct']:+.2f}%")
    print(f"  winner train Calmar: μ={train_calmar_mu:+.3f}, σ={train_calmar_sd:.3f} -> "
          f"holdout {'WITHIN' if holdout_within_1sigma else 'OUTSIDE'} 1σ of train")
    print(f"\n  VERDICT: {'ACTIVATE' if activate else 'DO NOT ACTIVATE — keep current multipliers'}")
    print("=" * 70)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "phase": "Fase 4 — walk-forward calibration of regime risk-multipliers",
        "data": {
            "csv": csv_path,
            "config": config_path,
            "strategy_params": base_strategy_cfg,
            "n_total_bars": n_total,
            "search_bars": len(df_search),
            "holdout_bars": len(df_holdout),
            "search_period": [str(df_search["timestamp"].iloc[0]), str(df_search["timestamp"].iloc[-1])],
            "holdout_period": [str(df_holdout["timestamp"].iloc[0]), str(df_holdout["timestamp"].iloc[-1])],
        },
        "grid": {"values": grid_values, "regime_keys": REGIME_KEYS, "n_trials": n_trials, "quick": quick},
        "walk_forward": {"n_splits": n_splits, "train_pct": train_pct, "min_trades": min_trades},
        "sizing": {"risk_per_trade_pct": risk_per_trade_pct, "leverage": 1.0,
                   "max_position_notional_pct": 100.0},
        "grid_degenerate": bool(grid_degenerate),
        "objective": "max Calmar ratio on train portion",
        "splits": split_records,
        "overall_winner": {
            "multipliers": overall_combo,
            "mean_train_calmar": overall_mean_train_calmar,
            "train_calmar_per_split": winner_train_calmars,
            "train_calmar_mu": train_calmar_mu,
            "train_calmar_sigma": train_calmar_sd,
            "z_vs_field_calmar": winner_z_calmar,
            "search_data_metrics": winner_search,
            "deflated_sharpe": None if math.isnan(dsr) else dsr,
            "sharpe_non_annualized": sr_obs,
            "n_obs": n_obs,
        },
        "field_spread": {
            "calmar_mean": calmar_mu, "calmar_sigma": calmar_sd,
            "calmar_min": float(np.min(all_mean_calmar)) if all_mean_calmar.size else None,
            "calmar_max": float(np.max(all_mean_calmar)) if all_mean_calmar.size else None,
            "sharpe_mean": sharpe_mu, "sharpe_sigma": sharpe_sd,
            "per_combo_mean_train_calmar": {
                ",".join(f"{x:g}" for x in k): (None if not math.isfinite(v) else v)
                for k, v in mean_calmar.items()
            },
        },
        "current_multipliers": CURRENT_MULTIPLIERS,
        "current_search_data_metrics": current_search,
        "holdout": {
            "winner_metrics": winner_holdout,
            "current_metrics": current_holdout,
            "winner_within_1sigma_of_train": bool(holdout_within_1sigma),
            "winner_beats_current": bool(beats_current_holdout),
        },
        "verdict": {
            "activate": activate,
            "reason": (
                "holdout within 1σ of train, beats current multipliers, and winner "
                "is not a noise outlier" if activate else
                ("grid degenerate (notional cap binds at this risk_per_trade_pct — "
                 "multipliers have no effect); keep current multipliers, "
                 "regime_multipliers.enabled stays false" if grid_degenerate else
                 "gate not satisfied (holdout outside 1σ of train, and/or does not beat "
                 "current multipliers, and/or winner indistinguishable from noise field) "
                 "— keep current multipliers, regime_multipliers.enabled stays false")
            ),
            "recommended_config_section": {
                "regime_multipliers": {
                    "enabled": False,
                    "bull_trend": overall_combo["bull_trend"],
                    "bear_trend": overall_combo["bear_trend"],
                    "range": overall_combo["range"],
                }
            },
        },
    }

    if out_path is None:
        out_path = os.path.join("backtest", "results",
                                f"regime_multiplier_calibration_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    full_out = out_path if os.path.isabs(out_path) else os.path.join(PROJECT_ROOT, out_path)
    Path(full_out).parent.mkdir(parents=True, exist_ok=True)
    with open(full_out, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nReport written to {full_out}")
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--csv", default=DEFAULT_CSV, help=f"5m candle CSV (default: {DEFAULT_CSV})")
    p.add_argument("--config", default="config.json",
                   help="Bot config; its `strategy` section sets indicator params (default: config.json)")
    p.add_argument("--n-splits", type=int, default=3, help="Walk-forward splits (default: 3)")
    p.add_argument("--train-pct", type=float, default=0.70, help="Train fraction per split (default: 0.70)")
    p.add_argument("--holdout-pct", type=float, default=0.20,
                   help="Fraction of data reserved as untouched holdout (default: 0.20)")
    p.add_argument("--min-trades", type=int, default=5, help="Min trades on train for a valid combo")
    p.add_argument("--risk-pct", type=float, default=5.0,
                   help="risk_per_trade_pct for the backtest (default 5.0 = production; use 1.0 to "
                        "avoid the notional-cap degeneracy where multipliers have no effect)")
    p.add_argument("--quick", action="store_true",
                   help="Reduced 3x3x3 grid for smoke-testing the pipeline (NOT a real calibration)")
    p.add_argument("--out", default=None, help="Output JSON path (default: backtest/results/regime_multiplier_calibration_<ts>.json)")
    args = p.parse_args()

    try:
        calibrate(csv_path=args.csv, n_splits=args.n_splits, train_pct=args.train_pct,
                  holdout_pct=args.holdout_pct, min_trades=args.min_trades,
                  quick=args.quick, out_path=args.out, config_path=args.config,
                  risk_per_trade_pct=args.risk_pct)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
