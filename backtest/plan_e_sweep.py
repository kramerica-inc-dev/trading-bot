#!/usr/bin/env python3
"""Plan E (ε) parameter sweep:
- lookback horizon: 6h, 24h, 72h (3d), 168h (7d), 720h (30d)
- rebalance cadence: 4h, 24h, 168h
- sign: +1 (momentum) / -1 (reversal)

Writes backtest/results/PLAN-E-sweep.md with a table across all configs.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backtest.plan_e_cross_sectional import (  # noqa: E402
    UNIVERSE, INITIAL_BALANCE, LONG_N, SHORT_N, LEG_NOTIONAL_PCT,
    FEE_RATE, SLIPPAGE_RATE, COST_PER_SIDE, load_universe,
)


def run_backtest_params(
    closes: pd.DataFrame,
    lookback_h: int,
    rebalance_h: int,
    sign: int,
) -> dict:
    simple_ret = closes.pct_change().values
    signal = np.log(closes / closes.shift(lookback_h)).values * sign
    ts = closes.index
    n_bars, n_assets = closes.shape

    equity = np.full(n_bars, INITIAL_BALANCE)
    prev_weights = np.zeros(n_assets)
    eq = INITIAL_BALANCE
    fee_total = 0.0
    gross_pnl_total = 0.0
    n_trades = 0
    start_idx = lookback_h

    for i in range(start_idx, n_bars):
        r = simple_ret[i]
        bar_pnl = 0.0 if np.isnan(r).any() else float(np.sum(prev_weights * eq * r))
        gross_pnl_total += bar_pnl
        eq_before_fees = eq + bar_pnl

        if ts[i].hour % rebalance_h == 0 and not np.isnan(signal[i]).any():
            ranks = np.argsort(-signal[i])
            longs = ranks[:LONG_N]
            shorts = ranks[-SHORT_N:]
            new_w = np.zeros(n_assets)
            new_w[longs] = LEG_NOTIONAL_PCT
            new_w[shorts] = -LEG_NOTIONAL_PCT
            turnover = float(np.sum(np.abs(new_w - prev_weights)))
            fee = eq_before_fees * turnover * COST_PER_SIDE
            fee_total += fee
            eq = eq_before_fees - fee
            prev_weights = new_w
            n_trades += 1
        else:
            eq = eq_before_fees

        equity[i] = eq

    # Metrics
    start = start_idx
    rr = np.diff(equity[start:]) / equity[start:-1]
    rr = rr[np.isfinite(rr)]
    ann = 24 * 365
    mean_r = rr.mean() if len(rr) else 0.0
    std_r = rr.std(ddof=1) if len(rr) > 1 else 0.0
    sharpe = (mean_r * ann) / (std_r * np.sqrt(ann)) if std_r > 0 else 0.0
    peak = np.maximum.accumulate(equity[start:])
    dd = (equity[start:] - peak) / peak
    max_dd = dd.min() if len(dd) else 0.0

    return {
        "lookback_h": lookback_h,
        "rebalance_h": rebalance_h,
        "sign": sign,
        "n_trades": n_trades,
        "final_eq": float(equity[-1]),
        "net_pnl": float(equity[-1] - INITIAL_BALANCE),
        "gross_pnl": float(gross_pnl_total),
        "fee_total": float(fee_total),
        "return_pct": float((equity[-1] - INITIAL_BALANCE) / INITIAL_BALANCE * 100),
        "sharpe": float(sharpe),
        "max_dd_pct": float(max_dd * 100),
    }


def main() -> int:
    closes = load_universe()
    print(f"Bars: {len(closes)}, assets: {len(closes.columns)}")

    LOOKBACKS = [6, 24, 72, 168, 720]
    REBALANCES = [4, 24, 168]
    SIGNS = [+1, -1]

    rows = []
    for lb in LOOKBACKS:
        for rb in REBALANCES:
            for s in SIGNS:
                if rb > lb:
                    continue  # nonsense
                r = run_backtest_params(closes, lb, rb, s)
                rows.append(r)
                tag = "MOM" if s > 0 else "REV"
                print(f"  lb={lb:4d}h rb={rb:4d}h {tag}: "
                      f"Sharpe={r['sharpe']:+.2f} "
                      f"net=${r['net_pnl']:+9.2f} ({r['return_pct']:+.1f}%) "
                      f"gross=${r['gross_pnl']:+9.2f} "
                      f"fee=${r['fee_total']:7.2f} "
                      f"dd={r['max_dd_pct']:+.1f}% "
                      f"n_rb={r['n_trades']}")

    df = pd.DataFrame(rows).sort_values("sharpe", ascending=False)
    _write(df)
    return 0


def _write(df: pd.DataFrame) -> None:
    path = PROJECT_ROOT / "backtest" / "results" / "PLAN-E-sweep.md"
    lines = []
    lines.append("# Plan E (ε) — parameter sweep")
    lines.append("")
    lines.append(f"**Generated:** {pd.Timestamp.now(tz='UTC').isoformat()}")
    lines.append(f"**Universe:** 10 assets")
    lines.append(f"**Deploy:** ${INITIAL_BALANCE:,.0f}, {LONG_N}L/{SHORT_N}S, "
                 f"{LEG_NOTIONAL_PCT:.0%} per leg")
    lines.append(f"**Signal:** trailing log-return × sign (+1=momentum, -1=reversal)")
    lines.append("")
    lines.append("## All configurations (sorted by Sharpe)")
    lines.append("")
    lines.append("| Lookback | Rebalance | Sign | Rebalances | Net P&L | Return % | Gross | Fees | Fee share | Sharpe | Max DD |")
    lines.append("|----------|-----------|------|------------|---------|----------|-------|------|-----------|--------|--------|")
    for _, r in df.iterrows():
        tag = "MOM" if r["sign"] > 0 else "REV"
        fee_share = r["fee_total"] / abs(r["gross_pnl"]) if abs(r["gross_pnl"]) > 1e-6 else float("nan")
        lines.append(
            f"| {int(r['lookback_h'])}h | {int(r['rebalance_h'])}h | {tag} | "
            f"{int(r['n_trades'])} | ${r['net_pnl']:+,.2f} | {r['return_pct']:+.1f}% | "
            f"${r['gross_pnl']:+,.2f} | ${r['fee_total']:,.2f} | "
            f"{fee_share:.1%} | {r['sharpe']:+.2f} | {r['max_dd_pct']:+.1f}% |"
        )
    lines.append("")

    best = df.iloc[0]
    lines.append("## Best configuration")
    lines.append("")
    lines.append(f"- Lookback: **{int(best['lookback_h'])}h**, "
                 f"Rebalance: **{int(best['rebalance_h'])}h**, "
                 f"Sign: **{'MOM' if best['sign'] > 0 else 'REV'}**")
    lines.append(f"- Net P&L: ${best['net_pnl']:+,.2f} ({best['return_pct']:+.1f}%)")
    lines.append(f"- Sharpe: {best['sharpe']:+.2f}")
    lines.append(f"- Max DD: {best['max_dd_pct']:+.1f}%")
    lines.append("")

    gate = (best["sharpe"] > 1.0 and best["net_pnl"] > 0
            and best["max_dd_pct"] > -15.0)
    lines.append(f"**Gate:** {'PASS — proceed to walk-forward on best config' if gate else 'FAIL — no configuration passes gate'}")
    lines.append("")

    path.write_text("\n".join(lines))
    print(f"\nSweep written to {path}")


if __name__ == "__main__":
    sys.exit(main())
