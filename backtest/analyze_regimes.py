#!/usr/bin/env python3
"""
Regime analysis: forward returns, percentile tables, condition failure analysis.

Reads regime_diagnostics.csv (from run_baseline --diagnostics) and the candle CSV
to measure whether regime labels have predictive value.

Usage:
    python -m backtest.analyze_regimes
    python -m backtest.analyze_regimes --diagnostics-csv path/to/regime_diagnostics.csv
    python -m backtest.analyze_regimes --candle-csv path/to/BTC-USDT_5m.csv
"""

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime

import numpy as np
import pandas as pd

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_DIAG_CSV = os.path.join(project_root, 'backtest', 'results', 'regime_diagnostics.csv')
DEFAULT_CANDLE_CSV = os.path.join(project_root, 'backtest', 'data', 'BTC-USDT_5m.csv')


def load_data(diag_path, candle_path):
    """Load diagnostics and candle CSVs, return merged DataFrame."""
    diag = pd.read_csv(diag_path)
    candles = pd.read_csv(candle_path)

    # The diagnostics CSV bar_index starts at 1 and corresponds to candles
    # starting from the lookback offset. We align on price to verify.
    # Forward returns are computed from the diagnostics price column directly.
    return diag, candles


def compute_forward_returns(diag, horizons=(6, 12, 24)):
    """Add forward return columns to diagnostics DataFrame."""
    prices = diag['price'].values
    for h in horizons:
        fwd = np.full(len(prices), np.nan)
        fwd[:len(prices) - h] = (prices[h:] - prices[:len(prices) - h]) / prices[:len(prices) - h] * 100
        diag[f'fwd_{h}'] = fwd
    return diag


def forward_returns_by_regime(diag, horizons=(6, 12, 24)):
    """Compute forward return stats grouped by final_regime."""
    results = {}
    for regime in sorted(diag['final_regime'].unique()):
        subset = diag[diag['final_regime'] == regime]
        regime_stats = {'count': len(subset)}
        for h in horizons:
            col = f'fwd_{h}'
            vals = subset[col].dropna()
            if len(vals) == 0:
                continue
            regime_stats[f'fwd_{h}'] = {
                'mean': float(vals.mean()),
                'median': float(vals.median()),
                'std': float(vals.std()),
                'pct_positive': float((vals > 0).mean() * 100),
                'pct_negative': float((vals < 0).mean() * 100),
                'sharpe': float(vals.mean() / vals.std()) if vals.std() > 0 else 0.0,
                'n': int(len(vals)),
            }
        results[regime] = regime_stats
    return results


def forward_returns_by_condition_count(diag, direction='bull', horizons=(6, 12, 24)):
    """Compute forward return stats grouped by condition passing count."""
    col = f'{direction}_conditions_passing'
    results = {}
    for count in sorted(diag[col].unique()):
        subset = diag[diag[col] == count]
        stats = {'count': len(subset)}
        for h in horizons:
            fwd_col = f'fwd_{h}'
            vals = subset[fwd_col].dropna()
            if len(vals) == 0:
                continue
            stats[f'fwd_{h}'] = {
                'mean': float(vals.mean()),
                'median': float(vals.median()),
                'pct_positive': float((vals > 0).mean() * 100),
                'n': int(len(vals)),
            }
        results[int(count)] = stats
    return results


def percentile_tables(diag):
    """Compute percentiles for key metrics, overall and per-regime."""
    metrics = ['efficiency_ratio', 'trend_bias', 'anchor_slope', 'atr_pct', 'regime_confidence']
    abs_metrics = {'trend_bias', 'anchor_slope'}
    percentiles = [5, 10, 25, 50, 75, 90, 95]

    results = {'overall': {}, 'by_regime': {}}

    for metric in metrics:
        vals = diag[metric].dropna()
        if metric in abs_metrics:
            vals = vals.abs()
        arr = vals.values
        pcts = np.percentile(arr, percentiles)
        results['overall'][metric] = {f'p{p}': float(v) for p, v in zip(percentiles, pcts)}

    for regime in sorted(diag['final_regime'].unique()):
        subset = diag[diag['final_regime'] == regime]
        results['by_regime'][regime] = {}
        for metric in metrics:
            vals = subset[metric].dropna()
            if metric in abs_metrics:
                vals = vals.abs()
            if len(vals) == 0:
                continue
            arr = vals.values
            pcts = np.percentile(arr, percentiles)
            results['by_regime'][regime][metric] = {f'p{p}': float(v) for p, v in zip(percentiles, pcts)}

    return results


