#!/usr/bin/env python3
"""Calibrate strategy parameters per timeframe.

For each configured timeframe (5m, 15m, 1h), runs walk-forward optimization
over a sensible parameter grid and writes the best profile to a JSON file
that the running bot can load at startup.

Output file format:
{
  "generated_at": "2026-04-17T...",
  "source": {
    "inst_id": "BTC-USDT",
    "days": 90,
    "base_timeframe": "5m"
  },
  "profiles": {
    "5m":  { "rsi_period": 14, "trend_strength_threshold": 0.0018, ... },
    "15m": { "rsi_period": 10, "trend_strength_threshold": 0.0012, ... },
    "1h":  { "rsi_period": 8,  "trend_strength_threshold": 0.0008, ... }
  },
  "metrics": {
    "5m":  { "oos_sharpe": 1.24, "oos_roi": 8.5, "oos_trades": 45, ... },
    ...
  }
}

Usage:
    python -m backtest.calibrate_per_timeframe \\
        --config config.json \\
        --days 90 \\
        --out memory/timeframe_profiles.json

    # Dry run without writing output:
    python -m backtest.calibrate_per_timeframe --days 90 --dry-run

    # Only calibrate specific TFs:
    python -m backtest.calibrate_per_timeframe --timeframes 15m 1h

Design notes:
- We fetch 5m candles live once and resample to 15m/1h. This matches how
  the bot behaves in production (all candles come from the same source
  and are already consistent), and saves API round trips.
- Parameter grids are TF-specific: shorter lookback periods on slower
  TFs keeps the effective lookback in wall-clock minutes comparable.
- Walk-forward with 3 splits and 70/30 train/test is the default. This
  is aggressive but safer than full-sample optimization.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))
sys.path.insert(0, PROJECT_ROOT)

from backtest.backtester import BacktestConfig
from backtest.data_collector import DataCollector
from backtest.optimizer import ParameterOptimizer


# ---------------------------------------------------------------------------
# Parameter grids per timeframe
# ---------------------------------------------------------------------------
#
# Five TF-sensitive parameters. Values are chosen so that the effective
# lookback in wall-clock minutes is roughly comparable across timeframes:
#     5m  ->  rsi_period 14  =  70 min
#     15m ->  rsi_period 10  = 150 min  (same order of magnitude for 15m bars)
#     1h  ->  rsi_period 8   = 480 min
# Thresholds are looser on slower TFs because price moves are larger in %
# terms per bar.
#
# Keep each grid small: 3 values x 5 params = 243 combos per TF, which on
# walk-forward with 3 splits is ~730 backtests per TF. At ~2s each, that
# is ~25 min per TF — manageable.

PARAM_GRIDS: Dict[str, Dict[str, List]] = {
    "5m": {
        "regime__trend_strength_threshold": [0.0008, 0.0014, 0.0020],
        "regime__efficiency_trend_threshold": [0.10, 0.18, 0.26],
        "min_confidence": [0.35, 0.45, 0.55],
        "regime__anchor_slope_threshold": [0.0006, 0.0010, 0.0015],
    },
    "15m": {
        "regime__trend_strength_threshold": [0.0006, 0.0010, 0.0016],
        "regime__efficiency_trend_threshold": [0.10, 0.18, 0.28],
        "min_confidence": [0.35, 0.45, 0.55],
        "regime__anchor_slope_threshold": [0.0004, 0.0008, 0.0012],
    },
    "1h": {
        "regime__trend_strength_threshold": [0.0004, 0.0008, 0.0012],
        "regime__efficiency_trend_threshold": [0.10, 0.18, 0.26],
        "min_confidence": [0.40, 0.50, 0.60],
        "regime__anchor_slope_threshold": [0.0003, 0.0006, 0.0010],
    },
}

SUPPORTED_CALIBRATION_TFS = list(PARAM_GRIDS.keys())


# ---------------------------------------------------------------------------
# Data loading / resampling
# ---------------------------------------------------------------------------

def _load_exchange_credentials(config_path: str) -> Dict:
    full_path = os.path.join(PROJECT_ROOT, config_path)
    with open(full_path) as f:
        cfg = json.load(f)
    exchange = cfg.get("exchange", "blofin")
    return cfg.get(exchange, {}), cfg.get("strategy", {}), cfg.get("trading_pair", "BTC-USDT")


def _build_api(creds: Dict):
    """Lazily construct the BloFin REST client."""
    from blofin_api import BlofinAPI
    return BlofinAPI(
        api_key=creds.get("api_key", ""),
        api_secret=creds.get("api_secret", ""),
        passphrase=creds.get("passphrase", ""),
        demo_mode=bool(creds.get("demo_mode", False)),
    )


def _resample_ohlcv(df_5m: pd.DataFrame, target_tf: str) -> pd.DataFrame:
    """Resample 5m OHLCV candles into a higher timeframe.

    Uses pandas' resample with OHLC aggregation rules. This is identical
    in spirit to what HTFCandleSync does internally for live data.
    """
    rule_map = {"5m": "5min", "15m": "15min", "30m": "30min",
                "1h": "1h", "4h": "4h", "1d": "1D"}
    if target_tf == "5m":
        return df_5m.copy()
    if target_tf not in rule_map:
        raise ValueError(f"Unsupported resample target: {target_tf}")

    df = df_5m.set_index("timestamp")
    rule = rule_map[target_tf]
    agg = df.resample(rule, label="left", closed="left").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna().reset_index()
    return agg


def _fetch_base_data(inst_id: str, days: int, creds: Dict,
                     force_refresh: bool) -> pd.DataFrame:
    """Fetch 5m candles. 15m and 1h are derived by resampling."""
    api = _build_api(creds)
    collector = DataCollector(api)
    print(f"Fetching {days} days of 5m candles for {inst_id}...")
    df = collector.get_data(inst_id=inst_id, bar="5m", days=days,
                             force_refresh=force_refresh)
    print(f"  Got {len(df)} bars "
          f"({df['timestamp'].min()} -> {df['timestamp'].max()})")
    return df


# ---------------------------------------------------------------------------
# Calibration driver
# ---------------------------------------------------------------------------

def _calibrate_one_tf(tf: str, df: pd.DataFrame, base_strategy_cfg: Dict,
                      bt_config: BacktestConfig, walk_forward: bool,
                      n_splits: int, min_trades: int) -> Dict:
    """Run optimization for one timeframe; return best params + metrics."""
    print(f"\n{'='*60}")
    print(f"Calibrating timeframe: {tf}  ({len(df)} bars)")
    print(f"{'='*60}")

    grid = PARAM_GRIDS[tf]
    n_combos = 1
    for values in grid.values():
        n_combos *= len(values)
    print(f"Grid: {n_combos} combinations, params={list(grid.keys())}")

    # Each TF needs its own strategy config, with the target TF as the base
    tf_strategy_cfg = dict(base_strategy_cfg)
    tf_strategy_cfg["base_timeframe"] = tf
    # Disable MTF resampling during calibration — we're feeding the
    # target-TF candles directly, so MTF context can't build meaningfully.
    tf_strategy_cfg.setdefault("multi_timeframe", {})
    tf_strategy_cfg["multi_timeframe"] = dict(
        tf_strategy_cfg.get("multi_timeframe", {}))
    tf_strategy_cfg["multi_timeframe"]["enabled"] = False

    optimizer = ParameterOptimizer(df, tf_strategy_cfg)

    if walk_forward:
        results = optimizer.walk_forward_optimize(
            param_grid=grid,
            backtest_config=bt_config,
            n_splits=n_splits,
            train_pct=0.70,
            min_trades=min_trades,
        )
        if results.empty:
            print(f"WARNING: no valid walk-forward results for {tf}")
            return {"timeframe": tf, "params": None, "metrics": None,
                    "warning": "no_valid_splits"}

        # Pick best params by average OOS Sharpe across splits
        param_cols = list(grid.keys())
        grouped = results.groupby(param_cols).agg({
            "oos_sharpe_ratio": "mean",
            "oos_total_roi": "mean",
            "oos_total_trades": "sum",
            "oos_win_rate": "mean",
            "oos_max_drawdown_pct": "mean",
            "oos_profit_factor": "mean",
        }).reset_index()
        grouped = grouped.sort_values(
            "oos_sharpe_ratio", ascending=False).reset_index(drop=True)
        if grouped.empty:
            return {"timeframe": tf, "params": None, "metrics": None,
                    "warning": "no_valid_groups"}

        best = grouped.iloc[0]
        best_params = {p: _coerce(best[p]) for p in param_cols}
        metrics = {
            "oos_sharpe": float(best["oos_sharpe_ratio"]),
            "oos_roi_pct": float(best["oos_total_roi"]),
            "oos_trades": int(best["oos_total_trades"]),
            "oos_win_rate": float(best["oos_win_rate"]),
            "oos_max_drawdown_pct": float(best["oos_max_drawdown_pct"]),
            "oos_profit_factor": float(best["oos_profit_factor"]),
            "n_splits_valid": int(len(results)),
        }
    else:
        results = optimizer.optimize(param_grid=grid,
                                      backtest_config=bt_config)
        if results.empty:
            return {"timeframe": tf, "params": None, "metrics": None,
                    "warning": "no_results"}
        best = results.iloc[0]
        best_params = {p: _coerce(best[p]) for p in grid.keys()}
        metrics = {
            "sharpe": float(best.get("sharpe_ratio", 0.0)),
            "roi_pct": float(best.get("total_roi", 0.0)),
            "trades": int(best.get("total_trades", 0)),
            "win_rate": float(best.get("win_rate", 0.0)),
            "max_drawdown_pct": float(best.get("max_drawdown_pct", 0.0)),
            "profit_factor": float(best.get("profit_factor", 0.0)),
        }

    print(f"\nBest params for {tf}: {best_params}")
    print(f"Metrics: {metrics}")
    return {"timeframe": tf, "params": best_params, "metrics": metrics}


def _coerce(value):
    """Coerce numpy / pandas scalars to plain Python for JSON serialization."""
    if hasattr(value, "item"):
        return value.item()
    return value


def calibrate(inst_id: str, days: int, timeframes: List[str],
              config_path: str, out_path: Optional[str],
              walk_forward: bool, n_splits: int, min_trades: int,
              force_refresh: bool, dry_run: bool,
              csv_path: Optional[str] = None) -> Dict:
    """Main calibration entry point."""
    if csv_path:
        # Use local CSV instead of fetching from API
        print(f"Loading data from {csv_path}...")
        df_5m = pd.read_csv(csv_path, parse_dates=["timestamp"])
        if inst_id == "auto":
            inst_id = "BTC-USDT"
        base_strategy_cfg = {}
    else:
        creds, base_strategy_cfg, cfg_inst = _load_exchange_credentials(config_path)
        if inst_id == "auto":
            inst_id = cfg_inst
        df_5m = _fetch_base_data(inst_id, days, creds, force_refresh)
    if df_5m.empty:
        raise RuntimeError("No 5m data fetched — check API credentials "
                           "and network")

    # Build a backtest config that matches production risk settings
    bt_config = BacktestConfig(
        initial_balance=10000.0,
        fee_rate=0.0006,
        slippage_pct=0.05,
        risk_per_trade_pct=float(
            base_strategy_cfg.get("risk_per_trade_pct", 5.0)),
        min_confidence=float(
            base_strategy_cfg.get("min_confidence", 0.45)),
        allow_shorts=True,
        lookback_candles=200,
        contract_value=0.001,
        use_risk_multiplier=True,
        use_time_exits=True,
    )

    profiles: Dict[str, Dict] = {}
    metrics: Dict[str, Dict] = {}
    warnings: List[str] = []

    for tf in timeframes:
        if tf not in PARAM_GRIDS:
            warnings.append(f"Skipping unsupported TF: {tf}")
            print(f"Skipping {tf} — no grid defined")
            continue

        df_tf = _resample_ohlcv(df_5m, tf) if tf != "5m" else df_5m.copy()
        if len(df_tf) < bt_config.lookback_candles + 100:
            warnings.append(f"{tf}: insufficient bars "
                            f"({len(df_tf)} < {bt_config.lookback_candles + 100})")
            print(f"Skipping {tf} — only {len(df_tf)} bars available")
            continue

        result = _calibrate_one_tf(
            tf=tf, df=df_tf, base_strategy_cfg=base_strategy_cfg,
            bt_config=bt_config, walk_forward=walk_forward,
            n_splits=n_splits, min_trades=min_trades)

        if result.get("params"):
            profiles[tf] = result["params"]
            metrics[tf] = result["metrics"]
        else:
            warnings.append(
                f"{tf}: {result.get('warning', 'calibration_failed')}")

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "inst_id": inst_id,
            "days": days,
            "base_timeframe": "5m (resampled for 15m/1h)",
            "total_bars": len(df_5m),
        },
        "calibration": {
            "walk_forward": walk_forward,
            "n_splits": n_splits,
            "min_trades": min_trades,
        },
        "profiles": profiles,
        "metrics": metrics,
        "warnings": warnings,
    }

    if dry_run:
        print("\n--- DRY RUN: output would be ---")
        print(json.dumps(output, indent=2))
    elif out_path:
        full_path = Path(out_path)
        if not full_path.is_absolute():
            full_path = Path(PROJECT_ROOT) / full_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = full_path.with_suffix(full_path.suffix + ".tmp")
        tmp.write_text(json.dumps(output, indent=2))
        tmp.replace(full_path)
        print(f"\nProfiles written to {full_path}")
        print(f"  {len(profiles)} timeframe(s) calibrated")
        if warnings:
            print(f"  {len(warnings)} warning(s):")
            for w in warnings:
                print(f"    - {w}")

    return output


def main():
    parser = argparse.ArgumentParser(
        description="Calibrate strategy parameters per timeframe via "
                    "walk-forward optimization.")
    parser.add_argument("--config", default="config.json",
                        help="Path to bot config (used for API credentials "
                             "and base strategy settings)")
    parser.add_argument("--inst-id", default="auto",
                        help="Trading pair (default: from config)")
    parser.add_argument("--days", type=int, default=90,
                        help="Days of history to fetch (default: 90)")
    parser.add_argument("--timeframes", nargs="+",
                        default=SUPPORTED_CALIBRATION_TFS,
                        help=f"Which timeframes to calibrate "
                             f"(default: {SUPPORTED_CALIBRATION_TFS})")
    parser.add_argument("--out", default="memory/timeframe_profiles.json",
                        help="Output JSON path (relative to project root)")
    parser.add_argument("--full-sample", action="store_true",
                        help="Disable walk-forward; optimize on full data "
                             "(higher overfitting risk)")
    parser.add_argument("--n-splits", type=int, default=3,
                        help="Walk-forward splits (default: 3)")
    parser.add_argument("--min-trades", type=int, default=5,
                        help="Min trades in training set for valid params")
    parser.add_argument("--force-refresh", action="store_true",
                        help="Ignore cached data, fetch fresh")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print output instead of writing to disk")
    parser.add_argument("--csv", default=None,
                        help="Path to local 5m CSV file (skip API fetch)")
    args = parser.parse_args()

    try:
        calibrate(
            inst_id=args.inst_id,
            days=args.days,
            timeframes=args.timeframes,
            config_path=args.config,
            out_path=args.out,
            walk_forward=not args.full_sample,
            n_splits=args.n_splits,
            min_trades=args.min_trades,
            force_refresh=args.force_refresh,
            dry_run=args.dry_run,
            csv_path=args.csv,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
