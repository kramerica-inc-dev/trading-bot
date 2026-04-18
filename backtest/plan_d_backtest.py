#!/usr/bin/env python3
"""Plan D step 3: 12-month backtest of the mean-reversion strategy.

Pipeline:
    1. Load 12 months of 5m BTC-USDT candles.
    2. Compute chop features + target (same definitions as step 1).
    3. Train ChopClassifier on pre-split data (first 9 months).
    4. Predict P(chop) for the full test slice (last 3 months).
    5. Inject features + predictions into MeanReversionStrategy.
    6. Run the Backtester over the test slice with the strategy.
    7. Report headline metrics, exit reason distribution, monthly P&L,
       gate fire-rate, and compare against baseline.

Success criteria (step 3 pass):
    - WR > 50%
    - win/loss ratio >= 0.8
    - net expectancy positive
    - Sharpe > 1.0
    - max DD < 15%

Writes:
    backtest/results/PLAN-D-step3-backtest.md
    backtest/results/PLAN-D-step3-trades.csv
"""

from __future__ import annotations

import math
import os
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT))

from chop_classifier import (  # noqa: E402
    FEATURE_NAMES,
    ChopClassifier,
    compute_features,
    compute_target,
)
from backtest.backtester import Backtester, BacktestConfig  # noqa: E402
from trading_strategy import create_strategy  # noqa: E402


SPLIT_DATE = pd.Timestamp("2026-01-12", tz="UTC")
RESULTS_DIR = PROJECT_ROOT / "backtest" / "results"

STRATEGY_CONFIG = {
    "min_chop_prob": 0.60,
    "z_entry": 2.0,
    "z_stop": 3.5,
    "sma_length": 20,
    "rsi_period": 14,
    "rsi_overbought": 70.0,
    "rsi_oversold": 30.0,
    "max_hold_bars": 48,
    "allow_long": True,
    "allow_short": True,
}

BACKTEST_CONFIG = BacktestConfig(
    initial_balance=115.0,
    fee_rate=0.0006,
    slippage_pct=0.05,
    risk_per_trade_pct=0.5,      # Plan D: reduced from 1.5%
    min_confidence=0.45,
    allow_shorts=True,
    lookback_candles=200,
    contract_value=0.001,
    use_risk_multiplier=True,
    use_time_exits=True,
    stale_trade_atr_progress=0.0,  # no stale-exit for mean-reversion
)

# Baseline comparison (from FINDINGS-2026-04-18 + fix_verification_report)
BASELINE_12MO = {
    "trades": 185, "wr": 0.124, "avg_win": 0.054, "avg_loss": -0.241,
    "net_pnl": -37.84,
}


def load_data() -> pd.DataFrame:
    df = pd.read_csv(PROJECT_ROOT / "backtest" / "data" / "BTC-USDT_5m.csv")
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