def condition_failure_analysis(diag):
    """Analyze which conditions fail most often for near-misses."""
    # Check if per-condition flags are available
    cond_cols = [c for c in diag.columns if c.startswith('cond_')]
    if not cond_cols:
        return None  # Need Phase 2 enhanced diagnostics

    results = {}
    for direction in ['bull', 'bear']:
        count_col = f'{direction}_conditions_passing'
        near_miss = diag[(diag[count_col] >= 5) & (diag[count_col] < 7)]
        if len(near_miss) == 0:
            results[direction] = {'near_misses': 0}
            continue

        failure_rates = {}
        for col in cond_cols:
            if direction == 'bear' and 'bull' in col:
                continue  # Skip bull-specific conditions for bear analysis
            if direction == 'bull' and 'bear' in col:
                continue
            fail_rate = float((near_miss[col] == 0).mean() * 100)
            failure_rates[col] = fail_rate

        results[direction] = {
            'near_misses': int(len(near_miss)),
            'failure_rates': dict(sorted(failure_rates.items(), key=lambda x: -x[1])),
        }
    return results


def mae_mfe_analysis(diag, regime='range', max_bars=24):
    """Compute MAE/MFE proxy for a given regime using forward price series."""
    subset = diag[diag['final_regime'] == regime].copy()
    prices = diag['price'].values
    indices = subset.index.values

    maes = []
    mfes = []
    for idx in indices:
        if idx + max_bars >= len(prices):
            continue
        entry = prices[idx]
        fwd_prices = prices[idx + 1:idx + max_bars + 1]
        returns = (fwd_prices - entry) / entry * 100
        maes.append(float(returns.min()))
        mfes.append(float(returns.max()))

    if not maes:
        return None

    return {
        'count': len(maes),
        'mae_mean': float(np.mean(maes)),
        'mae_median': float(np.median(maes)),
        'mae_p10': float(np.percentile(maes, 10)),
        'mfe_mean': float(np.mean(mfes)),
        'mfe_median': float(np.median(mfes)),
        'mfe_p90': float(np.percentile(mfes, 90)),
        'mfe_gt_0.5pct': float(np.mean(np.array(mfes) > 0.5) * 100),
    }


def print_forward_returns_table(fwd_by_regime, horizons=(6, 12, 24)):
    """Print forward returns table."""
    print("\n=== FORWARD RETURNS BY REGIME ===")
    header = f"{'Regime':>12s}  {'Count':>6s}"
    for h in horizons:
        header += f"  {'Mean':>7s}  {'Med':>7s}  {'%Pos':>5s}  {'Sharpe':>6s}"
    print(header)
    print(f"{'':>12s}  {'':>6s}" + f"  {'--- fwd_' + str(h) + ' ---':^30s}" * 0)

    for h in horizons:
        print(f"\n  fwd_{h} bars ({h * 5} min):")
        print(f"  {'Regime':>12s}  {'Count':>6s}  {'Mean%':>7s}  {'Med%':>7s}  "
              f"{'%Pos':>5s}  {'%Neg':>5s}  {'Sharpe':>7s}")
        print("  " + "-" * 65)
        for regime, stats in sorted(fwd_by_regime.items()):
            fwd_key = f'fwd_{h}'
            if fwd_key not in stats:
                continue
            s = stats[fwd_key]
            print(f"  {regime:>12s}  {stats['count']:>6d}  {s['mean']:>+7.4f}  "
                  f"{s['median']:>+7.4f}  {s['pct_positive']:>5.1f}  "
                  f"{s['pct_negative']:>5.1f}  {s['sharpe']:>+7.3f}")


def print_condition_count_table(fwd_by_count, direction, horizons=(6, 12, 24)):
    """Print forward returns by condition count."""
    print(f"\n=== FORWARD RETURNS BY {direction.upper()} CONDITIONS PASSING ===")
    for h in horizons:
        print(f"\n  fwd_{h} bars:")
        print(f"  {'Pass':>6s}  {'Count':>6s}  {'Mean%':>8s}  {'Med%':>8s}  {'%Pos':>5s}")
        print("  " + "-" * 45)
        for count in sorted(fwd_by_count.keys()):
            stats = fwd_by_count[count]
            fwd_key = f'fwd_{h}'
            if fwd_key not in stats:
                continue
            s = stats[fwd_key]
            label = f"{count}/7"
            print(f"  {label:>6s}  {stats['count']:>6d}  {s['mean']:>+8.4f}  "
                  f"{s['median']:>+8.4f}  {s['pct_positive']:>5.1f}")


def print_percentile_table(pct_results):
    """Print overall percentile table."""
    print("\n=== PERCENTILE TABLES (OVERALL) ===")
    pcts = ['p5', 'p10', 'p25', 'p50', 'p75', 'p90', 'p95']
    print(f"  {'Metric':>25s}  " + "  ".join(f"{p:>9s}" for p in pcts))
    print("  " + "-" * (25 + 2 + len(pcts) * 11))
    for metric, vals in pct_results['overall'].items():
        row = f"  {metric:>25s}  "
        row += "  ".join(f"{vals[p]:>9.6f}" for p in pcts)
        print(row)


