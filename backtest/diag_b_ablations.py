#!/usr/bin/env python3
"""Axis B ablations: exit-geometry variants. Throwaway."""
import os, sys, json
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, 'scripts'))
sys.path.insert(0, project_root)
import pandas as pd
from backtest.backtester import Backtester, BacktestConfig
from trading_strategy import create_strategy

DEFAULT_CSV = os.path.join(project_root, 'backtest', 'data', 'BTC-USDT_5m.csv')
df = pd.read_csv(DEFAULT_CSV)
df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)

def base_bt(**kw):
    d = dict(initial_balance=115.0, fee_rate=0.0006, slippage_pct=0.05,
             risk_per_trade_pct=5.0, min_confidence=0.45, allow_shorts=True,
             lookback_candles=200, contract_value=0.001, use_risk_multiplier=True,
             use_time_exits=True, stale_trade_atr_progress=0.18)
    d.update(kw)
    return BacktestConfig(**d)

def strat(extra=None):
    cfg = {'min_confidence': 0.45, 'min_votes': 2}
    if extra: cfg.update(extra)
    return create_strategy("advanced", cfg)

def metrics(res):
    d = res.to_dict()
    return {
        'trades': d.get('total_trades'),
        'win_rate': round(d.get('win_rate', 0), 4),
        'return_pct': round(d.get('total_return_pct', 0), 2),
        'calmar': round(d.get('calmar_ratio', 0) or 0, 3),
        'max_dd_pct': round(d.get('max_drawdown_pct', 0), 2),
        'alpha_vs_bh_pct': round(d.get('alpha_vs_benchmark_pct', 0) or 0, 2),
        'win_pnl_usd': round(d.get('avg_win',0)* (d.get('total_trades',0)*d.get('win_rate',0)),2),
    }

runs = {}

# Baseline
r = Backtester(strat(), base_bt()).run(df); runs['baseline'] = metrics(r)
print('baseline keys:', list(r.to_dict().keys()))

# (a) TP very wide
r = Backtester(strat({'take_profit_atr_mult': 1000.0, 'mean_reversion': {'take_profit_atr_mult': 1000.0}, 'trend_following': {'take_profit_atr_mult': 1000.0}}), base_bt()).run(df)
runs['a_TP_off'] = metrics(r)

# (b) SL 2x and 3x
r = Backtester(strat({'stop_loss_atr_mult': 4.0, 'mean_reversion': {'stop_loss_atr_mult': 2.4}, 'trend_following': {'stop_loss_atr_mult': 3.6}}), base_bt()).run(df)
runs['b_SL_2x'] = metrics(r)
r = Backtester(strat({'stop_loss_atr_mult': 6.0, 'mean_reversion': {'stop_loss_atr_mult': 3.6}, 'trend_following': {'stop_loss_atr_mult': 5.4}}), base_bt()).run(df)
runs['b_SL_3x'] = metrics(r)

# (c) time exits off
r = Backtester(strat(), base_bt(use_time_exits=False)).run(df)
runs['c_no_time_exits'] = metrics(r)

# (d) entries only, exit at end of data: TP & SL effectively off + time exits off
r = Backtester(strat({'take_profit_atr_mult': 1000.0, 'stop_loss_atr_mult': 1000.0,
                      'mean_reversion': {'take_profit_atr_mult': 1000.0, 'stop_loss_atr_mult': 1000.0},
                      'trend_following': {'take_profit_atr_mult': 1000.0, 'stop_loss_atr_mult': 1000.0}}),
              base_bt(use_time_exits=False)).run(df)
runs['d_passive_hold'] = metrics(r)

# (e) TP/SL off, time exit on
r = Backtester(strat({'take_profit_atr_mult': 1000.0, 'stop_loss_atr_mult': 1000.0,
                      'mean_reversion': {'take_profit_atr_mult': 1000.0, 'stop_loss_atr_mult': 1000.0},
                      'trend_following': {'take_profit_atr_mult': 1000.0, 'stop_loss_atr_mult': 1000.0}}),
              base_bt(use_time_exits=True)).run(df)
runs['e_only_time_exit'] = metrics(r)

print(json.dumps(runs, indent=2))
with open('/tmp/diag_b_ablations.json', 'w') as f:
    json.dump(runs, f, indent=2)
