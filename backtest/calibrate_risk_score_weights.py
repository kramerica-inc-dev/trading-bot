#!/usr/bin/env python3
"""Fase 5 — walk-forward calibration of the continuous-risk-score component weights.

Calibrates the four component weights of ``scripts/risk_scoring.py``
(``confidence`` / ``quality_score`` / ``regime_confidence`` / ``mtf_alignment``)
by grid-searching a *coarse* grid under walk-forward validation, optimizing for
**Calmar ratio** (not raw return).  Funding rate is NOT a component — it was a
NO-GO in Fase 3 (see ``docs/funding-analysis.md``).

Per IMPROVEMENT_PLAN.md §"Gewichten bepalen": **option A** (equal-weights
startpoint + coarse walk-forward grid).  Option B (Ridge/Lasso regression) is
explicitly *not* done — only if A doesn't converge, and given Fase 4's
experience the objective is likely flat anyway; if so we just report that and
keep equal weights.

Multiple-testing discipline (López de Prado / Bailey & López de Prado 2014),
mirroring ``backtest/calibrate_regime_multipliers.py``:
- coarse grid only — NO iterative refinement around the winner
- the last 20% of the data is reserved as an untouched holdout, evaluated once
- the deflated Sharpe ratio is reported alongside the raw Sharpe
- the spread of Calmar/Sharpe across all grid combos is tabulated so a winner
  that is merely a noise outlier is visible as such
- structure: 3 walk-forward splits, 80/20 search/holdout, 70/30 train/test

Usage (from project root):
    python3 -m backtest.calibrate_risk_score_weights
    python3 -m backtest.calibrate_risk_score_weights --csv backtest/data/BTC-USDT_5m.csv
    python3 -m backtest.calibrate_risk_score_weights --quick   # reduced grid, smoke test

Output: a JSON report under backtest/results/ + a human-readable summary on
stdout.  Wiring of the recommended weights into the bot lives behind the
``risk_scoring`` config section (default ``enabled: false``).
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
# Reuse the deflated-Sharpe machinery from the Fase 4 calibrator.
from backtest.calibrate_regime_multipliers import _deflated_sharpe  # noqa: E402

COMPONENT_KEYS = ["confidence", "quality_score", "regime_confidence", "mtf_alignment"]

# Coarse grid of per-component weights (un-normalized; risk_scoring normalizes
# them to sum to 1).  Keeping it small to limit multiple-testing exposure: the
# full product is 3**4 = 81 combos, the all-zero combo is dropped -> 80.
GRID_VALUES = [0.0, 1.0, 2.0]
QUICK_GRID_VALUES = [0.0, 1.0]

# Equal weights — the option-A startpoint and the production default.
EQUAL_WEIGHTS = {k: 1.0 for k in COMPONENT_KEYS}

DEFAULT_CSV = os.path.join("backtest", "data", "BTC-USDT_5m.csv")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_candles(csv_path: str) -> pd.DataFrame:
    full = csv_path if os.path.isabs(csv_path) else os.path.join(PROJECT_ROOT, csv_path)
    df = pd.read_csv(full, parse_dates=["timestamp"])
    return df.sort_values("timestamp").reset_index(drop=True)


def _load_base_strategy_cfg(config_path: Optional[str]) -> Dict:
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


def _make_bt_config(risk_per_trade_pct: float = 1.0) -> BacktestConfig:
    # Note (same caveat as the Fase 4 calibrator): with the 100% notional cap,
    # at risk_per_trade_pct=5 the cap binds and the position-size multiplier has
    # no effect; risk_per_trade_pct=1.0 keeps risk-based size below the cap so
    # the score->size mapping actually bites.  The calibrator warns if the grid
    # degenerates to a single value regardless.
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


def _strategy_config(base_strategy_cfg: Dict, weights: Optional[Dict[str, float]],
                     risk_scoring_enabled: bool) -> Dict:
    cfg = dict(base_strategy_cfg)
    # MTF resampling needs HTF datasets we are not feeding here — disable it so
    # the strategy stays consistent across all grid runs (same as Fase 4).
    mtf = dict(cfg.get("multi_timeframe", {}) or {})
    mtf["enabled"] = False
    cfg["multi_timeframe"] = mtf
    if risk_scoring_enabled:
        rs: Dict = {"enabled": True}
        if weights is not None:
            rs["weights"] = {k: float(weights[k]) for k in COMPONENT_KEYS}
        cfg["risk_scoring"] = rs
    else:
        cfg["risk_scoring"] = {"enabled": False}
    return cfg


def _run_backtest(df: pd.DataFrame, base_strategy_cfg: Dict,
                  weights: Optional[Dict[str, float]], risk_scoring_enabled: bool,
                  bt_config: BacktestConfig) -> Dict:
    strategy = create_strategy("advanced", _strategy_config(base_strategy_cfg, weights, risk_scoring_enabled))
    backtester = Backtester(strategy, bt_config)
    res = backtester.run(df)
    sizes = [float(t.size) for t in res.trades] if res.trades else []
    return {
        "calmar": float(res.calmar_ratio),
        "sharpe_bars": float(res.sharpe_ratio_bars),
        "sharpe_trades": float(res.sharpe_ratio),
        "max_dd_pct": float(res.max_drawdown_pct),
        "total_return_pct": float(res.total_roi),
        "alpha_vs_bh_pct": float(res.alpha_vs_benchmark_pct),
        "win_rate": float(res.win_rate),
        "trades": int(res.total_trades),
        "avg_trade_size": float(np.mean(sizes)) if sizes else 0.0,
        "cagr": float(res.cagr),
    }


def _equity_return_stats(df: pd.DataFrame, base_strategy_cfg: Dict,
                         weights: Optional[Dict[str, float]], risk_scoring_enabled: bool,
                         bt_config: BacktestConfig):
    strategy = create_strategy("advanced", _strategy_config(base_strategy_cfg, weights, risk_scoring_enabled))
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


def _norm_weights(w: Dict[str, float]) -> Dict[str, float]:
    total = sum(w.values())
    if total <= 0:
        return dict(EQUAL_WEIGHTS)
    return {k: v / total for k, v in w.items()}


# ---------------------------------------------------------------------------
# Walk-forward driver
# ---------------------------------------------------------------------------

def calibrate(csv_path: str, n_splits: int, train_pct: float, holdout_pct: float,
              min_trades: int, quick: bool, out_path: Optional[str],
              config_path: Optional[str] = "config.json",
              risk_per_trade_pct: float = 1.0, max_bars: Optional[int] = None) -> Dict:
    df_all = _load_candles(csv_path)
    if max_bars is not None and len(df_all) > max_bars:
        # Use the most-recent `max_bars` candles.  The full 106k-bar 5m series
        # makes the 80-combo grid prohibitively slow (~90 min) because the
        # deflated-Sharpe variance loop re-runs a full search-data backtest per
        # combo; a recent ~25-40k-bar window keeps the *coarse* grid honest
        # while finishing in minutes.
        df_all = df_all.iloc[-max_bars:].reset_index(drop=True)
        print(f"Restricted to the most recent {max_bars} candles "
              f"({df_all['timestamp'].iloc[0]} -> {df_all['timestamp'].iloc[-1]})")
    n_total = len(df_all)
    if n_total < 2000:
        raise RuntimeError(f"Not enough candles ({n_total}) for a meaningful calibration")

    bt_config = _make_bt_config(risk_per_trade_pct=risk_per_trade_pct)
    base_strategy_cfg = _load_base_strategy_cfg(config_path)
    if base_strategy_cfg:
        print(f"Using strategy params from {config_path}: {sorted(base_strategy_cfg.keys())}")
    else:
        print("No config strategy section found — using strategy defaults")

    holdout_start = int(n_total * (1.0 - holdout_pct))
    df_search = df_all.iloc[:holdout_start].reset_index(drop=True)
    df_holdout = df_all.iloc[holdout_start:].reset_index(drop=True)
    print(f"Data: {n_total} bars total | search = {len(df_search)} bars "
          f"({df_search['timestamp'].iloc[0]} -> {df_search['timestamp'].iloc[-1]}) | "
          f"holdout = {len(df_holdout)} bars "
          f"({df_holdout['timestamp'].iloc[0]} -> {df_holdout['timestamp'].iloc[-1]})")

    grid_values = QUICK_GRID_VALUES if quick else GRID_VALUES
    raw_combos = [dict(zip(COMPONENT_KEYS, c)) for c in itertools.product(grid_values, repeat=len(COMPONENT_KEYS))]
    combos = [c for c in raw_combos if sum(c.values()) > 0.0]  # drop the all-zero combo
    n_trials = len(combos)
    print(f"Grid: {grid_values} per component -> {n_trials} combos "
          f"({'QUICK reduced grid' if quick else 'coarse'}), "
          f"{n_splits} splits, {train_pct:.0%}/{1-train_pct:.0%} train/test")
    print(f"Backtest sizing: risk_per_trade_pct={risk_per_trade_pct}, leverage=1, notional_cap=100% "
          f"(at risk_per_trade_pct=5 the cap binds and the score->size multiplier has no effect; "
          f"use --risk-pct 1.0 for a non-degenerate grid)\n")

    window_size = len(df_search) // n_splits
    split_records: List[Dict] = []
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

        best = None
        for combo in combos:
            m = _run_backtest(train_df, base_strategy_cfg, combo, True, bt_config)
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
        test_m = _run_backtest(test_df, base_strategy_cfg, win_combo, True, bt_config)
        equal_train = _run_backtest(train_df, base_strategy_cfg, EQUAL_WEIGHTS, True, bt_config)
        equal_test = _run_backtest(test_df, base_strategy_cfg, EQUAL_WEIGHTS, True, bt_config)
        print(f"  split winner weights (normalized): {_norm_weights(win_combo)}")
        print(f"    train Calmar {train_m['calmar']:+.3f} (Sharpe {train_m['sharpe_bars']:+.2f}, "
              f"maxDD {train_m['max_dd_pct']:.2f}%, ret {train_m['total_return_pct']:+.2f}%, "
              f"trades {train_m['trades']}, avg size {train_m['avg_trade_size']:.1f})")
        print(f"    test  Calmar {test_m['calmar']:+.3f} (Sharpe {test_m['sharpe_bars']:+.2f}, "
              f"maxDD {test_m['max_dd_pct']:.2f}%, ret {test_m['total_return_pct']:+.2f}%, "
              f"trades {test_m['trades']}, avg size {test_m['avg_trade_size']:.1f})")
        print(f"    equal-weights baseline:  train Calmar {equal_train['calmar']:+.3f} | "
              f"test Calmar {equal_test['calmar']:+.3f}\n")
        split_records.append({
            "split": split + 1, "winner_weights": _norm_weights(win_combo),
            "train": train_m, "test": test_m,
            "equal_weights_train": equal_train, "equal_weights_test": equal_test,
        })

    mean_calmar = {k: (float(np.mean(v)) if v else float("-inf")) for k, v in combo_train_calmar.items()}
    overall_key = max(mean_calmar, key=mean_calmar.get)
    overall_combo = dict(zip(COMPONENT_KEYS, overall_key))
    overall_norm = _norm_weights(overall_combo)
    overall_mean_train_calmar = mean_calmar[overall_key]

    all_mean_calmar = np.array([v for v in mean_calmar.values() if math.isfinite(v)])
    all_mean_sharpe = np.array([float(np.mean(v)) for v in combo_train_sharpe.values() if v])
    calmar_mu, calmar_sd = float(np.mean(all_mean_calmar)), float(np.std(all_mean_calmar))
    sharpe_mu, sharpe_sd = float(np.mean(all_mean_sharpe)), float(np.std(all_mean_sharpe))
    winner_z_calmar = (overall_mean_train_calmar - calmar_mu) / calmar_sd if calmar_sd > 1e-9 else 0.0
    grid_degenerate = calmar_sd < 1e-9
    objective_flat = calmar_sd < 0.05  # heuristic: spread tiny -> objective effectively flat (cf. Fase 4)
    if grid_degenerate:
        print("\n*** WARNING: every grid combo produced an (essentially) IDENTICAL backtest "
              "(field σ≈0). The position notional cap likely binds at this risk_per_trade_pct, "
              "so the score->size multiplier has no effect. Re-run with --risk-pct 1.0 (or lower). "
              "Verdict will be DO-NOT-ACTIVATE regardless. ***\n")
    elif objective_flat:
        print("\n*** NOTE: the Calmar objective is essentially flat across the grid "
              f"(field σ={calmar_sd:.3f}). Per the plan, do NOT do option B (Ridge/Lasso) — "
              "report the flat objective and keep EQUAL weights. ***\n")

    # Deflated Sharpe for the overall winner on the full search data.
    sr_obs, n_obs, skew, kurt = _equity_return_stats(df_search, base_strategy_cfg, overall_combo, True, bt_config)
    full_search_combo_sr = []
    for combo in combos:
        sr_c, _, _, _ = _equity_return_stats(df_search, base_strategy_cfg, combo, True, bt_config)
        full_search_combo_sr.append(sr_c)
    sr_variance = float(np.var(full_search_combo_sr)) if len(full_search_combo_sr) > 1 else 0.0
    dsr = _deflated_sharpe(sr_obs, sr_variance, n_trials, n_obs, skew, kurt)

    winner_search = _run_backtest(df_search, base_strategy_cfg, overall_combo, True, bt_config)
    equal_search = _run_backtest(df_search, base_strategy_cfg, EQUAL_WEIGHTS, True, bt_config)
    winner_holdout = _run_backtest(df_holdout, base_strategy_cfg, overall_combo, True, bt_config)  # ONCE
    equal_holdout = _run_backtest(df_holdout, base_strategy_cfg, EQUAL_WEIGHTS, True, bt_config)
    binary_search = _run_backtest(df_search, base_strategy_cfg, None, False, bt_config)
    binary_holdout = _run_backtest(df_holdout, base_strategy_cfg, None, False, bt_config)

    winner_train_calmars = combo_train_calmar[overall_key]
    train_calmar_mu = float(np.mean(winner_train_calmars)) if winner_train_calmars else 0.0
    train_calmar_sd = float(np.std(winner_train_calmars)) if len(winner_train_calmars) > 1 else 0.0
    holdout_within_1sigma = (
        train_calmar_sd > 0
        and (train_calmar_mu - train_calmar_sd) <= winner_holdout["calmar"] <= (train_calmar_mu + train_calmar_sd)
    )

    # --- Recommendation ---
    # Per the plan: A first; if the objective is flat (Fase-4 experience),
    # report it and keep EQUAL weights — do NOT escalate to option B.
    winner_is_noise_outlier = abs(winner_z_calmar) > 3.0 or calmar_sd == 0.0
    beats_equal_holdout = winner_holdout["calmar"] > equal_holdout["calmar"]
    use_calibrated_weights = bool(holdout_within_1sigma and beats_equal_holdout
                                  and not winner_is_noise_outlier and not grid_degenerate
                                  and not objective_flat)
    recommended_weights = overall_norm if use_calibrated_weights else _norm_weights(EQUAL_WEIGHTS)

    print("=" * 72)
    print("OVERALL WINNER (highest mean train Calmar across splits)")
    print(f"  weights (un-normalized): {overall_combo}")
    print(f"  weights (normalized):    {overall_norm}")
    print(f"  mean train Calmar across splits: {overall_mean_train_calmar:+.3f} "
          f"(z vs {n_trials}-combo field: {winner_z_calmar:+.2f}σ; field μ={calmar_mu:+.3f}, σ={calmar_sd:.3f})")
    print(f"  full search-data: Calmar {winner_search['calmar']:+.3f}, Sharpe(bar) {winner_search['sharpe_bars']:+.2f}, "
          f"maxDD {winner_search['max_dd_pct']:.2f}%, ret {winner_search['total_return_pct']:+.2f}%, "
          f"trades {winner_search['trades']}, avg size {winner_search['avg_trade_size']:.1f}")
    print(f"  equal-weights search-data: Calmar {equal_search['calmar']:+.3f}, ret {equal_search['total_return_pct']:+.2f}%")
    print(f"  Sharpe (non-annualized, per-bar): {sr_obs:.4f} over {n_obs} obs | "
          f"Deflated Sharpe (P[SR>0] after {n_trials} trials): {'n/a' if math.isnan(dsr) else f'{dsr:.3f}'}")
    print(f"  HOLDOUT (evaluated once): winner Calmar {winner_holdout['calmar']:+.3f}, "
          f"maxDD {winner_holdout['max_dd_pct']:.2f}%, ret {winner_holdout['total_return_pct']:+.2f}%, "
          f"trades {winner_holdout['trades']}, avg size {winner_holdout['avg_trade_size']:.1f}")
    print(f"  HOLDOUT equal-weights:    Calmar {equal_holdout['calmar']:+.3f}, ret {equal_holdout['total_return_pct']:+.2f}%")
    print(f"  HOLDOUT binary gates:     Calmar {binary_holdout['calmar']:+.3f}, ret {binary_holdout['total_return_pct']:+.2f}%, "
          f"trades {binary_holdout['trades']}, avg size {binary_holdout['avg_trade_size']:.1f}")
    print(f"  winner train Calmar: μ={train_calmar_mu:+.3f}, σ={train_calmar_sd:.3f} -> "
          f"holdout {'WITHIN' if holdout_within_1sigma else 'OUTSIDE'} 1σ of train")
    print(f"\n  RECOMMENDED WEIGHTS: {recommended_weights}")
    print(f"  ({'calibrated winner' if use_calibrated_weights else 'EQUAL weights — objective flat / gate not satisfied'})")
    print("=" * 72)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "phase": "Fase 5 — walk-forward calibration of continuous-risk-score component weights",
        "data": {
            "csv": csv_path, "config": config_path, "strategy_params": base_strategy_cfg,
            "n_total_bars": n_total, "search_bars": len(df_search), "holdout_bars": len(df_holdout),
            "search_period": [str(df_search["timestamp"].iloc[0]), str(df_search["timestamp"].iloc[-1])],
            "holdout_period": [str(df_holdout["timestamp"].iloc[0]), str(df_holdout["timestamp"].iloc[-1])],
        },
        "grid": {"values": grid_values, "component_keys": COMPONENT_KEYS, "n_trials": n_trials, "quick": quick},
        "walk_forward": {"n_splits": n_splits, "train_pct": train_pct, "min_trades": min_trades},
        "sizing": {"risk_per_trade_pct": risk_per_trade_pct, "leverage": 1.0, "max_position_notional_pct": 100.0},
        "grid_degenerate": bool(grid_degenerate), "objective_flat": bool(objective_flat),
        "objective": "max Calmar ratio on train portion",
        "option_b_done": False,
        "option_b_note": ("Option B (Ridge/Lasso) intentionally NOT done — per the plan it is only "
                          "warranted if A doesn't converge; with a flat/near-flat objective (Fase 4 "
                          "experience) escalating to B would just overfit. Equal weights retained."),
        "splits": split_records,
        "overall_winner": {
            "weights_unnormalized": overall_combo, "weights_normalized": overall_norm,
            "mean_train_calmar": overall_mean_train_calmar, "train_calmar_per_split": winner_train_calmars,
            "train_calmar_mu": train_calmar_mu, "train_calmar_sigma": train_calmar_sd,
            "z_vs_field_calmar": winner_z_calmar, "search_data_metrics": winner_search,
            "deflated_sharpe": None if math.isnan(dsr) else dsr,
            "sharpe_non_annualized": sr_obs, "n_obs": n_obs,
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
        "equal_weights": _norm_weights(EQUAL_WEIGHTS),
        "equal_weights_search_metrics": equal_search,
        "binary_gates_search_metrics": binary_search,
        "holdout": {
            "winner_metrics": winner_holdout, "equal_weights_metrics": equal_holdout,
            "binary_gates_metrics": binary_holdout,
            "winner_within_1sigma_of_train": bool(holdout_within_1sigma),
            "winner_beats_equal_weights": bool(beats_equal_holdout),
        },
        "recommendation": {
            "use_calibrated_weights": use_calibrated_weights,
            "recommended_weights": recommended_weights,
            "reason": ("calibrated winner survives the holdout/noise gate and beats equal weights"
                       if use_calibrated_weights else
                       ("grid degenerate (notional cap binds) — keep equal weights, risk_scoring.enabled stays false"
                        if grid_degenerate else
                        ("Calmar objective flat across the grid (cf. Fase 4) — keep equal weights"
                         if objective_flat else
                         "gate not satisfied (holdout outside 1σ of train and/or does not beat equal weights "
                         "and/or winner indistinguishable from noise) — keep equal weights"))),
            "recommended_config_section": {
                "risk_scoring": {
                    "enabled": False,
                    "weights": recommended_weights,
                    "size_slope": 2.0, "size_intercept": -0.5,
                }
            },
        },
    }

    if out_path is None:
        out_path = os.path.join("backtest", "results",
                                f"risk_score_weights_calibration_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    full_out = out_path if os.path.isabs(out_path) else os.path.join(PROJECT_ROOT, out_path)
    Path(full_out).parent.mkdir(parents=True, exist_ok=True)
    with open(full_out, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nReport written to {full_out}")
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--csv", default=DEFAULT_CSV, help=f"5m candle CSV (default: {DEFAULT_CSV})")
    p.add_argument("--config", default="config.json",
                   help="Bot config; its `strategy` section sets indicator params (default: config.json)")
    p.add_argument("--n-splits", type=int, default=3, help="Walk-forward splits (default: 3)")
    p.add_argument("--train-pct", type=float, default=0.70, help="Train fraction per split (default: 0.70)")
    p.add_argument("--holdout-pct", type=float, default=0.20, help="Untouched holdout fraction (default: 0.20)")
    p.add_argument("--min-trades", type=int, default=5, help="Min trades on train for a valid combo")
    p.add_argument("--risk-pct", type=float, default=1.0,
                   help="risk_per_trade_pct for the backtest (default 1.0 = keeps size below the "
                        "notional cap so the score->size multiplier bites; 5.0 = production but degenerate)")
    p.add_argument("--quick", action="store_true", help="Reduced grid for smoke-testing (NOT a real calibration)")
    p.add_argument("--max-bars", type=int, default=None,
                   help="Cap the data to the most-recent N candles (the full 5m series makes the "
                        "80-combo grid take ~90 min; ~30000 keeps the coarse grid honest in minutes)")
    p.add_argument("--out", default=None, help="Output JSON path")
    args = p.parse_args()
    try:
        calibrate(csv_path=args.csv, n_splits=args.n_splits, train_pct=args.train_pct,
                  holdout_pct=args.holdout_pct, min_trades=args.min_trades, quick=args.quick,
                  out_path=args.out, config_path=args.config, risk_per_trade_pct=args.risk_pct,
                  max_bars=args.max_bars)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
