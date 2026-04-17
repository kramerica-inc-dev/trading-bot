#!/usr/bin/env python3
"""Verify baseline improvement across candidate config fixes.

Runs the full 12-month backtest under several configurations to answer
the step-3 verification question: does fixing allow_shorts and the
TP multiplier bring WR above 30%?

Configs compared:
    A. Current baseline (no changes)
    B. allow_shorts: true only
    C. B + mean_reversion.take_profit_atr_mult: 2.5
    D. B + mean_reversion.take_profit_atr_mult: 3.0
    E. B + mean_reversion.take_profit_atr_mult: 3.5 + stop_loss_atr_mult: 2.0
    F. B + strategy.take_profit_atr_mult: 5.0 (the no-op sanity check)

Prints WR, expectancy, net P&L, fee share, long/short split per config.
"""
import os
import sys
from collections import Counter

import numpy as np
import pandas as pd

project_root = '/Users/michiel/Downloads/openclaw/blofin-trader'
sys.path.insert(0, os.path.join(project_root, 'scripts'))
sys.path.insert(0, project_root)

from backtest.backtester import Backtester, BacktestConfig
from trading_strategy import create_strategy


CONFIGS = {
    'A_current': {},
    'B_shorts_only': {
        'mean_reversion': {'allow_shorts': True},
    },
    'C_shorts_tp25': {
        'mean_reversion': {'allow_shorts': True,
                           'take_profit_atr_mult': 2.5},
    },
    'D_shorts_tp30': {
        'mean_reversion': {'allow_shorts': True,
                           'take_profit_atr_mult': 3.0},
    },
    'E_shorts_tp35_sl20': {
        'mean_reversion': {'allow_shorts': True,
                           'take_profit_atr_mult': 3.5,
                           'stop_loss_atr_mult': 2.0},
    },
    'F_trend_tp50_only': {
        'trend': {'take_profit_atr_mult': 5.0},
    },
}

BT_CFG = BacktestConfig(
    initial_balance=115.0, fee_rate=0.0006, slippage_pct=0.05,
    risk_per_trade_pct=5.0, min_confidence=0.45, allow_shorts=True,
    lookback_candles=200, contract_value=0.001,
    use_risk_multiplier=True, use_time_exits=True,
    stale_trade_atr_progress=0.18,
)

df = pd.read_csv(os.path.join(project_root, 'backtest/data/BTC-USDT_5m.csv'))
df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
print(f"Data: {len(df)} candles, "
      f"{df['timestamp'].min()} to {df['timestamp'].max()}\n")

header = ("config                 | trades  long/short | WR     | avg_win/loss   "
          "| expect    | net_pnl | fee/gross | regimes")
print(header)
print("-" * len(header))

for name, strategy_extras in CONFIGS.items():
    base_cfg = {'min_confidence': 0.45, 'min_votes': 2}
    base_cfg.update(strategy_extras)
    strategy = create_strategy("advanced", base_cfg)
    bt = Backtester(strategy, BT_CFG)
    result = bt.run(df)
    trades = result.trades
    if not trades:
        print(f"{name:22s} | 0 trades")
        continue
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    wr = len(wins) / len(trades)
    avg_w = np.mean([t.pnl for t in wins]) if wins else 0.0
    avg_l = np.mean([t.pnl for t in losses]) if losses else 0.0
    exp = (avg_w * wr) + (avg_l * (1 - wr))
    net = sum(t.pnl for t in trades)
    longs = sum(1 for t in trades if t.side == 'buy')
    shorts = sum(1 for t in trades if t.side == 'sell')
    fees = sum(BT_CFG.fee_rate * t.size * BT_CFG.contract_value *
               (t.entry_price + t.exit_price) for t in trades)
    gross = net + fees
    fee_ratio = (fees / abs(gross)) if gross != 0 else float('inf')
    regimes = Counter(t.regime for t in trades)
    regime_str = ",".join(f"{r}:{c}" for r, c in regimes.most_common(3))

    print(f"{name:22s} | {len(trades):4d}  {longs:4d}/{shorts:4d} "
          f"| {wr:5.1%} | {avg_w:+.3f}/{avg_l:+.3f} "
          f"| {exp:+.3f}  | {net:+.2f} | {fee_ratio:5.2f}     "
          f"| {regime_str}")
