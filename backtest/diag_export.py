#!/usr/bin/env python3
"""One-off: run the baseline backtest and export per-trade + per-bar CSVs for
the edge-diagnosis. Output: backtest/results/diag_trades.csv, diag_bars.csv."""
import os, sys, json
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, 'scripts')); sys.path.insert(0, project_root)
import pandas as pd
from backtest.backtester import Backtester
from backtest.run_baseline import BASELINE_CONFIG, BASELINE_BACKTEST, DEFAULT_CSV
from trading_strategy import create_strategy

df = pd.read_csv(DEFAULT_CSV); df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
strat = create_strategy("advanced", dict(BASELINE_CONFIG))
res = Backtester(strat, BASELINE_BACKTEST).run(df)
print(f"trades={len(res.trades)} bars={len(res.equity_curve)}")

rows = []
for t in res.trades:
    r = dict(entry_time=t.entry_time, exit_time=t.exit_time, side=t.side,
             entry_price=t.entry_price, exit_price=t.exit_price, size=t.size,
             pnl=t.pnl, pnl_pct=t.pnl_pct, exit_reason=t.exit_reason,
             confidence=t.confidence, regime=t.regime, bars_held=t.bars_held)
    for k, v in (t.indicators or {}).items():
        if isinstance(v, (int, float)): r[f"ind_{k}"] = v
    rows.append(r)
td = pd.DataFrame(rows)
out_t = os.path.join(project_root, 'backtest', 'results', 'diag_trades.csv')
td.to_csv(out_t, index=False); print("wrote", out_t, td.shape)

es = res.equity_series()
bd = pd.DataFrame(es)
if res.closes and len(res.closes) == len(bd): bd['btc_close'] = res.closes
out_b = os.path.join(project_root, 'backtest', 'results', 'diag_bars.csv')
bd.to_csv(out_b, index=False); print("wrote", out_b, bd.shape)

# also dump a small summary json
summ = dict(total_trades=res.total_trades, win_rate=res.win_rate, avg_win=res.avg_win,
            avg_loss=res.avg_loss, total_roi=res.total_roi, total_pnl=res.total_pnl,
            max_drawdown_pct=res.max_drawdown_pct, calmar=res.calmar_ratio,
            alpha_vs_bh=res.alpha_vs_benchmark_pct, benchmark=res.benchmark,
            long_trades=res.long_trades, short_trades=res.short_trades,
            exit_reason_counts=td['exit_reason'].value_counts().to_dict(),
            pnl_by_exit_reason=td.groupby('exit_reason')['pnl'].sum().to_dict())
out_s = os.path.join(project_root, 'backtest', 'results', 'diag_summary.json')
json.dump(summ, open(out_s, 'w'), indent=2, default=str); print("wrote", out_s)
print(json.dumps(summ, indent=2, default=str))
