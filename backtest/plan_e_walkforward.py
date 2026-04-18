#!/usr/bin/env python3
"""Plan E — walk-forward validation.

The 72h/rb=24h/REV/k_exit=6 config was SELECTED on the full 12mo dataset.
To check for overfit, re-run the selection process on train-only data and
measure performance on a held-out test slice.

Split:
    Train: 2025-04-18 → 2025-12-31 (first ~8 months)
    Test:  2026-01-01 → 2026-04-18 (last ~3.5 months, held out)

Selection on train:
    - Sweep k_exit ∈ {4,5,6,7,8} at lb=72h, rb=24h, REV
    - Pick best by train Sharpe

OOS check:
    - Report train Sharpe / net / DD on chosen config
    - Report test Sharpe / net / DD on same config
    - PASS if test Sharpe > 0.5 (i.e., retains half the train edge)
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

LOOKBACK_H = 72
REBALANCE_H = 24
SIGN = -1
SPLIT_DATE = pd.Timestamp("2026-01-01", tz="UTC")
K_EXIT_GRID = [4, 5, 6, 7, 8]

# Test against both taker-only (conservative) and F=0.5 maker blend
FEE_TAKER = 0.0006
SLIP_TAKER = 0.0005
FEE_MAKER = -0.0001
SLIP_MAKER = 0.0000
COST_TAKER = FEE_TAKER + SLIP_TAKER            # 0.0011
COST_F05 = 0.5 * (FEE_MAKER + SLIP_MAKER) + 0.5 * COST_TAKER   # ~0.0005


def run(closes: pd.DataFrame, k_exit: int, cost_per_side: float) -> dict:
    """Returns equity curve + trade log so we can slice into IS/OOS."""
    simple_ret = closes.pct_change().values
    signal = np.log(closes / closes.shift(LOOKBACK_H)).values * SIGN
    ts = closes.index
    n_bars, n_assets = closes.shape

    equity = np.full(n_bars, INITIAL_BALANCE)
    prev_w = np.zeros(n_assets)
    eq = INITIAL_BALANCE
    fee_total = 0.0
    start_idx = LOOKBACK_H

    for i in range(start_idx, n_bars):
        r = simple_ret[i]
        bar_pnl = 0.0 if np.isnan(r).any() else float(np.sum(prev_w * eq * r))
        eq_before = eq + bar_pnl

        if ts[i].hour % REBALANCE_H == 0 and not np.isnan(signal[i]).any():
            ranks = np.argsort(-signal[i])
            keep_long = set(ranks[:k_exit].tolist())
            keep_short = set(ranks[-k_exit:].tolist())
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
            else:
                eq = eq_before
        else:
            eq = eq_before

        equity[i] = eq

    return {
        "ts": ts,
        "equity": equity,
        "fee_total": fee_total,
        "start_idx": start_idx,
    }


def slice_metrics(result: dict, start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> dict:
    ts = result["ts"]
    eq = result["equity"]
    mask = (ts >= start_ts) & (ts < end_ts)
    idx = np.where(mask)[0]
    if len(idx) < 2:
        return {"n_bars": 0}
    eq_slice = eq[idx]
    eq_start = eq_slice[0]
    eq_end = eq_slice[-1]
    rr = np.diff(eq_slice) / eq_slice[:-1]
    rr = rr[np.isfinite(rr)]
    ann = 24 * 365
    mean_r = rr.mean() if len(rr) else 0.0
    std_r = rr.std(ddof=1) if len(rr) > 1 else 0.0
    sharpe = (mean_r * ann) / (std_r * np.sqrt(ann)) if std_r > 0 else 0.0
    peak = np.maximum.accumulate(eq_slice)
    dd = (eq_slice - peak) / peak
    max_dd = dd.min() if len(dd) else 0.0
    # Return in this slice, normalized per initial slice balance
    ret_pct = (eq_end / eq_start - 1) * 100
    return {
        "n_bars": len(idx),
        "eq_start": float(eq_start),
        "eq_end": float(eq_end),
        "ret_pct": float(ret_pct),
        "sharpe": float(sharpe),
        "max_dd_pct": float(max_dd * 100),
    }


def main() -> int:
    closes = load_universe()
    print(f"Full range: {closes.index[0]} -> {closes.index[-1]}")
    print(f"Split at: {SPLIT_DATE}\n")

    full_start = closes.index[LOOKBACK_H]
    # Train = [full_start, SPLIT_DATE); Test = [SPLIT_DATE, closes.index[-1]]
    # Use the equity curve evaluated at each k_exit, slice metrics

    # Selection on train: which k_exit wins?
    print("Train-only Sharpe at taker cost (cost/side = 11bps):\n")
    train_results = []
    test_results = []
    for k in K_EXIT_GRID:
        res_taker = run(closes, k, COST_TAKER)
        m_train = slice_metrics(res_taker, full_start, SPLIT_DATE)
        m_test = slice_metrics(res_taker, SPLIT_DATE, closes.index[-1] + pd.Timedelta(hours=1))
        train_results.append((k, "taker", m_train))
        test_results.append((k, "taker", m_test))
        print(f"  k={k}: TRAIN Sharpe={m_train['sharpe']:+.2f} "
              f"ret={m_train['ret_pct']:+.1f}% dd={m_train['max_dd_pct']:+.1f}%   "
              f"TEST Sharpe={m_test['sharpe']:+.2f} "
              f"ret={m_test['ret_pct']:+.1f}% dd={m_test['max_dd_pct']:+.1f}%")

    # Pick best by train Sharpe
    best_k = max([(k, tm) for k, _, tm in train_results],
                 key=lambda t: t[1]["sharpe"])[0]
    print(f"\nSelected k_exit by train Sharpe: k={best_k}")

    # Also sweep at F=0.5 maker cost
    print(f"\nMaker-blend Sharpe at cost/side = {COST_F05*10000:.1f}bps (F=0.5):\n")
    for k in K_EXIT_GRID:
        res_f05 = run(closes, k, COST_F05)
        m_train_m = slice_metrics(res_f05, full_start, SPLIT_DATE)
        m_test_m = slice_metrics(res_f05, SPLIT_DATE, closes.index[-1] + pd.Timedelta(hours=1))
        train_results.append((k, "f05", m_train_m))
        test_results.append((k, "f05", m_test_m))
        print(f"  k={k}: TRAIN Sharpe={m_train_m['sharpe']:+.2f} "
              f"ret={m_train_m['ret_pct']:+.1f}% dd={m_train_m['max_dd_pct']:+.1f}%   "
              f"TEST Sharpe={m_test_m['sharpe']:+.2f} "
              f"ret={m_test_m['ret_pct']:+.1f}% dd={m_test_m['max_dd_pct']:+.1f}%")

    _write(train_results, test_results, best_k)

    # Decision
    test_at_best = [m for k, tag, m in test_results if k == best_k and tag == "taker"][0]
    print(f"\n== Walk-forward verdict (taker cost, k_exit={best_k}) ==")
    print(f"TEST Sharpe: {test_at_best['sharpe']:+.2f}")
    print(f"TEST return: {test_at_best['ret_pct']:+.1f}%")
    print(f"TEST DD:     {test_at_best['max_dd_pct']:+.1f}%")
    if test_at_best["sharpe"] > 0.5:
        print(f"PASS — signal retains edge out-of-sample.")
        return 0
    else:
        print(f"FAIL — OOS Sharpe below 0.5, likely overfit.")
        return 1


def _write(train_results, test_results, best_k):
    path = PROJECT_ROOT / "backtest" / "results" / "PLAN-E-walkforward.md"
    lines = []
    lines.append("# Plan E — walk-forward validation")
    lines.append("")
    lines.append(f"**Generated:** {pd.Timestamp.now(tz='UTC').isoformat()}")
    lines.append(f"**Signal config:** lb={LOOKBACK_H}h, rb={REBALANCE_H}h, REV")
    lines.append(f"**Split:** Train < {SPLIT_DATE.date()} | Test >= {SPLIT_DATE.date()}")
    lines.append(f"**Test period:** ~3.5 months (out-of-sample)")
    lines.append("")
    lines.append("## Taker execution (cost/side = 11bps)")
    lines.append("")
    lines.append("| k_exit | Train Sharpe | Train Return | Train DD | Test Sharpe | Test Return | Test DD |")
    lines.append("|--------|--------------|--------------|----------|-------------|-------------|---------|")
    for k, tag, m_tr in train_results:
        if tag != "taker":
            continue
        m_te = [mm for kk, tt, mm in test_results if kk == k and tt == "taker"][0]
        mark = " ←" if k == best_k else ""
        lines.append(
            f"| {k}{mark} | {m_tr['sharpe']:+.2f} | {m_tr['ret_pct']:+.1f}% | "
            f"{m_tr['max_dd_pct']:+.1f}% | "
            f"**{m_te['sharpe']:+.2f}** | {m_te['ret_pct']:+.1f}% | "
            f"{m_te['max_dd_pct']:+.1f}% |"
        )
    lines.append("")
    lines.append(f"Arrow marks the k selected by train Sharpe (k_exit={best_k}).")
    lines.append("")
    lines.append("## Maker-blend execution (F=0.5, cost/side ~5bps)")
    lines.append("")
    lines.append("| k_exit | Train Sharpe | Train Return | Train DD | Test Sharpe | Test Return | Test DD |")
    lines.append("|--------|--------------|--------------|----------|-------------|-------------|---------|")
    for k, tag, m_tr in train_results:
        if tag != "f05":
            continue
        m_te = [mm for kk, tt, mm in test_results if kk == k and tt == "f05"][0]
        mark = " ←" if k == best_k else ""
        lines.append(
            f"| {k}{mark} | {m_tr['sharpe']:+.2f} | {m_tr['ret_pct']:+.1f}% | "
            f"{m_tr['max_dd_pct']:+.1f}% | "
            f"**{m_te['sharpe']:+.2f}** | {m_te['ret_pct']:+.1f}% | "
            f"{m_te['max_dd_pct']:+.1f}% |"
        )
    lines.append("")

    # Verdict
    test_best_taker = [m for k, tag, m in test_results if k == best_k and tag == "taker"][0]
    test_best_f05 = [m for k, tag, m in test_results if k == best_k and tag == "f05"][0]
    lines.append("## Verdict")
    lines.append("")
    lines.append(f"Selected config: k_exit={best_k}")
    lines.append(f"- Taker OOS Sharpe: **{test_best_taker['sharpe']:+.2f}** "
                 f"(gate: >0.5 for PASS)")
    lines.append(f"- Maker(F=0.5) OOS Sharpe: **{test_best_f05['sharpe']:+.2f}**")
    lines.append(f"- Taker OOS Max DD: {test_best_taker['max_dd_pct']:+.1f}%")
    lines.append("")
    wf_pass = test_best_taker["sharpe"] > 0.5
    lines.append(f"**Walk-forward: {'PASS' if wf_pass else 'FAIL'}**")
    lines.append("")
    if wf_pass:
        lines.append("Signal retains meaningful edge out-of-sample. Next: "
                     "paper-trade per P1 policy (2-4 weeks) using taker "
                     "execution as conservative baseline, with an option "
                     "to switch to maker rebalancer for Sharpe improvement.")
    else:
        lines.append("Signal does not generalize. Escalate to γ (shutdown) "
                     "or redesign with different timeframe/universe/horizon.")
    lines.append("")
    path.write_text("\n".join(lines))
    print(f"\nReport written to {path}")


if __name__ == "__main__":
    sys.exit(main())
