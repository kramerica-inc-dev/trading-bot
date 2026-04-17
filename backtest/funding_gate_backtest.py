#!/usr/bin/env python3
"""Plan-A Step 3 — funding-gate grid backtest.

Runs the existing baseline backtest once, then replays the trade list
through a grid of funding-gate threshold pairs. For each (max_long,
min_short) pair the script splits the trade list into:

    - passed trades:   would still execute with the gate active
    - filtered trades: would be skipped by the gate

and computes the full Plan-A metric set on both sets:

    Sharpe, max drawdown, win rate, trade count, expectancy,
    gross vs net P&L, fee share of gross alpha, filtered winners vs
    losers, and a per-regime breakdown of gate impact.

**Approximation.** Post-filtering trades is not identical to re-running
the strategy with the gate active (skipping trade N could let trade N+1
fire earlier because the bot is flat). For a strategy that is flat most
of the time this effect is small; it is good enough for the step-3 go/
no-go decision. A full strategy-side integration happens in Step 5.

Usage:
    python -m backtest.funding_gate_backtest
    python -m backtest.funding_gate_backtest --days 180

Writes:
    backtest/results/funding_gate_grid.csv
    backtest/results/funding_gate_regime_grid.csv
    backtest/results/funding_gate_summary.md
"""

from __future__ import annotations

import argparse
import itertools
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, 'scripts'))
sys.path.insert(0, project_root)

from backtest.backtester import Backtester, BacktestConfig, BacktestTrade
from trading_strategy import create_strategy


# --- Grid of thresholds to sweep (per-8h funding rate, as decimal) ---
#
# Historical range on BTC-USDT perp over the last 365d is roughly
# -0.045% to +0.028%, so thresholds above +0.03%/below -0.05% will
# filter nothing. The grid below is deliberately skewed toward
# thresholds that can actually fire on the available data.

MAX_LONG_GRID = [0.00005, 0.0001, 0.00015, 0.0002, 0.00025, float("inf")]
# ^ 0.005%, 0.01%, 0.015%, 0.02%, 0.025%, disabled
MIN_SHORT_GRID = [-0.00005, -0.0001, -0.00015, -0.0002, -0.0003, float("-inf")]
# ^ -0.005%, -0.01%, -0.015%, -0.02%, -0.03%, disabled