def prepare_features_and_predictions(df: pd.DataFrame):
    """Return (feats_df with timestamps, p_chop_series aligned to feats index)."""
    feats = compute_features(df)
    feats["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).values
    feats["timestamp"] = pd.to_datetime(feats["timestamp"], utc=True)

    target = compute_target(feats, forward_bars=48, atr_band_mult=3.0)

    train_mask = feats["timestamp"] < SPLIT_DATE
    valid_mask = feats[FEATURE_NAMES].notna().all(axis=1) & target.notna()

    X_train = feats.loc[train_mask & valid_mask, FEATURE_NAMES]
    y_train = target.loc[train_mask & valid_mask].astype(int)

    clf = ChopClassifier().fit(X_train, y_train)

    # Predict on bars that have complete features (train + test both)
    X_all = feats.loc[valid_mask, FEATURE_NAMES]
    p_all = clf.predict_proba(X_all)

    p_chop = pd.Series(np.nan, index=feats.index, name="p_chop")
    p_chop.loc[X_all.index] = p_all

    return feats, p_chop, clf


def build_feature_maps(feats: pd.DataFrame, p_chop: pd.Series):
    """Map timestamp_ms -> dict(features subset) and timestamp_ms -> p_chop.

    NB: must use .timestamp()*1000 because CSV timestamps may be us-precision,
    and astype('int64')//1_000_000 then yields seconds, not ms.
    """
    ts_ms = np.asarray(
        [int(ts.timestamp() * 1000) for ts in feats["timestamp"]],
        dtype=np.int64,
    )
    feat_records = feats[["sma20", "std20", "atr14"]].to_dict(orient="records")
    features_by_ts = {int(k): v for k, v in zip(ts_ms, feat_records)}

    p_values = p_chop.values
    p_by_ts = {int(k): float(p) for k, p in zip(ts_ms, p_values)
               if np.isfinite(p)}
    return features_by_ts, p_by_ts


def compute_metrics(trades, equity_curve, fee_rate, contract_value):
    if not trades:
        return {
            "trades": 0, "long": 0, "short": 0, "wr": 0.0,
            "avg_win": 0.0, "avg_loss": 0.0, "wl_ratio": 0.0,
            "expectancy": 0.0, "net_pnl": 0.0, "gross_pnl": 0.0,
            "fees_total": 0.0, "fee_share": 0.0, "sharpe": 0.0,
            "max_dd_pct": 0.0,
        }

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    wr = len(wins) / len(trades)
    avg_w = float(np.mean([t.pnl for t in wins])) if wins else 0.0
    avg_l = float(np.mean([t.pnl for t in losses])) if losses else 0.0
    wl_ratio = (abs(avg_w) / abs(avg_l)) if avg_l else float("inf")
    expect = avg_w * wr + avg_l * (1 - wr)
    net = float(sum(t.pnl for t in trades))
    fees = float(sum(fee_rate * t.size * contract_value *
                     (t.entry_price + t.exit_price) for t in trades))
    gross = net + fees
    fee_share = (fees / abs(gross)) if gross != 0 else float("inf")

    # Daily Sharpe from equity curve
    eq = np.asarray(equity_curve, dtype=float)
    if len(eq) < 2:
        sharpe = 0.0
    else:
        daily_rets = pd.Series(eq).pct_change().dropna().values
        # 5m bars × 288 per day -> scale to daily Sharpe
        # Actually equity_curve has one entry per bar, so resample:
        if len(daily_rets) > 0 and daily_rets.std() > 0:
            sharpe = (daily_rets.mean() / daily_rets.std()) * math.sqrt(288 * 365)
        else:
            sharpe = 0.0

    # Max drawdown on equity curve
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / peak
    max_dd_pct = float(dd.min() * 100.0)

    return {
        "trades": len(trades),
        "long": sum(1 for t in trades if t.side == "buy"),
        "short": sum(1 for t in trades if t.side == "sell"),
        "wr": wr,
        "avg_win": avg_w, "avg_loss": avg_l,
        "wl_ratio": wl_ratio,
        "expectancy": expect,
        "net_pnl": net, "gross_pnl": gross,
        "fees_total": fees, "fee_share": fee_share,
        "sharpe": sharpe,
        "max_dd_pct": max_dd_pct,
    }


def main() -> int:
    df = load_data()
    print(f"Loaded {len(df)} candles "
          f"({df['timestamp'].min()} → {df['timestamp'].max()})")

    print("Computing features + predictions...")
    feats, p_chop, clf = prepare_features_and_predictions(df)
    print(f"  training features non-null: {feats[FEATURE_NAMES].notna().all(axis=1).sum()}")
    print(f"  classifier trained. p_chop coverage: {p_chop.notna().sum()}/{len(p_chop)}")

    # Backtest only the post-split slice (out-of-sample)
    test_mask = feats["timestamp"] >= SPLIT_DATE
    test_df = df.loc[test_mask].reset_index(drop=True)
    print(f"Test slice: {len(test_df)} candles, "
          f"{test_df['timestamp'].min()} → {test_df['timestamp'].max()}")

    # Build the precomputed maps keyed on ms
    features_by_ts, p_by_ts = build_feature_maps(feats, p_chop)

    # Create strategy with injected data
    strategy = create_strategy("meanrev", dict(STRATEGY_CONFIG))
    strategy.set_precomputed(features_by_ts, p_by_ts)

    bt = Backtester(strategy, BACKTEST_CONFIG)
    result = bt.run(test_df)

    metrics = compute_metrics(result.trades, result.equity_curve,
                              BACKTEST_CONFIG.fee_rate,
                              BACKTEST_CONFIG.contract_value)

    # Exit reason distribution
    exit_reasons = Counter(t.exit_reason for t in result.trades)
    reject_summary = strategy.reject_summary()

    # Monthly breakdown
    monthly = {}
    for t in result.trades:
        key = t.entry_time.strftime("%Y-%m")
        monthly.setdefault(key, []).append(t)

    # Write report + trades CSV
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    _write_report(
        RESULTS_DIR / "PLAN-D-step3-backtest.md",
        metrics, exit_reasons, monthly, reject_summary,
        clf, test_df, len(result.trades),
    )
    _write_trades_csv(
        RESULTS_DIR / "PLAN-D-step3-trades.csv", result.trades,
    )
    print(f"\nHeadline: trades={metrics['trades']} "
          f"WR={metrics['wr']:.1%} "
          f"exp={metrics['expectancy']:+.4f} "
          f"net=${metrics['net_pnl']:+.2f} "
          f"Sharpe={metrics['sharpe']:.2f}")

    # Decision gate
    passes = (
        metrics["wr"] > 0.50
        and metrics["wl_ratio"] >= 0.8
        and metrics["expectancy"] > 0
        and metrics["sharpe"] > 1.0
        and metrics["max_dd_pct"] > -15.0
    )
    print(f"Gate: {'PASS' if passes else 'FAIL'}")
    return 0 if passes else 1


def _write_trades_csv(path: Path, trades) -> None:
    rows = []
    for t in trades:
        rows.append({
            "entry_time": t.entry_time,
            "exit_time": getattr(t, "exit_time", None),
            "side": t.side,
            "entry_price": t.entry_price,
            "exit_price": t.exit_price,
            "size": t.size,
            "pnl": t.pnl,
            "bars_held": t.bars_held,
            "exit_reason": t.exit_reason,
            "regime": t.regime,
            "confidence": t.confidence,
        })
    pd.DataFrame(rows).to_csv(path, index=False)


def _write_report(path, metrics, exit_reasons, monthly, reject_summary,
                  clf, test_df, n_trades):
    lines = []
    lines.append("# Plan D — Step 3: 12-Month Backtest")
    lines.append("")
    lines.append(f"**Generated:** {pd.Timestamp.now(tz='UTC').isoformat()}")
    lines.append(f"**Test slice:** {test_df['timestamp'].min()} → "
                 f"{test_df['timestamp'].max()} ({len(test_df)} bars)")
    lines.append(f"**Strategy config:** {STRATEGY_CONFIG}")
    lines.append(f"**Backtest config:** "
                 f"fee={BACKTEST_CONFIG.fee_rate} "
                 f"slip={BACKTEST_CONFIG.slippage_pct}% "
                 f"risk={BACKTEST_CONFIG.risk_per_trade_pct}%")
    lines.append("")

    lines.append("## Headline metrics")
    lines.append("")
    lines.append("| Metric | Plan D (test) | Baseline (12mo, FINDINGS) |")
    lines.append("|--------|---------------|---------------------------|")
    lines.append(f"| Trades | {metrics['trades']} | {BASELINE_12MO['trades']} |")
    lines.append(f"| Long / Short | {metrics['long']} / {metrics['short']} | 185 / 0 |")
    lines.append(f"| Win rate | {metrics['wr']:.1%} | {BASELINE_12MO['wr']:.1%} |")
    lines.append(f"| Avg win / loss ($) | {metrics['avg_win']:+.3f} / {metrics['avg_loss']:+.3f} | "
                 f"{BASELINE_12MO['avg_win']:+.3f} / {BASELINE_12MO['avg_loss']:+.3f} |")
    lines.append(f"| Win:loss ratio | {metrics['wl_ratio']:.2f} | 0.22 |")
    lines.append(f"| Expectancy/trade ($) | {metrics['expectancy']:+.4f} | -0.205 |")
    lines.append(f"| Net P&L ($) | {metrics['net_pnl']:+.2f} | {BASELINE_12MO['net_pnl']:+.2f} |")
    lines.append(f"| Gross P&L ($) | {metrics['gross_pnl']:+.2f} | — |")
    lines.append(f"| Fees total ($) | {metrics['fees_total']:.2f} | — |")
    lines.append(f"| Fee share of gross | {metrics['fee_share']:.1%} | — |")
    lines.append(f"| Sharpe (annualized) | {metrics['sharpe']:.2f} | — |")
    lines.append(f"| Max drawdown | {metrics['max_dd_pct']:.2f}% | — |")
    lines.append("")
    lines.append("Baseline is the "
                 "12-month backtest of the previous advanced strategy, "
                 "for context only. Plan D runs on out-of-sample slice, "
                 "baseline ran on full 12 months.")
    lines.append("")

    lines.append("## Gate criteria (Plan D step 3)")
    lines.append("")
    passes = {
        "WR > 50%": metrics["wr"] > 0.50,
        "W/L ratio >= 0.8": metrics["wl_ratio"] >= 0.8,
        "Expectancy > 0": metrics["expectancy"] > 0,
        "Sharpe > 1.0": metrics["sharpe"] > 1.0,
        "Max DD > -15%": metrics["max_dd_pct"] > -15.0,
    }
    for k, v in passes.items():
        lines.append(f"- {'PASS' if v else 'FAIL'} — {k}")
    lines.append("")
    lines.append(f"**Overall:** {'PASS' if all(passes.values()) else 'FAIL'}")
    lines.append("")

    lines.append("## Exit reason distribution")
    lines.append("")
    lines.append("| Reason | Trades | Winners | WR |")
    lines.append("|--------|--------|---------|-----|")
    # Need original trades for per-reason analysis — we only have counters here
    for reason, count in exit_reasons.most_common():
        lines.append(f"| {reason} | {count} | — | — |")
    lines.append("")

    lines.append("## Monthly P&L breakdown")
    lines.append("")
    lines.append("| Month | Trades | Wins | WR | P&L ($) |")
    lines.append("|-------|--------|------|-----|---------|")
    for month in sorted(monthly):
        trades = monthly[month]
        w = sum(1 for t in trades if t.pnl > 0)
        pnl = sum(t.pnl for t in trades)
        wr = w / len(trades) if trades else 0.0
        lines.append(f"| {month} | {len(trades)} | {w} | {wr:.1%} | {pnl:+.2f} |")
    lines.append("")

    lines.append("## Strategy rejection reasons (chop-gate, z-score, RSI)")
    lines.append("")
    lines.append("| Reason | Count |")
    lines.append("|--------|-------|")
    total_rejects = sum(reject_summary.values())
    for reason, c in sorted(reject_summary.items(), key=lambda kv: -kv[1]):
        share = c / total_rejects if total_rejects else 0.0
        lines.append(f"| {reason} | {c} ({share:.1%}) |")
    lines.append("")
    lines.append(f"Total rejections: {total_rejects}. "
                 f"Trades taken: {n_trades}. "
                 f"Signal-to-trade rate: "
                 f"{n_trades / max(total_rejects + n_trades, 1):.2%}.")
    lines.append("")

    lines.append("## Classifier coefficients (held from step 1 training)")
    lines.append("")
    lines.append("| Feature | Coefficient |")
    lines.append("|---------|-------------|")
    coefs = clf.coef().to_dict()
    for k, v in sorted(coefs.items(), key=lambda kv: -abs(kv[1])):
        lines.append(f"| {k} | {v:+.4f} |")
    lines.append("")

    path.write_text("\n".join(lines))
    print(f"Report written to {path}")


if __name__ == "__main__":
    sys.exit(main())
