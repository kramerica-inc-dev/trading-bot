#!/usr/bin/env python3
"""Fase 6 — A/B test of the deterministic bear-check (devil's advocate) module.

Runs the strategy twice on a *bounded holdout window* (the full 106k-bar 5m
series takes ~1.5h; this caps to the most-recent ``--max-bars`` candles, like
the Fase 5 calibrator) — once with ``bear_check.enabled = false`` (A, control)
and once with ``bear_check.enabled = true`` (B, treatment) — and reports for
both arms: trades, avg trade size, Sharpe (bar & trade), Calmar, max DD,
alpha-vs-BH, win rate, total return.

It also runs the "are high-bear-check trades worse?" check directly on the B
arm: correlates each trade's ``bear_check.score`` with its realized PnL (Pearson
and Spearman), and bins trades by score tercile.  No relationship => bear-check
doesn't work.

Per IMPROVEMENT_PLAN.md Fase 6 / gate 5: expectation is lower trade volume,
higher win rate, lower max DD.  If Calmar gets worse or there is no measurable
effect => recommend NOT activating.

Usage (from project root):
    python3 -m backtest.bear_check_ab
    python3 -m backtest.bear_check_ab --max-bars 12000 --risk-pct 1.0
    python3 -m backtest.bear_check_ab --csv backtest/data/BTC-USDT_5m.csv --out backtest/results/bear_check_ab.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))
sys.path.insert(0, PROJECT_ROOT)

from backtest.backtester import Backtester, BacktestConfig  # noqa: E402
from trading_strategy import create_strategy  # noqa: E402

DEFAULT_CSV = os.path.join("backtest", "data", "BTC-USDT_5m.csv")


def _load_candles(csv_path: str) -> pd.DataFrame:
    full = csv_path if os.path.isabs(csv_path) else os.path.join(PROJECT_ROOT, csv_path)
    df = pd.read_csv(full, parse_dates=["timestamp"])
    return df.sort_values("timestamp").reset_index(drop=True)


def _load_base_strategy_cfg(config_path: Optional[str]) -> Dict:
    if not config_path:
        return {}
    full = config_path if os.path.isabs(config_path) else os.path.join(PROJECT_ROOT, config_path)
    if not os.path.exists(full):
        return {}
    try:
        with open(full) as f:
            cfg = json.load(f)
        return dict(cfg.get("strategy", {}) or {})
    except Exception:
        return {}


def _make_bt_config(risk_per_trade_pct: float) -> BacktestConfig:
    # Same caveat as the Fase 5 calibrator: at risk_per_trade_pct=5 the 100%
    # notional cap binds and the size multiplier has no effect.  risk_pct=1.0
    # keeps risk-based size below the cap so the bear-check multiplier bites.
    return BacktestConfig(
        initial_balance=10000.0,
        fee_rate=0.0006,
        slippage_pct=0.05,
        risk_per_trade_pct=float(risk_per_trade_pct),
        min_confidence=0.45,
        allow_shorts=True,
        lookback_candles=200,
        contract_value=0.001,
        use_risk_multiplier=True,
        use_time_exits=True,
    )


def _strategy_config(base_strategy_cfg: Dict, bear_check_enabled: bool) -> Dict:
    cfg = dict(base_strategy_cfg)
    # MTF resampling needs HTF datasets we are not feeding here — disable so the
    # strategy stays consistent across both arms (same as Fase 4/5 scripts).
    mtf = dict(cfg.get("multi_timeframe", {}) or {})
    mtf["enabled"] = False
    cfg["multi_timeframe"] = mtf
    cfg["bear_check"] = {"enabled": bool(bear_check_enabled)}
    return cfg


def _run(df: pd.DataFrame, base_strategy_cfg: Dict, bear_check_enabled: bool,
         bt_config: BacktestConfig):
    strategy = create_strategy("advanced", _strategy_config(base_strategy_cfg, bear_check_enabled))
    backtester = Backtester(strategy, bt_config)
    return backtester.run(df)


def _metrics(res) -> Dict:
    sizes = [float(t.size) for t in res.trades] if res.trades else []
    return {
        "trades": int(res.total_trades),
        "avg_trade_size": float(np.mean(sizes)) if sizes else 0.0,
        "sharpe_bars": float(res.sharpe_ratio_bars),
        "sharpe_trades": float(res.sharpe_ratio),
        "calmar": float(res.calmar_ratio),
        "max_dd_pct": float(res.max_drawdown_pct),
        "alpha_vs_bh_pct": float(res.alpha_vs_benchmark_pct),
        "win_rate": float(res.win_rate),
        "total_return_pct": float(res.total_roi),
        "cagr": float(res.cagr),
    }


def _corr(xs: List[float], ys: List[float]):
    """Return (pearson_r, spearman_rho) — NaN-safe, returns (nan, nan) if degenerate."""
    if len(xs) < 3 or len(ys) < 3:
        return float("nan"), float("nan")
    x = np.asarray(xs, dtype=float)
    y = np.asarray(ys, dtype=float)
    if np.std(x) <= 1e-12 or np.std(y) <= 1e-12:
        return float("nan"), float("nan")
    pearson = float(np.corrcoef(x, y)[0, 1])
    # Spearman = Pearson on ranks.
    rx = pd.Series(x).rank().to_numpy()
    ry = pd.Series(y).rank().to_numpy()
    if np.std(rx) <= 1e-12 or np.std(ry) <= 1e-12:
        return pearson, float("nan")
    spearman = float(np.corrcoef(rx, ry)[0, 1])
    return pearson, spearman


def _score_vs_outcome(res) -> Dict:
    rows = [(float(t.bear_check.get("score", 0.0)), float(t.pnl), float(t.pnl_pct))
            for t in res.trades if isinstance(t.bear_check, dict) and t.bear_check]
    if not rows:
        return {"n_trades_with_bear_check": 0,
                "note": "no trades carried a bear_check record (B arm produced 0 trades?)"}
    scores = [r[0] for r in rows]
    pnls = [r[1] for r in rows]
    pnl_pcts = [r[2] for r in rows]
    pr_pnl, sp_pnl = _corr(scores, pnls)
    pr_pct, sp_pct = _corr(scores, pnl_pcts)
    # Terciles by score.
    order = np.argsort(scores)
    n = len(order)
    thirds = np.array_split(order, 3) if n >= 3 else [order]
    tercile_stats = []
    for i, idx in enumerate(thirds):
        if len(idx) == 0:
            continue
        s_sub = [scores[j] for j in idx]
        p_sub = [pnls[j] for j in idx]
        wins = sum(1 for v in p_sub if v > 0)
        tercile_stats.append({
            "tercile": ["low", "mid", "high"][i] if len(thirds) == 3 else "all",
            "n": len(idx),
            "score_min": float(min(s_sub)), "score_max": float(max(s_sub)),
            "score_mean": float(np.mean(s_sub)),
            "win_rate": wins / len(p_sub),
            "mean_pnl": float(np.mean(p_sub)),
            "total_pnl": float(np.sum(p_sub)),
        })
    return {
        "n_trades_with_bear_check": n,
        "score_mean": float(np.mean(scores)), "score_std": float(np.std(scores)),
        "score_min": float(min(scores)), "score_max": float(max(scores)),
        "pearson_score_vs_pnl": None if math.isnan(pr_pnl) else pr_pnl,
        "spearman_score_vs_pnl": None if math.isnan(sp_pnl) else sp_pnl,
        "pearson_score_vs_pnl_pct": None if math.isnan(pr_pct) else pr_pct,
        "spearman_score_vs_pnl_pct": None if math.isnan(sp_pct) else sp_pct,
        "terciles": tercile_stats,
        "interpretation": (
            "score variance ~0 — bear-check produced (almost) identical scores across trades; "
            "no signal to correlate"
            if np.std(scores) < 1e-6 else
            "negative correlation => high-bear-check trades fared worse (bear-check 'works'); "
            "near-zero / positive => no relationship => bear-check does not work"
        ),
    }


def run_ab(csv_path: str, max_bars: Optional[int], risk_pct: float,
           config_path: Optional[str], out_path: Optional[str]) -> Dict:
    df_all = _load_candles(csv_path)
    if max_bars is not None and len(df_all) > max_bars:
        df_all = df_all.iloc[-max_bars:].reset_index(drop=True)
        print(f"Restricted to the most recent {max_bars} candles "
              f"({df_all['timestamp'].iloc[0]} -> {df_all['timestamp'].iloc[-1]})")
    if len(df_all) < 2000:
        raise RuntimeError(f"Not enough candles ({len(df_all)}) for a meaningful A/B test")

    bt_config = _make_bt_config(risk_pct)
    base_strategy_cfg = _load_base_strategy_cfg(config_path)
    if base_strategy_cfg:
        print(f"Using strategy params from {config_path}: {sorted(base_strategy_cfg.keys())}")
    else:
        print("No config strategy section found — using strategy defaults")
    print(f"Holdout window: {len(df_all)} bars "
          f"({df_all['timestamp'].iloc[0]} -> {df_all['timestamp'].iloc[-1]}), "
          f"risk_per_trade_pct={risk_pct}, leverage=1, notional_cap=100%\n")

    print("Running arm A (bear_check.enabled=false, control) ...")
    res_a = _run(df_all, base_strategy_cfg, False, bt_config)
    m_a = _metrics(res_a)
    print("Running arm B (bear_check.enabled=true, treatment) ...")
    res_b = _run(df_all, base_strategy_cfg, True, bt_config)
    m_b = _metrics(res_b)

    sv = _score_vs_outcome(res_b)

    def _fmt(m: Dict) -> str:
        return (f"trades={m['trades']:>4d}  avgSize={m['avg_trade_size']:>6.2f}  "
                f"Sharpe(bar)={m['sharpe_bars']:+.2f}  Sharpe(trade)={m['sharpe_trades']:+.2f}  "
                f"Calmar={m['calmar']:+.3f}  maxDD={m['max_dd_pct']:>6.2f}%  "
                f"alphaBH={m['alpha_vs_bh_pct']:+.2f}%  win={m['win_rate']*100:>5.1f}%  "
                f"ret={m['total_return_pct']:+.2f}%")

    print("\n" + "=" * 100)
    print(f"  A (off): {_fmt(m_a)}")
    print(f"  B (on) : {_fmt(m_b)}")
    print("-" * 100)
    print(f"  Δ trades         : {m_b['trades'] - m_a['trades']:+d}")
    print(f"  Δ Calmar         : {m_b['calmar'] - m_a['calmar']:+.3f}")
    print(f"  Δ max DD (pp)    : {m_b['max_dd_pct'] - m_a['max_dd_pct']:+.2f}")
    print(f"  Δ win rate (pp)  : {(m_b['win_rate'] - m_a['win_rate'])*100:+.2f}")
    print(f"  Δ alpha-vs-BH(pp): {m_b['alpha_vs_bh_pct'] - m_a['alpha_vs_bh_pct']:+.2f}")
    print("=" * 100)
    print("\nBear-check score vs outcome (B arm):")
    print(f"  trades with bear_check record: {sv.get('n_trades_with_bear_check', 0)}")
    if sv.get("n_trades_with_bear_check", 0):
        print(f"  score: mean={sv['score_mean']:.3f} std={sv['score_std']:.3f} "
              f"[{sv['score_min']:.3f}, {sv['score_max']:.3f}]")
        print(f"  Pearson(score, pnl)  = {sv['pearson_score_vs_pnl']}")
        print(f"  Spearman(score, pnl) = {sv['spearman_score_vs_pnl']}")
        for t in sv.get("terciles", []):
            print(f"    {t['tercile']:>4s} tercile: n={t['n']:>3d}  "
                  f"score[{t['score_min']:.3f},{t['score_max']:.3f}]  "
                  f"win={t['win_rate']*100:>5.1f}%  meanPnL={t['mean_pnl']:+.4f}  totPnL={t['total_pnl']:+.4f}")
        print(f"  => {sv['interpretation']}")

    # --- Verdict heuristic ---
    no_effect = (m_b['trades'] == m_a['trades']
                 and abs(m_b['calmar'] - m_a['calmar']) < 1e-6
                 and abs(m_b['max_dd_pct'] - m_a['max_dd_pct']) < 1e-6)
    calmar_worse = m_b['calmar'] < m_a['calmar'] - 1e-6
    activate = (not no_effect) and (not calmar_worse) and (m_b['calmar'] > m_a['calmar'])
    if no_effect:
        reason = ("no measurable effect (B identical to A on the holdout — likely the components "
                  "rarely trigger and/or the notional cap binds at this risk_pct) => DO NOT ACTIVATE")
    elif calmar_worse:
        reason = "bear-check made Calmar worse on the holdout => DO NOT ACTIVATE"
    elif not activate:
        reason = "bear-check did not improve Calmar on the holdout => DO NOT ACTIVATE"
    else:
        reason = "bear-check improved Calmar on the holdout (rare given the strategy's deep-red edge) => consider activation"
    print(f"\nVERDICT: {'ACTIVATE' if activate else 'DO NOT ACTIVATE'} — {reason}")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "phase": "Fase 6 — A/B test of the deterministic bear-check (devil's advocate)",
        "data": {
            "csv": csv_path, "config": config_path,
            "n_bars": len(df_all),
            "period": [str(df_all["timestamp"].iloc[0]), str(df_all["timestamp"].iloc[-1])],
            "strategy_params": base_strategy_cfg,
        },
        "sizing": {"risk_per_trade_pct": risk_pct, "leverage": 1.0, "max_position_notional_pct": 100.0},
        "arm_a_off": m_a,
        "arm_b_on": m_b,
        "deltas": {
            "trades": m_b['trades'] - m_a['trades'],
            "calmar": m_b['calmar'] - m_a['calmar'],
            "max_dd_pp": m_b['max_dd_pct'] - m_a['max_dd_pct'],
            "win_rate_pp": (m_b['win_rate'] - m_a['win_rate']) * 100,
            "alpha_vs_bh_pp": m_b['alpha_vs_bh_pct'] - m_a['alpha_vs_bh_pct'],
        },
        "bear_check_score_vs_outcome": sv,
        "verdict": {"activate": bool(activate), "reason": reason},
    }
    if out_path is None:
        out_path = os.path.join("backtest", "results",
                                f"bear_check_ab_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    full_out = out_path if os.path.isabs(out_path) else os.path.join(PROJECT_ROOT, out_path)
    Path(full_out).parent.mkdir(parents=True, exist_ok=True)
    with open(full_out, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nReport written to {full_out}")
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--csv", default=DEFAULT_CSV, help=f"5m candle CSV (default: {DEFAULT_CSV})")
    p.add_argument("--config", default="config.json", help="Bot config; its `strategy` section sets indicator params")
    p.add_argument("--max-bars", type=int, default=12000,
                   help="Cap to the most-recent N candles (default 12000; the full 5m series takes ~1.5h)")
    p.add_argument("--risk-pct", type=float, default=1.0,
                   help="risk_per_trade_pct (default 1.0 = keeps size below the notional cap so the "
                        "bear-check multiplier bites; 5.0 = production but the cap masks the multiplier)")
    p.add_argument("--out", default=None, help="Output JSON path")
    args = p.parse_args()
    try:
        run_ab(csv_path=args.csv, max_bars=args.max_bars, risk_pct=args.risk_pct,
               config_path=args.config, out_path=args.out)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
