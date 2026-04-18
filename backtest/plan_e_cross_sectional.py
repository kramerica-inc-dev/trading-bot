#!/usr/bin/env python3
"""Plan E (ε): cross-sectional momentum backtest.

Universe: 10 Blofin perps (BTC/ETH/SOL/XRP/BNB/DOGE/ADA/AVAX/DOT/LINK).
Signal: rank by trailing 24h return.
Entry: long top 3 / short bottom 3, equal-weight (10% notional per leg).
Rebalance: every 4h on UTC hours 0/4/8/12/16/20.
Fees: 0.06% taker, 0.05% slippage per side.
Deploy: $5,000.

In-sample: full year of 1h data across the 10 assets.
Gate: Sharpe>1.0, net P&L>0, max DD > -15%.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]

UNIVERSE = [
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "BNB-USDT",
    "DOGE-USDT", "ADA-USDT", "AVAX-USDT", "DOT-USDT", "LINK-USDT",
]
INITIAL_BALANCE = 5000.0
LONG_N = 3
SHORT_N = 3
LEG_NOTIONAL_PCT = 0.10          # 10% of equity per leg; 6 legs = 60% gross
REBALANCE_EVERY_H = 4
LOOKBACK_H = 24
FEE_RATE = 0.0006
SLIPPAGE_RATE = 0.0005
COST_PER_SIDE = FEE_RATE + SLIPPAGE_RATE   # 0.11%


def load_universe() -> pd.DataFrame:
    """Return wide DataFrame indexed by timestamp, columns = symbols, values = close."""
    data_dir = PROJECT_ROOT / "backtest" / "data"
    cols = {}
    for sym in UNIVERSE:
        path = data_dir / f"{sym}_1H.csv"
        df = pd.read_csv(path)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.sort_values("timestamp").reset_index(drop=True)
        cols[sym] = df.set_index("timestamp")["close"]
    wide = pd.DataFrame(cols)
    before = len(wide)
    wide = wide.dropna(how="any")
    after = len(wide)
    print(f"Loaded {len(UNIVERSE)} assets. Wide df: {before} rows -> {after} after dropna.")
    return wide


def run_backtest(closes: pd.DataFrame) -> dict:
    """Vectorized portfolio simulator.

    prev_weights[k] = fraction of equity allocated to asset k at bar i-1
                      (positive = long, negative = short).
    bar_pnl = sum_k(prev_weights[k] * eq * simple_ret[i,k])
    On rebalance: pay turnover * COST_PER_SIDE in fees, update weights.
    """
    simple_ret = closes.pct_change().values       # (n_bars, n_assets)
    signal = np.log(closes / closes.shift(LOOKBACK_H)).values
    ts = closes.index
    n_bars, n_assets = closes.shape
    cols = closes.columns.tolist()

    equity = np.full(n_bars, INITIAL_BALANCE)
    prev_weights = np.zeros(n_assets)
    eq = INITIAL_BALANCE
    fee_total = 0.0
    gross_pnl_total = 0.0
    trades = []

    start_idx = LOOKBACK_H  # need enough bars for first signal

    for i in range(start_idx, n_bars):
        # 1. Apply overnight P&L from prev positions
        r = simple_ret[i]
        if not np.isnan(r).any():
            bar_pnl = float(np.sum(prev_weights * eq * r))
        else:
            bar_pnl = 0.0
        gross_pnl_total += bar_pnl
        eq_before_fees = eq + bar_pnl

        # 2. Rebalance check (on UTC hours that are a multiple of REBALANCE_EVERY_H)
        hour = ts[i].hour
        do_rebalance = (hour % REBALANCE_EVERY_H == 0)

        if do_rebalance and not np.isnan(signal[i]).any():
            ranks = np.argsort(-signal[i])           # descending
            longs = ranks[:LONG_N]
            shorts = ranks[-SHORT_N:]
            new_w = np.zeros(n_assets)
            new_w[longs] = LEG_NOTIONAL_PCT
            new_w[shorts] = -LEG_NOTIONAL_PCT

            turnover = float(np.sum(np.abs(new_w - prev_weights)))
            fee = eq_before_fees * turnover * COST_PER_SIDE
            eq_after = eq_before_fees - fee
            fee_total += fee

            trades.append({
                "ts": ts[i],
                "turnover_frac": turnover,
                "fee": fee,
                "eq_before": eq_before_fees,
                "eq_after": eq_after,
                "longs": [cols[j] for j in longs],
                "shorts": [cols[j] for j in shorts],
            })
            prev_weights = new_w
            eq = eq_after
        else:
            eq = eq_before_fees

        equity[i] = eq

    return {
        "ts": ts,
        "equity": equity,
        "trades": trades,
        "fee_total": fee_total,
        "gross_pnl_total": gross_pnl_total,
        "net_pnl_total": equity[-1] - INITIAL_BALANCE,
        "start_idx": start_idx,
    }


def compute_metrics(result: dict) -> dict:
    eq = result["equity"]
    start = result["start_idx"]
    r = np.diff(eq[start:]) / eq[start:-1]
    r = r[np.isfinite(r)]

    ann_factor = 24 * 365        # hourly bars
    mean_r = r.mean() if len(r) else 0.0
    std_r = r.std(ddof=1) if len(r) > 1 else 0.0
    sharpe = (mean_r * ann_factor) / (std_r * np.sqrt(ann_factor)) if std_r > 0 else 0.0

    peak = np.maximum.accumulate(eq[start:])
    dd = (eq[start:] - peak) / peak
    max_dd = dd.min() if len(dd) else 0.0

    net = result["net_pnl_total"]
    gross = result["gross_pnl_total"]
    fee_share = result["fee_total"] / abs(gross) if abs(gross) > 1e-6 else float("nan")

    return {
        "n_trades": len(result["trades"]),
        "initial_eq": INITIAL_BALANCE,
        "final_eq": float(eq[-1]),
        "net_pnl": float(net),
        "gross_pnl": float(gross),
        "fee_total": float(result["fee_total"]),
        "fee_share": float(fee_share),
        "return_pct": float(net / INITIAL_BALANCE * 100),
        "sharpe": float(sharpe),
        "max_dd_pct": float(max_dd * 100),
    }


def _write_report(path: Path, result: dict, m: dict, closes: pd.DataFrame) -> None:
    lines = []
    lines.append("# Plan E (ε) — in-sample cross-sectional momentum backtest")
    lines.append("")
    lines.append(f"**Generated:** {pd.Timestamp.now(tz='UTC').isoformat()}")
    lines.append(f"**Universe:** {', '.join(closes.columns)}")
    lines.append(f"**Range:** {closes.index[0]} -> {closes.index[-1]}")
    lines.append(f"**Bars:** {len(closes)}")
    lines.append(f"**Deploy:** ${INITIAL_BALANCE:,.0f}")
    lines.append(f"**Signal:** trailing {LOOKBACK_H}h return (log), rank cross-sectionally")
    lines.append(f"**Entry:** long top {LONG_N} / short bottom {SHORT_N}, "
                 f"equal-weight, {LEG_NOTIONAL_PCT:.0%}/leg "
                 f"(gross exposure {(LONG_N + SHORT_N) * LEG_NOTIONAL_PCT:.0%})")
    lines.append(f"**Rebalance:** every {REBALANCE_EVERY_H}h on UTC hours "
                 f"{list(range(0, 24, REBALANCE_EVERY_H))}")
    lines.append(f"**Friction:** fee {FEE_RATE:.2%} + slippage {SLIPPAGE_RATE:.2%} per side "
                 f"(= {COST_PER_SIDE:.2%} per side, {2*COST_PER_SIDE:.2%} round-trip)")
    lines.append("")
    lines.append("## Results")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Initial equity | ${m['initial_eq']:,.2f} |")
    lines.append(f"| Final equity | ${m['final_eq']:,.2f} |")
    lines.append(f"| Net P&L | ${m['net_pnl']:+,.2f} |")
    lines.append(f"| Return % | {m['return_pct']:+.1f}% |")
    lines.append(f"| Gross P&L | ${m['gross_pnl']:+,.2f} |")
    lines.append(f"| Fees | ${m['fee_total']:,.2f} |")
    lines.append(f"| Fee share (|fees/gross|) | {m['fee_share']:.1%} |")
    lines.append(f"| Rebalances | {m['n_trades']} |")
    lines.append(f"| Sharpe (annualized) | {m['sharpe']:.2f} |")
    lines.append(f"| Max drawdown | {m['max_dd_pct']:.1f}% |")
    lines.append("")

    gate_sharpe = m["sharpe"] > 1.0
    gate_pnl = m["net_pnl"] > 0
    gate_dd = m["max_dd_pct"] > -15.0
    passes = gate_sharpe and gate_pnl and gate_dd
    lines.append("## Gate")
    lines.append("")
    lines.append(f"- Sharpe > 1.0: **{'PASS' if gate_sharpe else 'FAIL'}** ({m['sharpe']:.2f})")
    lines.append(f"- Net P&L > 0: **{'PASS' if gate_pnl else 'FAIL'}** (${m['net_pnl']:+.2f})")
    lines.append(f"- Max DD > -15%: **{'PASS' if gate_dd else 'FAIL'}** ({m['max_dd_pct']:.1f}%)")
    lines.append("")
    lines.append(f"**Overall:** {'PASS — proceed to walk-forward' if passes else 'FAIL — re-parametrize or escalate to γ'}")
    lines.append("")

    # Per-rebalance turnover stats
    if result["trades"]:
        turnovers = [t["turnover_frac"] for t in result["trades"]]
        fees = [t["fee"] for t in result["trades"]]
        lines.append("## Turnover / fee distribution")
        lines.append("")
        lines.append(f"- Turnover per rebalance: mean {np.mean(turnovers):.2f}, "
                     f"median {np.median(turnovers):.2f}, max {np.max(turnovers):.2f}")
        lines.append(f"- Fee per rebalance: mean ${np.mean(fees):.4f}, max ${np.max(fees):.4f}")
        lines.append(f"- Total rebalance cost: ${sum(fees):.2f} over {len(fees)} events")
        lines.append("")

    path.write_text("\n".join(lines))
    print(f"\nReport written to {path}")


def main() -> int:
    closes = load_universe()
    print(f"Universe: {len(closes.columns)} assets")
    print(f"Range: {closes.index[0]} -> {closes.index[-1]}")
    print(f"Bars: {len(closes)}")

    result = run_backtest(closes)
    m = compute_metrics(result)

    print("\n== Plan E in-sample results ==")
    print(f"Initial:    ${m['initial_eq']:,.2f}")
    print(f"Final:      ${m['final_eq']:,.2f}")
    print(f"Net P&L:    ${m['net_pnl']:+,.2f} ({m['return_pct']:+.1f}%)")
    print(f"Gross P&L:  ${m['gross_pnl']:+,.2f}")
    print(f"Fees:       ${m['fee_total']:,.2f}")
    print(f"Fee share:  {m['fee_share']:.1%}")
    print(f"Rebalances: {m['n_trades']}")
    print(f"Sharpe:     {m['sharpe']:.2f}")
    print(f"Max DD:     {m['max_dd_pct']:.1f}%")

    path = PROJECT_ROOT / "backtest" / "results" / "PLAN-E-insample.md"
    _write_report(path, result, m, closes)

    gate_pass = m["sharpe"] > 1.0 and m["net_pnl"] > 0 and m["max_dd_pct"] > -15.0
    return 0 if gate_pass else 1


if __name__ == "__main__":
    sys.exit(main())
