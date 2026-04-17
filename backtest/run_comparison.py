#!/usr/bin/env python3
"""Compare advanced vs robust strategies with optimized regime thresholds."""

import os
import sys
import json
import time

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, 'scripts'))
sys.path.insert(0, project_root)

import pandas as pd

from trading_strategy import create_strategy
from backtest.backtester import Backtester, BacktestConfig
from backtest.optimizer import ParameterOptimizer


DATA_PATH = os.path.join(os.path.dirname(__file__), 'data', 'BTC-USDT_5m.csv')
RESULTS_DIR = os.path.join(os.path.dirname(__file__), 'results')
os.makedirs(RESULTS_DIR, exist_ok=True)


def load_data():
    df = pd.read_csv(DATA_PATH, parse_dates=['timestamp'])
    print(f"Loaded {len(df)} candles: {df['timestamp'].iloc[0]} to {df['timestamp'].iloc[-1]}")
    return df


LOOKBACK = 210  # Must exceed anchor_ema(200) + buffer for robust, and regime_anchor_ema(89)+slope(12) for advanced


def run_advanced_baseline(df):
    """Run baseline advanced strategy backtest."""
    print("\n" + "=" * 60)
    print("ADVANCED STRATEGY - BASELINE")
    print("=" * 60)
    config = {}  # defaults
    strategy = create_strategy("advanced", config)
    bt_cfg = BacktestConfig(
        initial_balance=10000.0,
        risk_per_trade_pct=10.0,
        min_confidence=0.45,
        allow_shorts=True,
        lookback_candles=LOOKBACK,
    )
    bt = Backtester(strategy, bt_cfg)
    result = bt.run(df)
    print(result.summary())
    return result


def run_advanced_grid(df):
    """Grid search over regime thresholds for advanced strategy."""
    print("\n" + "=" * 60)
    print("ADVANCED STRATEGY - GRID SEARCH (regime thresholds)")
    print("=" * 60)

    base_config = {}  # start from defaults
    bt_cfg = BacktestConfig(
        initial_balance=10000.0,
        risk_per_trade_pct=10.0,
        min_confidence=0.45,
        allow_shorts=True,
        lookback_candles=LOOKBACK,
    )

    param_grid = {
        'regime__efficiency_trend_threshold': [0.10, 0.15, 0.22],
        'regime__trend_strength_threshold': [0.0008, 0.0014, 0.0020],
        'regime__anchor_slope_threshold': [0.0006, 0.0010, 0.0015],
    }

    optimizer = ParameterOptimizer(df, base_config, strategy_name="advanced")
    t0 = time.time()
    results_df = optimizer.optimize(param_grid, bt_cfg)
    elapsed = time.time() - t0
    print(f"\nGrid search completed in {elapsed:.0f}s")

    optimizer.print_comparison(results_df)
    out_path = os.path.join(RESULTS_DIR, 'advanced_grid_results.csv')
    optimizer.save_results(results_df, out_path)

    # Show top 5
    if not results_df.empty:
        print("\nTop 5 by Sharpe ratio:")
        top5 = results_df.head(5)
        for _, row in top5.iterrows():
            print(f"  eff_thresh={row.get('regime__efficiency_trend_threshold', '?'):.2f}  "
                  f"trend_str={row.get('regime__trend_strength_threshold', '?'):.4f}  "
                  f"anchor_sl={row.get('regime__anchor_slope_threshold', '?'):.4f}  "
                  f"| trades={int(row.get('total_trades', 0))}  "
                  f"WR={row.get('win_rate', 0):.1%}  "
                  f"ROI={row.get('total_roi', 0):+.2f}%  "
                  f"PF={row.get('profit_factor', 0):.2f}  "
                  f"Sharpe={row.get('sharpe_ratio', 0):.2f}")

    return results_df


def run_robust_baseline(df):
    """Run robust strategy backtest with default config."""
    print("\n" + "=" * 60)
    print("ROBUST STRATEGY - BASELINE")
    print("=" * 60)
    config = {
        "fast_ema": 20,
        "slow_ema": 50,
        "anchor_ema": 200,
        "rsi_period": 14,
        "atr_period": 14,
        "pullback_atr_multiple": 0.6,
        "stop_atr_multiple": 1.8,
        "take_profit_atr_multiple": 2.8,
        "min_trend_strength": 0.0025,
        "max_atr_pct": 0.018,
        "min_atr_pct": 0.002,
        "long_rsi_floor": 45,
        "short_rsi_ceiling": 55,
        "max_hold_bars": 36,
    }
    strategy = create_strategy("robust", config)
    bt_cfg = BacktestConfig(
        initial_balance=10000.0,
        risk_per_trade_pct=10.0,
        min_confidence=0.40,
        allow_shorts=True,
        lookback_candles=LOOKBACK,
    )
    bt = Backtester(strategy, bt_cfg)
    result = bt.run(df)
    print(result.summary())
    return result


