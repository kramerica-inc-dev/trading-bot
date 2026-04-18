#!/usr/bin/env python3
"""Plan E (ε) — θ probe: rank hysteresis to reduce turnover.

Classic momentum implementation trick:
    - Enter a new long when asset enters the tight top-N rank.
    - EXIT an existing long only when it falls below a looser top-K_exit band.
    - K_exit > LONG_N means legs stay held through mild rank wiggles.

This should cut turnover without touching the signal.

Sweeps K_exit over {N, N+1, N+2, N+3} for the three most promising signal
configs from PLAN-E-sweep.md:
    - lb=72h,  rb=24h, REV  (best by Sharpe)
    - lb=168h, rb=24h, REV
    - lb=720h, rb=24h, REV
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backtest.plan_e_cross_sectional import (  # noqa: E402
    INITIAL_BALANCE, LONG_N, SHORT_N, LEG_NOTIONAL_PCT,
    COST_PER_SIDE, load_universe,
)


def run_hysteresis(
    closes: pd.DataFrame,
    lookback_h: int,
    rebalance_h: int,
    sign: int,
    k_exit: int,           # looser exit band (>= LONG_N); k_exit=LONG_N = no hysteresis
) -> dict:
    """Backtest with rank hysteresis.

    At each rebalance:
      - Rank assets by signal (descending).
      - Top LONG_N indices = "enter longs set". Bottom SHORT_N = "enter shorts set".
      - Top K_EXIT indices = "keep longs set". Bottom K_EXIT = "keep shorts set".
      - New longs = (current longs ∩ keep_longs) ∪ enter_longs, truncated to LONG_N.
      - Same for shorts.
    """
    simple_ret = closes.pct_change().values
    signal = np.log(closes / closes.shift(lookback_h)).values * sign
    ts = closes.index
    n_bars, n_assets = closes.shape

    equity = np.full(n_bars, INITIAL_BALANCE)
    prev_w = np.zeros(n_assets)
    eq = INITIAL_BALANCE
    fee_total = 0.0
    gross_pnl_total = 0.0
    n_trades = 0
    n_actual_rebals = 0
    start_idx = lookback_h

    for i in range(start_idx, n_bars):
        r = simple_ret[i]
        bar_pnl = 0.0 if np.isnan(r).any() else float(np.sum(prev_w * eq * r))
        gross_pnl_total += bar_pnl
        eq_before = eq + bar_pnl

        if ts[i].hour % rebalance_h == 0 and not np.isnan(signal[i]).any():
            n_trades += 1
            ranks = np.argsort(-signal[i])   # descending
            enter_long = set(ranks[:LONG_N].tolist())
            enter_short = set(ranks[-SHORT_N:].tolist())
            keep_long = set(ranks[:k_exit].tolist())
            keep_short = set(ranks[-k_exit:].tolist())

            cur_long = set(np.where(prev_w > 0)[0].tolist())
            cur_short = set(np.where(prev_w < 0)[0].tolist())

            # Longs: retained ∪ entrants, priority to retained
            retained_l = cur_long & keep_long
            need_l = LONG_N - len(retained_l)
            # Add entrants in rank order, skipping already-retained
            entrants_l = []
            for a in ranks:
                if len(entrants_l) >= need_l:
                    break
                if a in retained_l:
                    continue
                if a in enter_long or need_l > 0:
                    # Fall through: fill from top of ranks if enter_long alone can't cover
                    entrants_l.append(int(a))
            new_long_set = retained_l | set(entrants_l[:need_l])

            # Shorts: mirror
            retained_s = cur_short & keep_short
            need_s = SHORT_N - len(retained_s)
            entrants_s = []
            for a in ranks[::-1]:
                if len(entrants_s) >= need_s:
                    break
                if a in retained_s:
                    continue
                entrants_s.append(int(a))
            new_short_set = retained_s | set(entrants_s[:need_s])

            new_w = np.zeros(n_assets)
            for a in new_long_set:
                new_w[a] = LEG_NOTIONAL_PCT
            for a in new_short_set:
                new_w[a] = -LEG_NOTIONAL_PCT

            turnover = float(np.sum(np.abs(new_w - prev_w)))
            if turnover > 1e-9:
                fee = eq_before * turnover * COST_PER_SIDE
                fee_total += fee
                eq = eq_before - fee
                prev_w = new_w
                n_actual_rebals += 1
            else:
                eq = eq_before
        else:
            eq = eq_before

        equity[i] = eq

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
        "k_exit": k_exit,
        "rebalance_slots": n_trades,
        "actual_rebalances": n_actual_rebals,
        "net_pnl": float(equity[-1] - INITIAL_BALANCE),
        "gross_pnl": float(gross_pnl_total),
        "fee_total": float(fee_total),
        "return_pct": float((equity[-1] - INITIAL_BALANCE) / INITIAL_BALANCE * 100),
        "sharpe": float(sharpe),
        "max_dd_pct": float(max_dd * 100),
    }


def main() -> int:
    closes = load_universe()
    configs = [
        (72, 24, -1),    # best by gross Sharpe from PLAN-E-sweep
        (168, 24, -1),
        (720, 24, -1),
    ]
    k_exits = [LONG_N, LONG_N + 1, LONG_N + 2, LONG_N + 3]  # 3, 4, 5, 6

    rows = []
    for lb, rb, s in configs:
        for k in k_exits:
            r = run_hysteresis(closes, lb, rb, s, k)
            rows.append(r)
            print(f"  lb={lb:4d}h rb={rb}h REV k_exit={k}: "
                  f"Sharpe={r['sharpe']:+.2f} "
                  f"net=${r['net_pnl']:+8.2f} ({r['return_pct']:+.1f}%) "
                  f"gross=${r['gross_pnl']:+8.2f} "
                  f"fee=${r['fee_total']:7.2f} "
                  f"dd={r['max_dd_pct']:+.1f}% "
                  f"actual_rb={r['actual_rebalances']}/{r['rebalance_slots']}")

    df = pd.DataFrame(rows).sort_values("sharpe", ascending=False)
    _write(df)
    return 0


def _write(df: pd.DataFrame) -> None:
    path = PROJECT_ROOT / "backtest" / "results" / "PLAN-E-theta-hysteresis.md"
    lines = []
    lines.append("# Plan E — θ probe: rank hysteresis")
    lines.append("")
    lines.append(f"**Generated:** {pd.Timestamp.now(tz='UTC').isoformat()}")
    lines.append(f"**Setup:** 3 signal configs × 4 k_exit values; sign=REV (cross-sectional reversal)")
    lines.append(f"**k_exit=3 is no-hysteresis baseline (matches PLAN-E-sweep.md)**")
    lines.append("")
    lines.append("| Lookback | Rebalance | k_exit | Slots | Actual | Net P&L | Return % | Gross | Fees | Fee share | Sharpe | Max DD |")
    lines.append("|----------|-----------|--------|-------|--------|---------|----------|-------|------|-----------|--------|--------|")
    for _, r in df.iterrows():
        fee_share = r["fee_total"] / abs(r["gross_pnl"]) if abs(r["gross_pnl"]) > 1e-6 else float("nan")
        lines.append(
            f"| {int(r['lookback_h'])}h | {int(r['rebalance_h'])}h | {int(r['k_exit'])} | "
            f"{int(r['rebalance_slots'])} | {int(r['actual_rebalances'])} | "
            f"${r['net_pnl']:+,.2f} | {r['return_pct']:+.1f}% | "
            f"${r['gross_pnl']:+,.2f} | ${r['fee_total']:,.2f} | "
            f"{fee_share:.1%} | {r['sharpe']:+.2f} | {r['max_dd_pct']:+.1f}% |"
        )
    lines.append("")

    best = df.iloc[0]
    lines.append(f"## Best: lb={int(best['lookback_h'])}h, k_exit={int(best['k_exit'])}")
    lines.append("")
    lines.append(f"- Net P&L: ${best['net_pnl']:+,.2f} ({best['return_pct']:+.1f}%)")
    lines.append(f"- Sharpe: {best['sharpe']:+.2f}")
    lines.append(f"- Max DD: {best['max_dd_pct']:+.1f}%")
    lines.append(f"- Actual rebalances: {int(best['actual_rebalances'])} "
                 f"(vs {int(best['rebalance_slots'])} slots)")
    lines.append("")

    gate = (best["sharpe"] > 1.0 and best["net_pnl"] > 0
            and best["max_dd_pct"] > -15.0)
    lines.append(f"**Gate:** {'PASS — hysteresis closes the gap, proceed to walk-forward' if gate else 'FAIL — hysteresis helps but not enough; escalate to η (maker) or γ'}")
    lines.append("")

    path.write_text("\n".join(lines))
    print(f"\nReport written to {path}")


if __name__ == "__main__":
    sys.exit(main())
