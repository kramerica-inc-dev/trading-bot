#!/usr/bin/env python3
"""Plan E — θ refinement: wider k_exit grid + signal blend.

Two additions on top of plan_e_hysteresis:
    1. Wider k_exit grid around the peaks (k in {4,5,6,7,8}) for both
       promising signal configs.
    2. Signal blend: 50/50 combination of 72h and 720h reversal signals.
       Diverse-timescale blending is often Sharpe-additive even when each
       component is marginal.
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


def _run(closes: pd.DataFrame, signal_arr: np.ndarray, rebalance_h: int,
         k_exit: int, start_idx: int) -> dict:
    """Core loop shared between single-signal and blended-signal runs.

    signal_arr: pre-computed (n_bars, n_assets) signal (already sign-flipped).
    """
    simple_ret = closes.pct_change().values
    ts = closes.index
    n_bars, n_assets = closes.shape

    equity = np.full(n_bars, INITIAL_BALANCE)
    prev_w = np.zeros(n_assets)
    eq = INITIAL_BALANCE
    fee_total = 0.0
    gross_pnl_total = 0.0
    n_slots = 0
    n_actual = 0

    for i in range(start_idx, n_bars):
        r = simple_ret[i]
        bar_pnl = 0.0 if np.isnan(r).any() else float(np.sum(prev_w * eq * r))
        gross_pnl_total += bar_pnl
        eq_before = eq + bar_pnl

        if ts[i].hour % rebalance_h == 0 and not np.isnan(signal_arr[i]).any():
            n_slots += 1
            ranks = np.argsort(-signal_arr[i])
            keep_long = set(ranks[:k_exit].tolist())
            keep_short = set(ranks[-k_exit:].tolist())
            cur_long = set(np.where(prev_w > 0)[0].tolist())
            cur_short = set(np.where(prev_w < 0)[0].tolist())

            retained_l = cur_long & keep_long
            need_l = LONG_N - len(retained_l)
            new_long = set(retained_l)
            for a in ranks:
                if need_l <= 0:
                    break
                if int(a) in new_long:
                    continue
                new_long.add(int(a))
                need_l -= 1

            retained_s = cur_short & keep_short
            need_s = SHORT_N - len(retained_s)
            new_short = set(retained_s)
            for a in ranks[::-1]:
                if need_s <= 0:
                    break
                if int(a) in new_short:
                    continue
                new_short.add(int(a))
                need_s -= 1

            new_w = np.zeros(n_assets)
            for a in new_long:
                new_w[a] = LEG_NOTIONAL_PCT
            for a in new_short:
                new_w[a] = -LEG_NOTIONAL_PCT

            turnover = float(np.sum(np.abs(new_w - prev_w)))
            if turnover > 1e-9:
                fee = eq_before * turnover * COST_PER_SIDE
                fee_total += fee
                eq = eq_before - fee
                prev_w = new_w
                n_actual += 1
            else:
                eq = eq_before
        else:
            eq = eq_before

        equity[i] = eq

    rr = np.diff(equity[start_idx:]) / equity[start_idx:-1]
    rr = rr[np.isfinite(rr)]
    ann = 24 * 365
    mean_r = rr.mean() if len(rr) else 0.0
    std_r = rr.std(ddof=1) if len(rr) > 1 else 0.0
    sharpe = (mean_r * ann) / (std_r * np.sqrt(ann)) if std_r > 0 else 0.0
    peak = np.maximum.accumulate(equity[start_idx:])
    dd = (equity[start_idx:] - peak) / peak
    max_dd = dd.min() if len(dd) else 0.0

    return {
        "k_exit": k_exit,
        "actual_rebalances": n_actual,
        "rebalance_slots": n_slots,
        "net_pnl": float(equity[-1] - INITIAL_BALANCE),
        "gross_pnl": float(gross_pnl_total),
        "fee_total": float(fee_total),
        "return_pct": float((equity[-1] - INITIAL_BALANCE) / INITIAL_BALANCE * 100),
        "sharpe": float(sharpe),
        "max_dd_pct": float(max_dd * 100),
    }


def build_signal(closes: pd.DataFrame, lookback_h: int, sign: int) -> np.ndarray:
    return np.log(closes / closes.shift(lookback_h)).values * sign


def zscore_crosssectional(x: np.ndarray) -> np.ndarray:
    """Per-row (per-bar) z-score across assets, for signal blending."""
    mean = np.nanmean(x, axis=1, keepdims=True)
    std = np.nanstd(x, axis=1, keepdims=True)
    std = np.where(std < 1e-12, 1.0, std)
    return (x - mean) / std


def main() -> int:
    closes = load_universe()
    k_exits = [4, 5, 6, 7, 8]

    tests = []

    # Single-signal refinement
    for lb in [72, 720]:
        sig = build_signal(closes, lb, sign=-1)
        for k in k_exits:
            r = _run(closes, sig, rebalance_h=24, k_exit=k, start_idx=lb)
            r["config"] = f"lb={lb}h REV"
            tests.append(r)
            print(f"  lb={lb:4d}h REV k={k}: Sharpe={r['sharpe']:+.2f} "
                  f"net=${r['net_pnl']:+8.2f} ({r['return_pct']:+.1f}%) "
                  f"gross=${r['gross_pnl']:+8.2f} fee=${r['fee_total']:7.2f} "
                  f"dd={r['max_dd_pct']:+.1f}% rb={r['actual_rebalances']}")

    # Blended signal: z-score of 72h + 720h
    sig_72 = build_signal(closes, 72, sign=-1)
    sig_720 = build_signal(closes, 720, sign=-1)
    sig_blend = zscore_crosssectional(sig_72) + zscore_crosssectional(sig_720)
    for k in k_exits:
        r = _run(closes, sig_blend, rebalance_h=24, k_exit=k, start_idx=720)
        r["config"] = "blend(72+720) REV"
        tests.append(r)
        print(f"  blend(72+720) REV k={k}: Sharpe={r['sharpe']:+.2f} "
              f"net=${r['net_pnl']:+8.2f} ({r['return_pct']:+.1f}%) "
              f"gross=${r['gross_pnl']:+8.2f} fee=${r['fee_total']:7.2f} "
              f"dd={r['max_dd_pct']:+.1f}% rb={r['actual_rebalances']}")

    df = pd.DataFrame(tests).sort_values("sharpe", ascending=False)

    path = PROJECT_ROOT / "backtest" / "results" / "PLAN-E-theta-refine.md"
    lines = []
    lines.append("# Plan E — θ refinement: wider k_exit + signal blend")
    lines.append("")
    lines.append(f"**Generated:** {pd.Timestamp.now(tz='UTC').isoformat()}")
    lines.append("")
    lines.append("| Config | k_exit | Actual rb | Net P&L | Return % | Gross | Fees | Fee share | Sharpe | Max DD |")
    lines.append("|--------|--------|-----------|---------|----------|-------|------|-----------|--------|--------|")
    for _, r in df.iterrows():
        fee_share = r["fee_total"] / abs(r["gross_pnl"]) if abs(r["gross_pnl"]) > 1e-6 else float("nan")
        lines.append(
            f"| {r['config']} | {int(r['k_exit'])} | {int(r['actual_rebalances'])} | "
            f"${r['net_pnl']:+,.2f} | {r['return_pct']:+.1f}% | "
            f"${r['gross_pnl']:+,.2f} | ${r['fee_total']:,.2f} | "
            f"{fee_share:.1%} | {r['sharpe']:+.2f} | {r['max_dd_pct']:+.1f}% |"
        )
    lines.append("")

    best = df.iloc[0]
    lines.append(f"## Best: {best['config']}, k_exit={int(best['k_exit'])}")
    lines.append("")
    lines.append(f"- Net P&L: ${best['net_pnl']:+,.2f} ({best['return_pct']:+.1f}%)")
    lines.append(f"- Sharpe: {best['sharpe']:+.2f}")
    lines.append(f"- Max DD: {best['max_dd_pct']:+.1f}%")
    lines.append("")

    gate = (best["sharpe"] > 1.0 and best["net_pnl"] > 0
            and best["max_dd_pct"] > -15.0)
    lines.append(f"**Gate:** {'PASS — proceed to walk-forward' if gate else 'FAIL — escalate to η (maker execution)'}")
    lines.append("")
    path.write_text("\n".join(lines))
    print(f"\nReport written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
