#!/usr/bin/env python3
"""Plan E — η probe: sensitivity to execution cost (maker vs taker).

Take the robust θ config (lb=72h, rb=24h, REV, k_exit=6) and sweep
cost_per_side from 0 (zero-cost upper bound) to 0.0011 (current taker).

Maker model: assume fill rate F at the mid (zero slippage, fee rebate),
unfilled fraction (1-F) executes taker (fee + slippage).
    effective_cost_per_side = F * (-fee_rebate) + (1-F) * (fee_taker + slip)

Blofin maker rebate ≈ -0.02% (or 0 depending on tier).
Taker fee = 0.06%. Slippage = 0.05%. Conservative rebate = 0.

F ∈ {0.0, 0.3, 0.5, 0.7, 0.9, 1.0}.
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
    load_universe,
)

# Fixed config (θ-refinement winner)
LOOKBACK_H = 72
REBALANCE_H = 24
SIGN = -1
K_EXIT = 6

FEE_TAKER = 0.0006
FEE_MAKER = -0.0001   # conservative: 0.01% rebate (Blofin VIP can be higher)
SLIP_TAKER = 0.0005
SLIP_MAKER = 0.0000   # mid-limit assumed


def run_with_cost(closes: pd.DataFrame, cost_per_side: float) -> dict:
    simple_ret = closes.pct_change().values
    signal = np.log(closes / closes.shift(LOOKBACK_H)).values * SIGN
    ts = closes.index
    n_bars, n_assets = closes.shape

    equity = np.full(n_bars, INITIAL_BALANCE)
    prev_w = np.zeros(n_assets)
    eq = INITIAL_BALANCE
    fee_total = 0.0
    gross_pnl_total = 0.0
    n_actual = 0
    start_idx = LOOKBACK_H

    for i in range(start_idx, n_bars):
        r = simple_ret[i]
        bar_pnl = 0.0 if np.isnan(r).any() else float(np.sum(prev_w * eq * r))
        gross_pnl_total += bar_pnl
        eq_before = eq + bar_pnl

        if ts[i].hour % REBALANCE_H == 0 and not np.isnan(signal[i]).any():
            ranks = np.argsort(-signal[i])
            keep_long = set(ranks[:K_EXIT].tolist())
            keep_short = set(ranks[-K_EXIT:].tolist())
            cur_long = set(np.where(prev_w > 0)[0].tolist())
            cur_short = set(np.where(prev_w < 0)[0].tolist())

            retained_l = cur_long & keep_long
            new_long = set(retained_l)
            need_l = LONG_N - len(retained_l)
            for a in ranks:
                if need_l <= 0:
                    break
                if int(a) in new_long:
                    continue
                new_long.add(int(a))
                need_l -= 1

            retained_s = cur_short & keep_short
            new_short = set(retained_s)
            need_s = SHORT_N - len(retained_s)
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
                fee = eq_before * turnover * cost_per_side
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
        "cost_per_side": cost_per_side,
        "actual_rebalances": n_actual,
        "net_pnl": float(equity[-1] - INITIAL_BALANCE),
        "gross_pnl": float(gross_pnl_total),
        "fee_total": float(fee_total),
        "return_pct": float((equity[-1] - INITIAL_BALANCE) / INITIAL_BALANCE * 100),
        "sharpe": float(sharpe),
        "max_dd_pct": float(max_dd * 100),
    }


def effective_cost(fill_rate: float) -> float:
    """Blended cost assuming fill_rate of turnover at maker, rest at taker."""
    return fill_rate * (FEE_MAKER + SLIP_MAKER) + (1 - fill_rate) * (FEE_TAKER + SLIP_TAKER)


def main() -> int:
    closes = load_universe()
    print(f"Config: lb={LOOKBACK_H}h, rb={REBALANCE_H}h, "
          f"sign={'REV' if SIGN<0 else 'MOM'}, k_exit={K_EXIT}\n")

    # Sweep by fill rate
    fill_rates = [0.0, 0.3, 0.5, 0.7, 0.9, 1.0]
    rows = []
    print("Fill-rate sweep (F = fraction of turnover filled at maker):")
    print("  F=0.0 is pure taker; F=1.0 is pure maker (rebate-only).\n")
    for F in fill_rates:
        c = effective_cost(F)
        r = run_with_cost(closes, c)
        r["fill_rate"] = F
        rows.append(r)
        fs = r['fee_total'] / abs(r['gross_pnl']) if abs(r['gross_pnl']) > 1e-6 else float('nan')
        print(f"  F={F:.1f}  cost/side={c*10000:+.2f}bps  "
              f"Sharpe={r['sharpe']:+.2f}  net=${r['net_pnl']:+8.2f} ({r['return_pct']:+.1f}%)  "
              f"gross=${r['gross_pnl']:+8.2f}  fee=${r['fee_total']:7.2f}  "
              f"feeshare={fs:.0%}  dd={r['max_dd_pct']:+.1f}%")

    # Also: pure-cost sensitivity sweep
    print("\nPure cost sensitivity:")
    raw_costs = [0.0, 0.0001, 0.0002, 0.0003, 0.0005, 0.0007, 0.0011]
    raw_rows = []
    for c in raw_costs:
        r = run_with_cost(closes, c)
        raw_rows.append(r)
        print(f"  cost/side={c*10000:+.2f}bps  "
              f"Sharpe={r['sharpe']:+.2f}  net=${r['net_pnl']:+8.2f} "
              f"({r['return_pct']:+.1f}%)")

    _write(rows, raw_rows)
    return 0


def _write(fill_rows, cost_rows) -> None:
    path = PROJECT_ROOT / "backtest" / "results" / "PLAN-E-eta-maker.md"
    lines = []
    lines.append("# Plan E — η probe: maker execution sensitivity")
    lines.append("")
    lines.append(f"**Generated:** {pd.Timestamp.now(tz='UTC').isoformat()}")
    lines.append(f"**Fixed config:** lb={LOOKBACK_H}h, rb={REBALANCE_H}h, REV, k_exit={K_EXIT}")
    lines.append(f"**Deploy:** ${INITIAL_BALANCE:,.0f}")
    lines.append("")
    lines.append("## Fill-rate sweep")
    lines.append("")
    lines.append(f"Assumes fraction F of each rebalance's turnover fills at maker "
                 f"(fee {FEE_MAKER*100:+.3f}%, slip {SLIP_MAKER*100:.3f}%); "
                 f"remainder at taker (fee {FEE_TAKER*100:+.3f}%, slip {SLIP_TAKER*100:.3f}%).")
    lines.append("")
    lines.append("| Fill rate F | Eff cost/side (bps) | Net P&L | Return % | Gross | Fees | Sharpe | Max DD |")
    lines.append("|-------------|---------------------|---------|----------|-------|------|--------|--------|")
    for r in fill_rows:
        c = effective_cost(r["fill_rate"])
        lines.append(
            f"| {r['fill_rate']:.1f} | {c*10000:+.2f} | "
            f"${r['net_pnl']:+,.2f} | {r['return_pct']:+.1f}% | "
            f"${r['gross_pnl']:+,.2f} | ${r['fee_total']:,.2f} | "
            f"{r['sharpe']:+.2f} | {r['max_dd_pct']:+.1f}% |"
        )
    lines.append("")
    lines.append("## Raw cost sensitivity")
    lines.append("")
    lines.append("| Cost/side (bps) | Net P&L | Return % | Sharpe | Max DD |")
    lines.append("|-----------------|---------|----------|--------|--------|")
    for r in cost_rows:
        lines.append(
            f"| {r['cost_per_side']*10000:+.2f} | "
            f"${r['net_pnl']:+,.2f} | {r['return_pct']:+.1f}% | "
            f"{r['sharpe']:+.2f} | {r['max_dd_pct']:+.1f}% |"
        )
    lines.append("")
    # Verdict — find smallest fill rate where Sharpe > 1.0
    passing = [r for r in fill_rows if r["sharpe"] > 1.0
               and r["net_pnl"] > 0 and r["max_dd_pct"] > -15]
    lines.append("## Gate-crossing threshold")
    lines.append("")
    if passing:
        min_pass = min(passing, key=lambda r: r["fill_rate"])
        lines.append(f"**Minimum fill rate for gate pass (Sharpe>1, net>0, DD>-15%):** "
                     f"**F = {min_pass['fill_rate']:.1f}** "
                     f"(Sharpe {min_pass['sharpe']:+.2f}, net ${min_pass['net_pnl']:+,.2f})")
        lines.append("")
        lines.append(f"If realistic maker fill rate on a 24h-cadence rebalance is "
                     f">= {min_pass['fill_rate']:.0%}, ε passes the gate with maker engineering.")
    else:
        lines.append("**No fill rate achieves gate pass.** Even perfect maker execution "
                     "(F=1.0) leaves Sharpe below 1.0.")
    lines.append("")
    path.write_text("\n".join(lines))
    print(f"\nReport written to {path}")


if __name__ == "__main__":
    sys.exit(main())