def print_percentile_by_regime(pct_results):
    """Print percentile tables split by regime."""
    print("\n=== PERCENTILE TABLES BY REGIME ===")
    pcts = ['p25', 'p50', 'p75', 'p90']
    for regime in sorted(pct_results['by_regime'].keys()):
        regime_data = pct_results['by_regime'][regime]
        if not regime_data:
            continue
        print(f"\n  {regime}:")
        print(f"    {'Metric':>25s}  " + "  ".join(f"{p:>9s}" for p in pcts))
        print("    " + "-" * (25 + 2 + len(pcts) * 11))
        for metric, vals in regime_data.items():
            row = f"    {metric:>25s}  "
            row += "  ".join(f"{vals[p]:>9.6f}" for p in pcts)
            print(row)


def main():
    parser = argparse.ArgumentParser(description="Regime analysis: forward returns and calibration data")
    parser.add_argument("--diagnostics-csv", default=DEFAULT_DIAG_CSV,
                        help="Path to regime_diagnostics.csv")
    parser.add_argument("--candle-csv", default=DEFAULT_CANDLE_CSV,
                        help="Path to candle CSV")
    args = parser.parse_args()

    if not os.path.exists(args.diagnostics_csv):
        print(f"Diagnostics CSV not found: {args.diagnostics_csv}")
        print("Run: python -m backtest.run_baseline --diagnostics")
        sys.exit(1)

    print(f"Loading diagnostics: {args.diagnostics_csv}")
    diag, candles = load_data(args.diagnostics_csv, args.candle_csv)
    print(f"Diagnostics: {len(diag)} rows, Candles: {len(candles)} rows")
    print(f"Regimes: {dict(Counter(diag['final_regime']))}")

    # Compute forward returns
    diag = compute_forward_returns(diag, horizons=(6, 12, 24))

    # 1A: Forward returns by regime
    fwd_by_regime = forward_returns_by_regime(diag)
    print_forward_returns_table(fwd_by_regime)

    # 1B: Percentile tables
    pct = percentile_tables(diag)
    print_percentile_table(pct)
    print_percentile_by_regime(pct)

    # 1C: Condition failure analysis (needs Phase 2 condition flags)
    cond_analysis = condition_failure_analysis(diag)
    if cond_analysis:
        print("\n=== CONDITION FAILURE ANALYSIS (NEAR-MISSES) ===")
        for direction, data in cond_analysis.items():
            print(f"\n  {direction} near-misses: {data['near_misses']}")
            if 'failure_rates' in data:
                for cond, rate in data['failure_rates'].items():
                    print(f"    {cond:>35s}: {rate:5.1f}% fail")
    else:
        print("\n=== CONDITION FAILURE ANALYSIS ===")
        print("  (not available — run Phase 2 enhanced diagnostics first)")

    # 1D: Range evaluation
    print("\n=== RANGE TRADE EVALUATION ===")
    for regime in ['range', 'chop', 'unclear']:
        mam = mae_mfe_analysis(diag, regime=regime)
        if mam:
            print(f"\n  {regime} ({mam['count']} candles):")
            print(f"    MAE: mean={mam['mae_mean']:+.4f}%  median={mam['mae_median']:+.4f}%  p10={mam['mae_p10']:+.4f}%")
            print(f"    MFE: mean={mam['mfe_mean']:+.4f}%  median={mam['mfe_median']:+.4f}%  p90={mam['mfe_p90']:+.4f}%")
            print(f"    MFE > 0.5%: {mam['mfe_gt_0.5pct']:.1f}%")

    # 1E: Soft trend identification
    print("\n=== SOFT TREND IDENTIFICATION ===")
    for direction in ['bull', 'bear']:
        fwd_by_count = forward_returns_by_condition_count(diag, direction=direction)
        print_condition_count_table(fwd_by_count, direction)

    # Regime distribution
    print("\n=== REGIME DISTRIBUTION ===")
    total = len(diag)
    for regime, count in sorted(Counter(diag['final_regime']).items(), key=lambda x: -x[1]):
        pct_val = count / total * 100
        bar = '#' * int(pct_val / 2)
        print(f"  {regime:>12s}: {count:>6d} ({pct_val:5.1f}%)  {bar}")

    # Save JSON
    results_dir = os.path.join(project_root, 'backtest', 'results')
    os.makedirs(results_dir, exist_ok=True)
    output = {
        'forward_returns_by_regime': fwd_by_regime,
        'percentiles': pct,
        'condition_analysis': cond_analysis,
        'mae_mfe': {
            regime: mae_mfe_analysis(diag, regime=regime)
            for regime in diag['final_regime'].unique()
        },
        'regime_distribution': dict(Counter(diag['final_regime'])),
        'timestamp': datetime.now().isoformat(),
        'diagnostics_rows': len(diag),
    }
    output_path = os.path.join(results_dir, 'regime_analysis.json')
    with open(output_path, 'w') as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\nResults saved to {output_path}")


if __name__ == "__main__":
    main()