BASELINE_STRATEGY_CONFIG = {
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


# =====================================================================
# Funding data alignment
# =====================================================================

def load_funding(path: Path) -> pd.DataFrame:
    """Load funding CSV, return sorted-by-time DataFrame."""
    df = pd.read_csv(path)
    df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
    df = df.sort_values('fundingTime').reset_index(drop=True)
    return df[['fundingTime', 'fundingRate', 'timestamp']]


def funding_at(funding_df: pd.DataFrame, entry_time: datetime) -> Optional[float]:
    """Return the funding rate in effect at entry_time (last settlement).

    Funding settles every 8h; between settlements the rate applicable to
    a position held across the next boundary is the current (most
    recently published) fundingRate. We use last-known-before-or-at as
    the gate input — this is what a live system sees.
    """
    if pd.isna(entry_time):
        return None
    # Ensure tz-aware UTC
    if entry_time.tzinfo is None:
        entry_time = entry_time.replace(tzinfo=None)
        ts_ms = int(pd.Timestamp(entry_time, tz='UTC').timestamp() * 1000)
    else:
        ts_ms = int(entry_time.timestamp() * 1000)

    # Rows strictly at or before entry time
    mask = funding_df['fundingTime'] <= ts_ms
    if not mask.any():
        return None
    return float(funding_df.loc[mask, 'fundingRate'].iloc[-1])


# =====================================================================
# Metric computation
# =====================================================================

@dataclass
class GateMetrics:
    trades: int
    win_rate: float
    avg_win: float
    avg_loss: float
    expectancy: float          # per-trade expected $ P&L
    gross_pnl: float           # price-move P&L, no fees
    net_pnl: float             # after fees (estimated below)
    fee_total: float
    fee_share: float           # fees / gross_pnl (abs)
    sharpe: float
    max_drawdown_pct: float
    long_trades: int
    short_trades: int


def compute_metrics(trades: List[BacktestTrade], *,
                    initial_balance: float,
                    fee_rate: float,
                    contract_value: float) -> GateMetrics:
    """Compute the Plan-A metric set for an arbitrary list of trades.

    Important caveat on gross vs net: BacktestTrade.pnl already includes
    fees (and slippage via adjusted entry/exit prices — see backtester).
    We re-derive the fee component using the exact same formula the
    backtester applies — fee = fee_rate * size * contract_value * price,
    on entry + exit — and add it back to recover a gross figure.
    """
    n = len(trades)
    if n == 0:
        return GateMetrics(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                           0.0, 0.0, 0, 0)

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    wr = len(wins) / n
    avg_win = float(np.mean([t.pnl for t in wins])) if wins else 0.0
    avg_loss = float(np.mean([t.pnl for t in losses])) if losses else 0.0
    expectancy = (avg_win * wr) + (avg_loss * (1.0 - wr))

    # Fee formula matches backtester._calculate_pnl:
    #   entry_fee = fee_rate * size * contract_value * entry_price
    #   exit_fee  = fee_rate * size * contract_value * exit_price
    fee_total = 0.0
    for t in trades:
        fee_total += fee_rate * t.size * contract_value * (
            t.entry_price + t.exit_price)
    net_pnl = sum(t.pnl for t in trades)
    gross_pnl = net_pnl + fee_total  # add fees back to recover gross
    fee_share = (fee_total / abs(gross_pnl)) if gross_pnl != 0 else 0.0

    # Equity curve on this subset (chronological)
    equity = [initial_balance]
    for t in sorted(trades, key=lambda x: x.entry_time):
        equity.append(equity[-1] + t.pnl)
    eq = np.array(equity)
    peak = np.maximum.accumulate(eq)
    dd_pct = float(np.max((peak - eq) / peak) * 100) if len(eq) > 1 else 0.0

    # Sharpe (annualized by observed trade cadence)
    rets = np.array([t.pnl_pct / 100.0 for t in trades])
    if len(rets) > 1 and rets.std() > 0:
        sorted_trades = sorted(trades, key=lambda x: x.entry_time)
        span_days = max(
            1.0,
            (sorted_trades[-1].entry_time - sorted_trades[0].entry_time)
                .total_seconds() / 86400)
        trades_per_year = (n / span_days) * 365.0
        sharpe = float(rets.mean() / rets.std() * np.sqrt(trades_per_year))
    else:
        sharpe = 0.0

    return GateMetrics(
        trades=n, win_rate=wr, avg_win=avg_win, avg_loss=avg_loss,
        expectancy=expectancy, gross_pnl=gross_pnl, net_pnl=net_pnl,
        fee_total=fee_total, fee_share=fee_share, sharpe=sharpe,
        max_drawdown_pct=dd_pct,
        long_trades=sum(1 for t in trades if t.side == "buy"),
        short_trades=sum(1 for t in trades if t.side == "sell"),
    )


# =====================================================================
# Gate logic
# =====================================================================

def gate_split(trades: List[BacktestTrade], funding_df: pd.DataFrame,
               max_long: float, min_short: float
               ) -> tuple[List[BacktestTrade], List[BacktestTrade]]:
    """Split trades by whether the funding gate would allow them.

    Rules:
      - Long (buy) entry skipped iff funding > max_long
      - Short (sell) entry skipped iff funding < min_short
      - Trades with no funding data available are kept (fail-open)
    """
    passed, filtered = [], []
    for t in trades:
        fr = funding_at(funding_df, t.entry_time)
        if fr is None:
            passed.append(t)
            continue
        if t.side == "buy" and fr > max_long:
            filtered.append(t)
        elif t.side == "sell" and fr < min_short:
            filtered.append(t)
        else:
            passed.append(t)
    return passed, filtered


# =====================================================================
# Regime-segmented report
# =====================================================================

def regime_breakdown(trades: List[BacktestTrade],
                     filtered: List[BacktestTrade]) -> pd.DataFrame:
    """Per-regime gate impact.

    For each regime produces: baseline_trades, passed_trades, skipped_trades,
    baseline_wr, passed_wr, skipped_wr, baseline_pnl, passed_pnl, skipped_pnl.
    """
    regimes = sorted({t.regime for t in trades} | {t.regime for t in filtered})
    rows = []
    passed_set = {id(t) for t in trades}
    # trades contains ALL baseline trades; we get passed via set difference
    filtered_ids = {id(t) for t in filtered}
    for r in regimes:
        baseline_r = [t for t in trades if t.regime == r]
        skipped_r = [t for t in filtered if t.regime == r]
        passed_r = [t for t in baseline_r if id(t) not in filtered_ids]

        def _pct(lst):
            wins = sum(1 for t in lst if t.pnl > 0)
            return (wins / len(lst)) if lst else 0.0

        def _pnl(lst):
            return sum(t.pnl for t in lst)

        rows.append({
            'regime': r,
            'baseline_trades': len(baseline_r),
            'passed_trades': len(passed_r),
            'skipped_trades': len(skipped_r),
            'baseline_wr': _pct(baseline_r),
            'passed_wr': _pct(passed_r),
            'skipped_wr': _pct(skipped_r),
            'baseline_pnl': _pnl(baseline_r),
            'passed_pnl': _pnl(passed_r),
            'skipped_pnl': _pnl(skipped_r),
        })
    return pd.DataFrame(rows)


# =====================================================================
# Main
# =====================================================================

def run_baseline_backtest(csv_path: Path, days: Optional[int]
                          ) -> tuple[List[BacktestTrade], datetime, datetime]:
    """Run the existing baseline backtest once and return its trade list."""
    df = pd.read_csv(csv_path)
    df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
    if days is not None:
        cutoff = df['timestamp'].max() - pd.Timedelta(days=days)
        df = df[df['timestamp'] >= cutoff].reset_index(drop=True)
    print(f"Loaded {len(df)} candles "
          f"from {df['timestamp'].iloc[0]} to {df['timestamp'].iloc[-1]}")

    strategy = create_strategy("advanced", dict(BASELINE_STRATEGY_CONFIG))
    backtester = Backtester(strategy, BASELINE_BACKTEST)
    result = backtester.run(df)
    print(f"Baseline produced {len(result.trades)} trades")
    return result.trades, df['timestamp'].iloc[0], df['timestamp'].iloc[-1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candle-csv", default=None,
                        help="Path to candle CSV "
                             "(default: backtest/data/BTC-USDT_5m.csv)")
    parser.add_argument("--funding-csv", default=None,
                        help="Path to funding CSV "
                             "(default: backtest/data/funding_BTC-USDT.csv)")
    parser.add_argument("--days", type=int, default=None,
                        help="Restrict backtest to last N days "
                             "(default: all available candles)")
    parser.add_argument("--results-dir", default=None,
                        help="Where to write output "
                             "(default: backtest/results/)")
    args = parser.parse_args()

    backtest_root = Path(project_root) / 'backtest'
    candle_csv = Path(args.candle_csv) if args.candle_csv else \
        backtest_root / 'data' / 'BTC-USDT_5m.csv'
    funding_csv = Path(args.funding_csv) if args.funding_csv else \
        backtest_root / 'data' / 'funding_BTC-USDT.csv'
    results_dir = Path(args.results_dir) if args.results_dir else \
        backtest_root / 'results'
    results_dir.mkdir(parents=True, exist_ok=True)

    if not candle_csv.exists():
        print(f"Missing candle CSV: {candle_csv}", file=sys.stderr)
        return 1
    if not funding_csv.exists():
        print(f"Missing funding CSV: {funding_csv}\n"
              f"  Run: python -m backtest.funding_backfill", file=sys.stderr)
        return 1

    funding_df = load_funding(funding_csv)
    print(f"Loaded {len(funding_df)} funding rows "
          f"from {funding_df['timestamp'].iloc[0]} "
          f"to {funding_df['timestamp'].iloc[-1]}")

    trades, start_ts, end_ts = run_baseline_backtest(candle_csv, args.days)
    if not trades:
        print("Baseline produced no trades; nothing to evaluate.",
              file=sys.stderr)
        return 1

    # Baseline metrics (no gate)
    baseline_metrics = compute_metrics(
        trades,
        initial_balance=BASELINE_BACKTEST.initial_balance,
        fee_rate=BASELINE_BACKTEST.fee_rate,
        contract_value=BASELINE_BACKTEST.contract_value)

    # Grid sweep
    rows = []
    for max_long, min_short in itertools.product(MAX_LONG_GRID, MIN_SHORT_GRID):
        passed, filtered = gate_split(trades, funding_df, max_long, min_short)
        passed_m = compute_metrics(
            passed,
            initial_balance=BASELINE_BACKTEST.initial_balance,
            fee_rate=BASELINE_BACKTEST.fee_rate,
            contract_value=BASELINE_BACKTEST.contract_value)
        filt_m = compute_metrics(
            filtered,
            initial_balance=BASELINE_BACKTEST.initial_balance,
            fee_rate=BASELINE_BACKTEST.fee_rate,
            contract_value=BASELINE_BACKTEST.contract_value)

        rows.append({
            'max_long_pct': max_long * 100 if np.isfinite(max_long) else float("inf"),
            'min_short_pct': min_short * 100 if np.isfinite(min_short) else float("-inf"),
            # Passed (would still execute)
            'passed_trades': passed_m.trades,
            'passed_wr': passed_m.win_rate,
            'passed_expectancy': passed_m.expectancy,
            'passed_sharpe': passed_m.sharpe,
            'passed_max_dd_pct': passed_m.max_drawdown_pct,
            'passed_gross_pnl': passed_m.gross_pnl,
            'passed_net_pnl': passed_m.net_pnl,
            'passed_fees': passed_m.fee_total,
            'passed_fee_share': passed_m.fee_share,
            # Filtered (would be skipped) — what-if metrics
            'filtered_trades': filt_m.trades,
            'filtered_winners': sum(1 for t in filtered if t.pnl > 0),
            'filtered_losers': sum(1 for t in filtered if t.pnl <= 0),
            'filtered_wr': filt_m.win_rate,
            'filtered_net_pnl': filt_m.net_pnl,
            # Deltas vs baseline
            'delta_sharpe': passed_m.sharpe - baseline_metrics.sharpe,
            'delta_expectancy': passed_m.expectancy - baseline_metrics.expectancy,
            'delta_net_pnl': passed_m.net_pnl - baseline_metrics.net_pnl,
            'suppression_pct': (1.0 - passed_m.trades / baseline_metrics.trades) * 100
                               if baseline_metrics.trades else 0.0,
        })

    grid_df = pd.DataFrame(rows)
    grid_csv = results_dir / 'funding_gate_grid.csv'
    grid_df.to_csv(grid_csv, index=False)
    print(f"\nWrote grid to {grid_csv}")

    # Find best config by passed Sharpe (subject to suppression <= 50%)
    eligible = grid_df[grid_df['suppression_pct'] <= 50.0]
    if eligible.empty:
        print("WARNING: no grid point passes the ≤50% suppression constraint.")
        best_row = grid_df.sort_values('passed_sharpe', ascending=False).iloc[0]
    else:
        best_row = eligible.sort_values('passed_sharpe', ascending=False).iloc[0]

    # Regime breakdown for the best config
    best_passed, best_filtered = gate_split(
        trades, funding_df,
        best_row['max_long_pct'] / 100 if np.isfinite(best_row['max_long_pct']) else float("inf"),
        best_row['min_short_pct'] / 100 if np.isfinite(best_row['min_short_pct']) else float("-inf"),
    )
    regime_df = regime_breakdown(trades, best_filtered)
    regime_csv = results_dir / 'funding_gate_regime_grid.csv'
    regime_df.to_csv(regime_csv, index=False)
    print(f"Wrote regime breakdown to {regime_csv}")

    # Summary report
    summary_path = results_dir / 'funding_gate_summary.md'
    write_summary(summary_path, baseline_metrics, best_row, regime_df,
                  start_ts, end_ts, len(trades), grid_df)
    print(f"Wrote summary to {summary_path}")
    return 0


def write_summary(path: Path, baseline: GateMetrics, best: pd.Series,
                  regime_df: pd.DataFrame, start_ts, end_ts,
                  total_trades: int, grid_df: pd.DataFrame) -> None:
    lines = []
    lines.append("# Plan A — Step 3 funding-gate backtest summary\n")
    lines.append(f"**Generated:** {datetime.utcnow().isoformat(timespec='seconds')}Z")
    lines.append(f"**Data window:** {start_ts} → {end_ts}")
    lines.append(f"**Baseline trade count:** {total_trades}")
    lines.append("")
    lines.append("## Baseline (no gate)")
    lines.append(f"- Sharpe: {baseline.sharpe:.3f}")
    lines.append(f"- Win rate: {baseline.win_rate:.1%}")
    lines.append(f"- Expectancy / trade: ${baseline.expectancy:.2f}")
    lines.append(f"- Gross P&L: ${baseline.gross_pnl:.2f}")
    lines.append(f"- Net P&L:   ${baseline.net_pnl:.2f}")
    lines.append(f"- Fees:      ${baseline.fee_total:.2f} "
                 f"({baseline.fee_share:.1%} of gross)")
    lines.append(f"- Max DD:    {baseline.max_drawdown_pct:.2f}%")
    lines.append("")
    lines.append("## Best gate (≤50% suppression, ranked by Sharpe)")
    lines.append(f"- max_long: {best['max_long_pct']:.4f}%"
                 f"  min_short: {best['min_short_pct']:.4f}%")
    lines.append(f"- Passed trades: {int(best['passed_trades'])} "
                 f"(suppression {best['suppression_pct']:.1f}%)")
    lines.append(f"- Passed Sharpe: {best['passed_sharpe']:.3f} "
                 f"(Δ {best['delta_sharpe']:+.3f})")
    lines.append(f"- Passed WR:     {best['passed_wr']:.1%}")
    lines.append(f"- Passed expectancy: ${best['passed_expectancy']:.2f} "
                 f"(Δ {best['delta_expectancy']:+.2f})")
    lines.append(f"- Passed net P&L: ${best['passed_net_pnl']:.2f} "
                 f"(Δ {best['delta_net_pnl']:+.2f})")
    lines.append(f"- Fee share of gross: {best['passed_fee_share']:.1%}")
    lines.append(f"- Filtered: {int(best['filtered_trades'])} "
                 f"({int(best['filtered_winners'])} winners / "
                 f"{int(best['filtered_losers'])} losers, "
                 f"net ${best['filtered_net_pnl']:.2f})")
    lines.append("")
    lines.append("## Regime breakdown (best gate)")
    lines.append("")
    lines.append(regime_df.to_markdown(index=False, floatfmt=".2f"))
    lines.append("")
    lines.append("## Decision point")
    lines.append("")

    # Step 3 success criteria from the plan
    gate_helps = (best['delta_sharpe'] > 0
                  and best['suppression_pct'] <= 50.0
                  and best['passed_expectancy'] > baseline.expectancy)
    filter_wins_not_losers = best['filtered_winners'] <= best['filtered_losers']

    if gate_helps and filter_wins_not_losers:
        verdict = ("**PROCEED to Step 4-6.** The best gate improves Sharpe "
                   "and expectancy without suppressing >50% of trades, and "
                   "skips more losers than winners.")
    elif not gate_helps:
        verdict = ("**DO NOT PROCEED with funding gate as-is.** Best grid "
                   "point fails to improve Sharpe or expectancy within "
                   "the ≤50% suppression budget. Consider Plan C (OI "
                   "divergence) or Plan E (cross-sectional) instead.")
    else:
        verdict = ("**DO NOT PROCEED.** Gate skips more winners than "
                   "losers — it is destroying edge. Revert and reconsider.")

    lines.append(verdict)
    lines.append("")
    lines.append("See `funding_gate_grid.csv` for the full sweep and "
                 "`funding_gate_regime_grid.csv` for regime-level impact.")

    path.write_text("\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())
