#!/usr/bin/env python3
"""
Reproducible baseline backtest run.

Fixed parameters, fixed dataset, deterministic output.
Used to verify the strategy produces trades and to diagnose filtering problems.

Usage:
    python -m backtest.run_baseline
    python -m backtest.run_baseline --diagnostics
    python -m backtest.run_baseline --verbose-rejections
    python -m backtest.run_baseline --csv backtest/data/BTC-USDT_5m.csv
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, 'scripts'))
sys.path.insert(0, project_root)

import pandas as pd
from backtest.backtester import Backtester, BacktestConfig
from trading_strategy import create_strategy

# --- Fixed baseline parameters (do not change without reason) ---
BASELINE_CONFIG = {
    'min_confidence': 0.45,
    'min_votes': 2,
}

BASELINE_BACKTEST = BacktestConfig(
    initial_balance=115.0,
    fee_rate=0.0006,
    slippage_pct=0.05,
    risk_per_trade_pct=5.0,
    min_confidence=0.45,
    allow_shorts=True,
    lookback_candles=200,
    contract_value=0.001,
    use_risk_multiplier=True,
    use_time_exits=True,
    stale_trade_atr_progress=0.18,
)

DEFAULT_CSV = os.path.join(project_root, 'backtest', 'data', 'BTC-USDT_5m.csv')


def main():
    parser = argparse.ArgumentParser(description="Reproducible baseline backtest")
    parser.add_argument("--csv", default=DEFAULT_CSV,
                        help="Path to candle CSV file")
    parser.add_argument("--verbose-rejections", action="store_true",
                        help="Print per-bar rejection reasons for first N rejections")
    parser.add_argument("--rejection-limit", type=int, default=20,
                        help="Max rejection reasons to print in verbose mode")
    parser.add_argument("--diagnostics", action="store_true",
                        help="Enable per-candle regime diagnostics, export CSV, print distribution report")
    args = parser.parse_args()

    # Load data
    csv_path = args.csv
    if not os.path.exists(csv_path):
        print(f"Data file not found: {csv_path}")
        sys.exit(1)

    print(f"Loading data from {csv_path}...")
    df = pd.read_csv(csv_path)
    df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
    print(f"Data: {len(df)} candles from {df['timestamp'].iloc[0]} "
          f"to {df['timestamp'].iloc[-1]}")

    # Create strategy
    strategy = create_strategy("advanced", dict(BASELINE_CONFIG))

    if args.diagnostics and hasattr(strategy, 'enable_diagnostics'):
        strategy.enable_diagnostics()

    # Run backtest
    backtester = Backtester(strategy, BASELINE_BACKTEST)
    print(f"\nRunning baseline: balance=${BASELINE_BACKTEST.initial_balance}, "
          f"risk={BASELINE_BACKTEST.risk_per_trade_pct}%, "
          f"confidence={BASELINE_BACKTEST.min_confidence}, "
          f"lookback={BASELINE_BACKTEST.lookback_candles}")

    result = backtester.run(df)

    # Print results
    print(result.summary())

    # Rejection stats
    if hasattr(strategy, 'rejection_stats'):
        stats = strategy.rejection_stats
        total_rejections = sum(stats.values())
        print(f"\nRejection Stats ({total_rejections} total rejections):")
        if stats:
            max_key_len = max(len(k) for k in stats)
            for key, count in sorted(stats.items(), key=lambda x: -x[1]):
                pct = count / total_rejections * 100 if total_rejections > 0 else 0
                bar = '#' * int(pct / 2)
                print(f"  {key:{max_key_len}s}  {count:5d}  ({pct:5.1f}%)  {bar}")
        else:
            print("  (no rejections recorded)")

    # No-trade diagnosis
    if result.total_trades == 0:
        print("\n*** NO TRADES GENERATED ***")
        print("Top blocking filters:")
        if hasattr(strategy, 'rejection_stats') and strategy.rejection_stats:
            top3 = sorted(strategy.rejection_stats.items(),
                          key=lambda x: -x[1])[:3]
            for key, count in top3:
                print(f"  - {key}: {count} rejections")
        print("\nSuggested actions:")
        print("  1. Lower min_confidence (currently 0.45)")
        print("  2. Lower min_votes (currently 2)")
        print("  3. Check regime detection thresholds")
        print("  4. Run with --verbose-rejections for per-bar detail")

    # Last trades
    if result.trades:
        n = min(10, len(result.trades))
        print(f"\nLast {n} trades:")
        print(f"{'Time':>20s} {'Side':>5s} {'Entry':>10s} {'Exit':>10s} "
              f"{'P&L':>10s} {'Reason':>12s} {'Regime':>12s}")
        print("-" * 85)
        for trade in result.trades[-n:]:
            time_str = (trade.entry_time.strftime('%Y-%m-%d %H:%M')
                        if hasattr(trade.entry_time, 'strftime')
                        else str(trade.entry_time))
            print(f"{time_str:>20s} {trade.side:>5s} "
                  f"${trade.entry_price:>9,.2f} ${trade.exit_price:>9,.2f} "
                  f"${trade.pnl:>+9.2f} {trade.exit_reason:>12s} "
                  f"{trade.regime:>12s}")

    # Near-miss stats
    if hasattr(strategy, 'near_miss_stats'):
        nm = strategy.near_miss_stats
        if nm:
            print(f"\nNear-miss Stats:")
            for key, count in sorted(nm.items(), key=lambda x: -x[1]):
                print(f"  {key}: {count}")
        else:
            print("\nNear-miss Stats: (none)")

    # Diagnostics report
    results_dir = os.path.join(project_root, 'backtest', 'results')
    os.makedirs(results_dir, exist_ok=True)
    if args.diagnostics and hasattr(strategy, 'get_diagnostics'):
        diag = strategy.get_diagnostics()
        if diag:
            import numpy as np
            from collections import Counter
            print(f"\n--- Threshold Distribution Report ({len(diag)} candles) ---")
            for key in ['efficiency_ratio', 'trend_bias', 'anchor_slope', 'atr_pct']:
                vals = [abs(d[key]) if key in ('trend_bias', 'anchor_slope') else d[key]
                        for d in diag if key in d]
                if vals:
                    arr = np.array(vals)
                    pcts = np.percentile(arr, [10, 25, 50, 75, 90])
                    label = f"|{key}|" if key in ('trend_bias', 'anchor_slope') else key
                    print(f"  {label:25s}  p10={pcts[0]:.6f}  p25={pcts[1]:.6f}  "
                          f"p50={pcts[2]:.6f}  p75={pcts[3]:.6f}  p90={pcts[4]:.6f}")

            for key in ['bull_conditions_passing', 'bear_conditions_passing']:
                vals = [int(d.get(key, 0)) for d in diag]
                if vals:
                    dist = Counter(vals)
                    total = len(vals)
                    print(f"\n  {key}:")
                    for n in sorted(dist.keys()):
                        pct = dist[n] / total * 100
                        bar = '#' * int(pct / 2)
                        print(f"    {n}/7: {dist[n]:5d} ({pct:5.1f}%)  {bar}")

            # Regime distribution
            regime_dist = Counter(d.get('final_regime', '?') for d in diag)
            print(f"\n  Regime distribution:")
            for regime, count in sorted(regime_dist.items(), key=lambda x: -x[1]):
                pct = count / len(diag) * 100
                print(f"    {regime:15s}: {count:5d} ({pct:5.1f}%)")

            # Export CSV
            csv_path = os.path.join(results_dir, 'regime_diagnostics.csv')
            n_exported = strategy.export_diagnostics_csv(csv_path)
            print(f"\nDiagnostics exported: {n_exported} rows -> {csv_path}")
        else:
            print("\nDiagnostics: no data recorded")

    # Save result JSON
    output_path = os.path.join(results_dir, 'baseline_latest.json')

    output = result.to_dict()
    output['baseline_config'] = {
        'min_confidence': BASELINE_CONFIG['min_confidence'],
        'min_votes': BASELINE_CONFIG['min_votes'],
        'initial_balance': BASELINE_BACKTEST.initial_balance,
        'risk_per_trade_pct': BASELINE_BACKTEST.risk_per_trade_pct,
        'slippage_pct': BASELINE_BACKTEST.slippage_pct,
    }
    output['rejection_stats'] = (strategy.rejection_stats
                                  if hasattr(strategy, 'rejection_stats') else {})
    output['near_miss_stats'] = (strategy.near_miss_stats
                                  if hasattr(strategy, 'near_miss_stats') else {})
    output['data_file'] = os.path.basename(csv_path)
    output['data_rows'] = len(df)
    output['timestamp'] = datetime.now().isoformat()

    with open(output_path, 'w') as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\nResults saved to {output_path}")

    return 0 if result.total_trades > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