def run_robust_grid(df):
    """Grid search over robust strategy parameters."""
    print("\n" + "=" * 60)
    print("ROBUST STRATEGY - GRID SEARCH")
    print("=" * 60)

    base_config = {
        "fast_ema": 20,
        "slow_ema": 50,
        "anchor_ema": 200,
        "rsi_period": 14,
        "atr_period": 14,
        "pullback_atr_multiple": 0.6,
        "stop_atr_multiple": 1.8,
        "take_profit_atr_multiple": 2.8,
        "min_trend_strength": 0.0025,
        "max_atr_pct": 0.018,
        "min_atr_pct": 0.002,
        "long_rsi_floor": 45,
        "short_rsi_ceiling": 55,
        "max_hold_bars": 36,
    }
    bt_cfg = BacktestConfig(
        initial_balance=10000.0,
        risk_per_trade_pct=10.0,
        min_confidence=0.40,
        allow_shorts=True,
        lookback_candles=LOOKBACK,
    )

    param_grid = {
        'min_trend_strength': [0.0015, 0.0025, 0.0035],
        'pullback_atr_multiple': [0.4, 0.6, 0.8],
        'stop_atr_multiple': [1.5, 1.8, 2.2],
    }

    optimizer = ParameterOptimizer(df, base_config, strategy_name="robust")
    t0 = time.time()
    results_df = optimizer.optimize(param_grid, bt_cfg)
    elapsed = time.time() - t0
    print(f"\nGrid search completed in {elapsed:.0f}s")

    optimizer.print_comparison(results_df)
    out_path = os.path.join(RESULTS_DIR, 'robust_grid_results.csv')
    optimizer.save_results(results_df, out_path)

    if not results_df.empty:
        print("\nTop 5 by Sharpe ratio:")
        top5 = results_df.head(5)
        for _, row in top5.iterrows():
            print(f"  trend_str={row.get('min_trend_strength', '?'):.4f}  "
                  f"pullback={row.get('pullback_atr_multiple', '?'):.1f}  "
                  f"stop={row.get('stop_atr_multiple', '?'):.1f}  "
                  f"| trades={int(row.get('total_trades', 0))}  "
                  f"WR={row.get('win_rate', 0):.1%}  "
                  f"ROI={row.get('total_roi', 0):+.2f}%  "
                  f"PF={row.get('profit_factor', 0):.2f}  "
                  f"Sharpe={row.get('sharpe_ratio', 0):.2f}")

    return results_df


def main():
    df = load_data()

    # 1. Advanced baseline
    adv_baseline = run_advanced_baseline(df)

    # 2. Advanced grid search with lower regime thresholds
    adv_grid = run_advanced_grid(df)

    # 3. Robust baseline
    rob_baseline = run_robust_baseline(df)

    # 4. Robust grid search
    rob_grid = run_robust_grid(df)

    # 5. Summary comparison
    print("\n" + "=" * 60)
    print("STRATEGY COMPARISON SUMMARY")
    print("=" * 60)

    print(f"\nAdvanced baseline:  {adv_baseline.total_trades} trades  "
          f"WR {adv_baseline.win_rate:.1%}  ROI {adv_baseline.total_roi:+.2f}%  "
          f"PF {adv_baseline.profit_factor:.2f}  Sharpe {adv_baseline.sharpe_ratio:.2f}  "
          f"DD {adv_baseline.max_drawdown_pct:.1f}%")

    if not adv_grid.empty:
        best_adv = adv_grid.iloc[0]
        print(f"Advanced optimized: {int(best_adv['total_trades'])} trades  "
              f"WR {best_adv['win_rate']:.1%}  ROI {best_adv['total_roi']:+.2f}%  "
              f"PF {best_adv['profit_factor']:.2f}  Sharpe {best_adv['sharpe_ratio']:.2f}  "
              f"DD {best_adv['max_drawdown_pct']:.1f}%")

    print(f"\nRobust baseline:    {rob_baseline.total_trades} trades  "
          f"WR {rob_baseline.win_rate:.1%}  ROI {rob_baseline.total_roi:+.2f}%  "
          f"PF {rob_baseline.profit_factor:.2f}  Sharpe {rob_baseline.sharpe_ratio:.2f}  "
          f"DD {rob_baseline.max_drawdown_pct:.1f}%")

    if not rob_grid.empty:
        best_rob = rob_grid.iloc[0]
        print(f"Robust optimized:   {int(best_rob['total_trades'])} trades  "
              f"WR {best_rob['win_rate']:.1%}  ROI {best_rob['total_roi']:+.2f}%  "
              f"PF {best_rob['profit_factor']:.2f}  Sharpe {best_rob['sharpe_ratio']:.2f}  "
              f"DD {best_rob['max_drawdown_pct']:.1f}%")

    # Save summary
    summary = {
        'advanced_baseline': adv_baseline.to_dict(),
        'robust_baseline': rob_baseline.to_dict(),
    }
    if not adv_grid.empty:
        best = adv_grid.iloc[0]
        summary['advanced_best'] = {
            'params': {
                'regime__efficiency_trend_threshold': best.get('regime__efficiency_trend_threshold'),
                'regime__trend_strength_threshold': best.get('regime__trend_strength_threshold'),
                'regime__anchor_slope_threshold': best.get('regime__anchor_slope_threshold'),
            },
            'metrics': {k: best[k] for k in ['total_trades', 'win_rate', 'total_roi',
                                               'profit_factor', 'sharpe_ratio', 'max_drawdown_pct']
                        if k in best.index},
        }
    if not rob_grid.empty:
        best = rob_grid.iloc[0]
        summary['robust_best'] = {
            'params': {
                'min_trend_strength': best.get('min_trend_strength'),
                'pullback_atr_multiple': best.get('pullback_atr_multiple'),
                'stop_atr_multiple': best.get('stop_atr_multiple'),
            },
            'metrics': {k: best[k] for k in ['total_trades', 'win_rate', 'total_roi',
                                               'profit_factor', 'sharpe_ratio', 'max_drawdown_pct']
                        if k in best.index},
        }

    summary_path = os.path.join(RESULTS_DIR, 'comparison_summary.json')
    with open(summary_path, 'w') as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"\nSummary saved to {summary_path}")


if __name__ == '__main__':
    main()
