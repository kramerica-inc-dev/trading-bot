#!/usr/bin/env python3
"""
Parameter Optimizer
Grid search over strategy parameters with comparison output.
"""

import os
import sys
import itertools
from typing import Dict, List

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
from trading_strategy import create_strategy
from backtest.backtester import Backtester, BacktestConfig, BacktestResult


class ParameterOptimizer:
    """Grid search over strategy parameters"""

    def __init__(self, candles_df: pd.DataFrame, base_strategy_config: Dict):
        self.candles_df = candles_df
        self.base_config = dict(base_strategy_config)

    def optimize(self, param_grid: Dict[str, List],
                 backtest_config: BacktestConfig = None) -> pd.DataFrame:
        """Run backtests for all parameter combinations.

        Args:
            param_grid: e.g. {
                "min_confidence": [0.3, 0.4, 0.45, 0.5, 0.6],
                "min_votes": [2, 3],
                "allow_shorts": [True, False]
            }
            backtest_config: Base backtest config (balance, fees, etc.)
        """
        if backtest_config is None:
            backtest_config = BacktestConfig()

        # Separate strategy params from backtest params
        bt_params = {'allow_shorts', 'min_confidence', 'initial_balance',
                     'risk_per_trade_pct', 'fee_rate'}

        # Generate all combinations
        param_names = list(param_grid.keys())
        param_values = list(param_grid.values())
        combinations = list(itertools.product(*param_values))

        print(f"Running {len(combinations)} backtest combinations...")
        results = []

        for i, combo in enumerate(combinations):
            params = dict(zip(param_names, combo))
            print(f"  [{i+1}/{len(combinations)}] {params}")

            # Build strategy config
            strategy_config = dict(self.base_config)
            bt_cfg = BacktestConfig(
                initial_balance=backtest_config.initial_balance,
                fee_rate=backtest_config.fee_rate,
                risk_per_trade_pct=backtest_config.risk_per_trade_pct,
                min_confidence=backtest_config.min_confidence,
                allow_shorts=backtest_config.allow_shorts,
                lookback_candles=backtest_config.lookback_candles,
                contract_value=backtest_config.contract_value,
            )

            for name, value in params.items():
                if name == 'allow_shorts':
                    bt_cfg.allow_shorts = value
                elif name == 'min_confidence':
                    bt_cfg.min_confidence = value
                    strategy_config['min_confidence'] = value
                elif name == 'risk_per_trade_pct':
                    bt_cfg.risk_per_trade_pct = value
                else:
                    strategy_config[name] = value

            # Create strategy and run backtest
            strategy = create_strategy("advanced", strategy_config)
            backtester = Backtester(strategy, bt_cfg)
            result = backtester.run(self.candles_df)

            # Collect result row
            row = dict(params)
            row.update(result.to_dict())
            results.append(row)

        df = pd.DataFrame(results)
        # Sort by Sharpe ratio descending
        if 'sharpe_ratio' in df.columns:
            df = df.sort_values('sharpe_ratio', ascending=False).reset_index(drop=True)

        return df

    def walk_forward_optimize(self, param_grid: Dict[str, List],
                              backtest_config: BacktestConfig = None,
                              n_splits: int = 3,
                              train_pct: float = 0.70,
                              min_trades: int = 5) -> pd.DataFrame:
        """Walk-forward optimization with out-of-sample validation.

        Splits data into n_splits sequential windows. For each window, trains
        on train_pct of the data and tests on the remainder. Reports only
        out-of-sample (OOS) performance.

        Args:
            param_grid: Parameter grid (same as optimize)
            backtest_config: Base backtest config
            n_splits: Number of walk-forward windows
            train_pct: Fraction of each window used for training
            min_trades: Minimum trades in training set to consider valid
        """
        if backtest_config is None:
            backtest_config = BacktestConfig()

        total_rows = len(self.candles_df)
        window_size = total_rows // n_splits
        if window_size < 200:
            print(f"Warning: window size {window_size} is small, "
                  f"consider using fewer splits or more data")

        param_names = list(param_grid.keys())
        param_values = list(param_grid.values())
        combinations = list(itertools.product(*param_values))

        print(f"Walk-forward: {n_splits} windows, "
              f"{len(combinations)} param combos, "
              f"{train_pct:.0%} train / {1-train_pct:.0%} test")

        oos_results = []

        for split in range(n_splits):
            start = split * window_size
            end = min(start + window_size, total_rows)
            if split == n_splits - 1:
                end = total_rows

            split_data = self.candles_df.iloc[start:end].reset_index(drop=True)
            split_point = int(len(split_data) * train_pct)
            train_data = split_data.iloc[:split_point].reset_index(drop=True)
            test_data = split_data.iloc[split_point:].reset_index(drop=True)

            if len(train_data) < backtest_config.lookback_candles + 50:
                print(f"  Split {split+1}: train too small, skipping")
                continue
            if len(test_data) < backtest_config.lookback_candles + 20:
                print(f"  Split {split+1}: test too small, skipping")
                continue

            print(f"\n  Split {split+1}/{n_splits}: "
                  f"train={len(train_data)} bars, test={len(test_data)} bars")

            # Phase 1: Find best params on training data
            best_sharpe = -999
            best_params = None

            for combo in combinations:
                params = dict(zip(param_names, combo))
                strategy_config = dict(self.base_config)
                bt_cfg = self._make_bt_config(backtest_config, params,
                                              strategy_config)

                strategy = create_strategy("advanced", strategy_config)
                backtester = Backtester(strategy, bt_cfg)
                result = backtester.run(train_data)

                if result.total_trades < min_trades:
                    continue
                if result.sharpe_ratio > best_sharpe:
                    best_sharpe = result.sharpe_ratio
                    best_params = params

            if best_params is None:
                print(f"    No valid params found (min_trades={min_trades})")
                continue

            print(f"    Best train params: {best_params} "
                  f"(Sharpe={best_sharpe:.2f})")

            # Phase 2: Test best params on OOS data
            strategy_config = dict(self.base_config)
            bt_cfg = self._make_bt_config(backtest_config, best_params,
                                          strategy_config)
            strategy = create_strategy("advanced", strategy_config)
            backtester = Backtester(strategy, bt_cfg)
            oos_result = backtester.run(test_data)

            row = dict(best_params)
            row['split'] = split + 1
            row['train_sharpe'] = best_sharpe
            row.update({f'oos_{k}': v
                        for k, v in oos_result.to_dict().items()})
            oos_results.append(row)

            print(f"    OOS: trades={oos_result.total_trades}, "
                  f"ROI={oos_result.total_roi:+.2f}%, "
                  f"Sharpe={oos_result.sharpe_ratio:.2f}, "
                  f"WR={oos_result.win_rate:.1%}")

        df = pd.DataFrame(oos_results)
        if not df.empty and 'oos_sharpe_ratio' in df.columns:
            avg_oos_sharpe = df['oos_sharpe_ratio'].mean()
            avg_oos_roi = df['oos_total_roi'].mean()
            print(f"\n  === Walk-Forward Summary ===")
            print(f"  Avg OOS Sharpe: {avg_oos_sharpe:.2f}")
            print(f"  Avg OOS ROI:    {avg_oos_roi:+.2f}%")
            print(f"  Splits valid:   {len(df)}/{n_splits}")
        return df

    def _make_bt_config(self, base_config: BacktestConfig,
                        params: Dict, strategy_config: Dict) -> BacktestConfig:
        """Build a BacktestConfig from base + param overrides."""
        bt_cfg = BacktestConfig(
            initial_balance=base_config.initial_balance,
            fee_rate=base_config.fee_rate,
            slippage_pct=base_config.slippage_pct,
            risk_per_trade_pct=base_config.risk_per_trade_pct,
            min_confidence=base_config.min_confidence,
            allow_shorts=base_config.allow_shorts,
            lookback_candles=base_config.lookback_candles,
            contract_value=base_config.contract_value,
            use_risk_multiplier=base_config.use_risk_multiplier,
            use_time_exits=base_config.use_time_exits,
            stale_trade_atr_progress=base_config.stale_trade_atr_progress,
        )
        for name, value in params.items():
            if name == 'allow_shorts':
                bt_cfg.allow_shorts = value
            elif name == 'min_confidence':
                bt_cfg.min_confidence = value
                strategy_config['min_confidence'] = value
            elif name == 'risk_per_trade_pct':
                bt_cfg.risk_per_trade_pct = value
            else:
                strategy_config[name] = value
        return bt_cfg

    @staticmethod
    def print_comparison(results_df: pd.DataFrame):
        """Pretty-print comparison table."""
        if results_df.empty:
            print("No results to display.")
            return

        # Determine which columns are parameters vs metrics
        metric_cols = ['total_trades', 'win_rate', 'total_roi', 'max_drawdown_pct',
                       'profit_factor', 'sharpe_ratio', 'long_trades', 'short_trades',
                       'final_balance']
        param_cols = [c for c in results_df.columns if c not in metric_cols
                      and c not in ['total_pnl', 'avg_win', 'avg_loss',
                                    'initial_balance']]

        # Header
        header_parts = []
        for col in param_cols:
            header_parts.append(f"{col:>14s}")
        header_parts.extend([
            f"{'trades':>7s}", f"{'win%':>7s}", f"{'ROI%':>8s}",
            f"{'DD%':>7s}", f"{'PF':>7s}", f"{'sharpe':>7s}",
            f"{'L/S':>7s}"
        ])
        print("\n" + " | ".join(header_parts))
        print("-" * len(" | ".join(header_parts)))

        # Rows
        for _, row in results_df.iterrows():
            row_parts = []
            for col in param_cols:
                val = row[col]
                if isinstance(val, bool):
                    row_parts.append(f"{'yes' if val else 'no':>14s}")
                elif isinstance(val, float):
                    row_parts.append(f"{val:>14.3f}")
                else:
                    row_parts.append(f"{str(val):>14s}")

            row_parts.extend([
                f"{int(row.get('total_trades', 0)):>7d}",
                f"{row.get('win_rate', 0):>6.1%}",
                f"{row.get('total_roi', 0):>+7.1f}%",
                f"{row.get('max_drawdown_pct', 0):>6.1f}%",
                f"{row.get('profit_factor', 0):>7.2f}",
                f"{row.get('sharpe_ratio', 0):>7.2f}",
                f"{int(row.get('long_trades', 0)):>3d}/{int(row.get('short_trades', 0)):<3d}",
            ])
            print(" | ".join(row_parts))

        print()

    @staticmethod
    def save_results(results_df: pd.DataFrame, path: str):
        """Save results to CSV."""
        results_df.to_csv(path, index=False)
        print(f"Results saved to {path}")
