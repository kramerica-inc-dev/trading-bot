#!/usr/bin/env python3
"""
Backtest CLI Entry Point

Usage:
    # Single backtest with synthetic data (no API needed):
    python -m backtest.run_backtest --days 90 --synthetic

    # Single backtest with live data:
    python -m backtest.run_backtest --days 30 --config config.advanced.json

    # From cached CSV:
    python -m backtest.run_backtest --csv backtest/data/BTC-USDT_5m.csv

    # Parameter optimization:
    python -m backtest.run_backtest --days 90 --synthetic --optimize

    # With equity curve chart:
    python -m backtest.run_backtest --days 90 --synthetic --chart
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Ensure project root is in path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, 'scripts'))
sys.path.insert(0, project_root)

import pandas as pd
from backtest.data_collector import DataCollector, generate_synthetic_data
from backtest.backtester import Backtester, BacktestConfig, HTFCandleSync
from backtest.optimizer import ParameterOptimizer
from trading_strategy import create_strategy


def load_config(config_path: str) -> dict:
    """Load strategy config from JSON file."""
    full_path = os.path.join(project_root, config_path)
    with open(full_path) as f:
        config = json.load(f)

    # If strategy params are nested, extract them
    if 'strategy' in config:
        strategy_config = dict(config['strategy'])
    else:
        strategy_config = dict(config)

    return config, strategy_config


def get_candle_data(args, full_config: dict) -> pd.DataFrame:
    """Get candle data from CSV, API, or synthetic generation."""
    if args.csv:
        print(f"Loading data from {args.csv}...")
        df = pd.read_csv(args.csv)
        df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
        return df

    if args.synthetic:
        print(f"Generating {args.days} days of synthetic {args.timeframe} data...")
        return generate_synthetic_data(days=args.days, bar=args.timeframe)

    # Fetch from API
    from blofin_api import BlofinAPI
    api = BlofinAPI(
        api_key=full_config.get('api_key', ''),
        api_secret=full_config.get('api_secret', ''),
        passphrase=full_config.get('passphrase', ''),
        demo=full_config.get('demo_mode', False)
    )
    collector = DataCollector(api)
    return collector.get_data(args.pair, args.timeframe, args.days)


def plot_results(result, output_path: str = None):
    """Plot equity curve and drawdown."""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        print("matplotlib not installed. Run: pip install matplotlib")
        return

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), height_ratios=[3, 1],
                                     sharex=True)

    timestamps = result.timestamps
    equity = result.equity_curve

    # Equity curve
    ax1.plot(timestamps, equity, 'b-', linewidth=1)
    ax1.axhline(y=result.config.initial_balance, color='gray', linestyle='--',
                alpha=0.5, label='Initial Balance')
    ax1.set_ylabel('Balance ($)')
    ax1.set_title(f'Backtest: {result.total_trades} trades, '
                  f'ROI {result.total_roi:+.1f}%, '
                  f'Win Rate {result.win_rate:.0%}, '
                  f'Sharpe {result.sharpe_ratio:.2f}')
    ax1.legend()
    ax1.grid(True, alpha=0.3)

    # Mark trades
    for trade in result.trades:
        color = 'green' if trade.pnl > 0 else 'red'
        marker = '^' if trade.side == 'buy' else 'v'
        ax1.plot(trade.entry_time, result.config.initial_balance, marker,
                 color=color, markersize=6, alpha=0.6)

    # Drawdown
    equity_arr = pd.Series(equity)
    peak = equity_arr.cummax()
    drawdown = ((peak - equity_arr) / peak) * 100
    ax2.fill_between(timestamps, 0, drawdown, color='red', alpha=0.3)
    ax2.set_ylabel('Drawdown (%)')
    ax2.set_xlabel('Date')
    ax2.grid(True, alpha=0.3)
    ax2.invert_yaxis()

    ax2.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
    fig.autofmt_xdate()

    plt.tight_layout()

    if output_path is None:
        results_dir = os.path.join(project_root, 'backtest', 'results')
        os.makedirs(results_dir, exist_ok=True)
        output_path = os.path.join(results_dir,
                                    f'equity_{datetime.now().strftime("%Y%m%d_%H%M%S")}.png')

    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    print(f"Chart saved to {output_path}")
    plt.close()


def run_single_backtest(args, strategy_config: dict, candles_df: pd.DataFrame, htf_sync=None):
    """Run a single backtest and display results."""
    bt_config = BacktestConfig(
        initial_balance=args.balance,
        min_confidence=args.confidence,
        allow_shorts=not args.no_shorts,
        risk_per_trade_pct=args.risk,
    )

    strategy_config['min_confidence'] = args.confidence
    strategy_config['min_votes'] = args.min_votes

    strategy = create_strategy("advanced", strategy_config)
    backtester = Backtester(strategy, bt_config, htf_sync=htf_sync)

    print(f"\nRunning backtest: {len(candles_df)} candles, "
          f"confidence={args.confidence}, min_votes={args.min_votes}, "
          f"shorts={'yes' if not args.no_shorts else 'no'}...")

    result = backtester.run(candles_df)
    print(result.summary())

    # Print last 10 trades
    if result.trades:
        print(f"\nLast {min(10, len(result.trades))} trades:")
        print(f"{'Time':>20s} {'Side':>5s} {'Entry':>10s} {'Exit':>10s} "
              f"{'P&L':>10s} {'Reason':>12s} {'Conf':>6s}")
        print("-" * 75)
        for trade in result.trades[-10:]:
            time_str = trade.entry_time.strftime('%Y-%m-%d %H:%M') if hasattr(trade.entry_time, 'strftime') else str(trade.entry_time)
            print(f"{time_str:>20s} {trade.side:>5s} "
                  f"${trade.entry_price:>9,.2f} ${trade.exit_price:>9,.2f} "
                  f"${trade.pnl:>+9.2f} {trade.exit_reason:>12s} "
                  f"{trade.confidence:>5.2f}")

    if args.chart:
        plot_results(result)

    return result


def run_optimization(args, strategy_config: dict, candles_df: pd.DataFrame):
    """Run parameter grid search."""
    bt_config = BacktestConfig(
        initial_balance=args.balance,
        risk_per_trade_pct=args.risk,
    )

    param_grid = {
        "min_confidence": [0.3, 0.35, 0.4, 0.45, 0.5],
        "min_votes": [2, 3],
        "allow_shorts": [True, False],
    }

    optimizer = ParameterOptimizer(candles_df, strategy_config)
    results_df = optimizer.optimize(param_grid, bt_config)

    print("\n" + "=" * 80)
    print("OPTIMIZATION RESULTS (sorted by Sharpe ratio)")
    print("=" * 80)
    ParameterOptimizer.print_comparison(results_df)

    # Save results
    results_dir = os.path.join(project_root, 'backtest', 'results')
    os.makedirs(results_dir, exist_ok=True)
    csv_path = os.path.join(results_dir,
                            f'optimization_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv')
    ParameterOptimizer.save_results(results_df, csv_path)

    return results_df


def main():
    parser = argparse.ArgumentParser(description="Backtest trading strategies")
    parser.add_argument("--pair", default="BTC-USDT", help="Trading pair")
    parser.add_argument("--timeframe", default="5m", choices=["5m", "15m", "1H"],
                        help="Candle timeframe")
    parser.add_argument("--days", type=int, default=30, help="Days of history")
    parser.add_argument("--csv", help="Load data from CSV file instead of API")
    parser.add_argument("--synthetic", action="store_true",
                        help="Use synthetic data (no API needed)")
    parser.add_argument("--balance", type=float, default=115.0,
                        help="Initial balance in USDT")
    parser.add_argument("--risk", type=float, default=10.0,
                        help="Risk per trade (%%)")
    parser.add_argument("--confidence", type=float, default=0.45,
                        help="Min confidence threshold")
    parser.add_argument("--min-votes", type=int, default=2,
                        help="Min indicator votes required")
    parser.add_argument("--no-shorts", action="store_true",
                        help="Disable short selling")
    parser.add_argument("--optimize", action="store_true",
                        help="Run parameter grid search")
    parser.add_argument("--chart", action="store_true",
                        help="Generate equity curve chart")
    parser.add_argument("--config", default="config.advanced.json",
                        help="Strategy config file")
    parser.add_argument("--mtf", action="store_true",
                        help="Use real multi-timeframe candles (fetches 15m/1H/4H separately, no lookahead)")

    args = parser.parse_args()

    # Load config
    full_config, strategy_config = load_config(args.config)

    # Get data
    candles_df = get_candle_data(args, full_config)

    if candles_df.empty or len(candles_df) < 100:
        print(f"Not enough data: {len(candles_df)} candles (need at least 100)")
        sys.exit(1)

    print(f"Data: {len(candles_df)} candles from {candles_df['timestamp'].iloc[0]} "
          f"to {candles_df['timestamp'].iloc[-1]}")

    # Optionally fetch real HTF data
    htf_sync = None
    if args.mtf and not args.synthetic:
        print("\nFetching real HTF candles (15m, 1H, 4H)...")
        from blofin_api import BlofinAPI
        blofin_cfg = full_config.get('blofin', {})
        api_mtf = BlofinAPI(
            api_key=blofin_cfg.get('api_key', full_config.get('api_key', '')),
            api_secret=blofin_cfg.get('api_secret', full_config.get('api_secret', '')),
            passphrase=blofin_cfg.get('passphrase', full_config.get('passphrase', '')),
            demo=blofin_cfg.get('demo_mode', False)
        )
        collector_htf = DataCollector(api_mtf)
        htf_datasets = collector_htf.get_multi_timeframe_data(
            args.pair, args.timeframe, ['15m', '1H', '4H'], args.days)
        htf_only = {k: v for k, v in htf_datasets.items() if k != args.timeframe}
        if htf_only:
            htf_sync = HTFCandleSync(htf_only)
            print(f"HTF data loaded: {', '.join(f'{k}={len(v)} bars' for k, v in htf_only.items())}")
        else:
            print("No HTF data available, falling back to resampling")
    elif args.mtf and args.synthetic:
        print("Warning: --mtf with --synthetic not supported. Using resampling.")

    if args.optimize:
        run_optimization(args, strategy_config, candles_df)
    else:
        run_single_backtest(args, strategy_config, candles_df, htf_sync=htf_sync)


if __name__ == "__main__":
    main()
