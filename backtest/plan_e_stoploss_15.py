#!/usr/bin/env python3
"""Plan E SL-15 variant: fixed symmetric 15% stop-loss.

Evaluates whether a +/-15% hard stop on each leg adds edge to the baseline
Plan E cross-sectional-reversal strategy. Does NOT modify the live runner.

Baseline config (matches plan_e_runner.py / plan_e_walkforward.py):
    - Universe: 10 USDT-perps (BTC, ETH, SOL, XRP, BNB, DOGE, ADA, AVAX, DOT, LINK)
    - Signal: 72h log-return, sign=-1 (reversal)
    - Entry: top 3 long, bottom 3 short
    - Leg notional: 10% (gross 60%)
    - Rebalance: every 24h at UTC 08:00
    - k_exit hysteresis: 6
    - Fees: 0.06% + 0.05% slip = 0.11% per side
    - Initial balance: $5000

SL-15 overlay:
    - LONG:  stop triggers if bar.low  < entry_price * 0.85
    - SHORT: stop triggers if bar.high > entry_price * 1.15
    - Fill at stop level, or bar.open if gap through stop
    - Post-stop: position zeroed; stay flat until next 24h rebalance
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# --- Config (aligned with plan_e_walkforward.py) ---
UNIVERSE = [
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "BNB-USDT",
    "DOGE-USDT", "ADA-USDT", "AVAX-USDT", "DOT-USDT", "LINK-USDT",
]
INITIAL_BALANCE = 5000.0
LONG_N = 3
SHORT_N = 3
LEG_NOTIONAL_PCT = 0.10
LOOKBACK_H = 72
REBALANCE_H = 24
REBALANCE_HOUR_UTC = 8
K_EXIT = 6
SIGN = -1

FEE_RATE = 0.0006
SLIPPAGE_RATE = 0.0005
COST_PER_SIDE = FEE_RATE + SLIPPAGE_RATE  # 0.0011

STOP_LONG_MULT = 0.85   # long stops if low < entry * 0.85
STOP_SHORT_MULT = 1.15  # short stops if high > entry * 1.15

SPLIT_DATE = pd.Timestamp("2026-01-01", tz="UTC")


def load_ohlc() -> dict:
    """Return dict with wide DataFrames: open, high, low, close (all aligned)."""
    data_dir = PROJECT_ROOT / "backtest" / "data"
    frames = {"open": {}, "high": {}, "low": {}, "close": {}}
    for sym in UNIVERSE:
        path = data_dir / f"{sym}_1H.csv"
        df = pd.read_csv(path)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.sort_values("timestamp").reset_index(drop=True).set_index("timestamp")
        for f in frames:
            frames[f][sym] = df[f]
    wide = {f: pd.DataFrame(frames[f]) for f in frames}
    # Align on common index
    common = wide["close"].dropna(how="any").index
    for f in wide:
        wide[f] = wide[f].loc[common]
    # Drop rows still containing NaN across any field
    joint = pd.concat([wide[f] for f in ("open", "high", "low", "close")], axis=1).dropna(how="any")
    common = joint.index
    for f in wide:
        wide[f] = wide[f].loc[common]
    print(f"Loaded {len(UNIVERSE)} assets. Aligned OHLC bars: {len(common)}")
    return wide


def _rebuild_targets(signal_row: np.ndarray, prev_w: np.ndarray) -> np.ndarray:
    """Apply k_exit=K_EXIT hysteresis: keep current holdings if still within top/bottom K_EXIT."""
    ranks = np.argsort(-signal_row)
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
        ai = int(a)
        if ai in new_long:
            continue
        new_long.add(ai)
        need_l -= 1

    retained_s = cur_short & keep_short
    new_short = set(retained_s)
    need_s = SHORT_N - len(retained_s)
    for a in ranks[::-1]:
        if need_s <= 0:
            break
        ai = int(a)
        if ai in new_short:
            continue
        new_short.add(ai)
        need_s -= 1

    new_w = np.zeros_like(prev_w)
    for a in new_long:
        new_w[a] = LEG_NOTIONAL_PCT
    for a in new_short:
        new_w[a] = -LEG_NOTIONAL_PCT
    return new_w


def run_backtest(ohlc: dict, apply_stoploss: bool) -> dict:
    """Bar-by-bar simulator with optional SL-15 overlay.

    Intrabar handling:
      - Compute bar P&L from prev close -> current close for positions held over the whole bar.
      - For stopped positions: compute open-to-stop P&L then mark leg to zero for the rest of the bar.
      - If bar.open already breaches stop (gap), fill at bar.open.

    Accounting approach: to keep equity math clean we (a) apply full close-to-close
    P&L for all active legs, then (b) ADJUST by the difference (stop_px - close_px)
    for any stopped leg, which exactly replaces the held-to-close return with the
    realized stop return on that bar.
    """
    closes = ohlc["close"]
    opens = ohlc["open"]
    highs = ohlc["high"]
    lows = ohlc["low"]
    ts = closes.index
    n_bars, n_assets = closes.shape
    cols = closes.columns.tolist()

    close_v = closes.values
    open_v = opens.values
    high_v = highs.values
    low_v = lows.values
    prev_close = np.roll(close_v, 1, axis=0)
    prev_close[0] = np.nan
    simple_ret = (close_v - prev_close) / prev_close

    signal = np.log(closes / closes.shift(LOOKBACK_H)).values * SIGN

    equity = np.full(n_bars, INITIAL_BALANCE, dtype=float)
    prev_w = np.zeros(n_assets)
    entry_px = np.zeros(n_assets)  # 0 => no position
    eq = INITIAL_BALANCE
    fee_total = 0.0
    gross_pnl_total = 0.0
    n_rebalances = 0
    turnover_sum = 0.0

    # Per-asset stop telemetry
    trig_count = np.zeros(n_assets, dtype=int)
    loss_avoided = np.zeros(n_assets, dtype=float)   # counterfactual: close-ret minus stop-ret (pct, per trigger)
    realized_stop_ret = np.zeros(n_assets, dtype=float)
    # positions-taken-count per asset (needed for trigger_rate)
    positions_taken = np.zeros(n_assets, dtype=int)

    # Tracking "entry at last rebalance" for loss-avoided calc: we want to compare
    # what would have happened had we held the leg to the NEXT rebalance. Since
    # that's path-dependent (cross-sectional), we use a simpler proxy: at trigger
    # time, counterfactual = close-at-next-rebalance return vs stop return.
    # To compute that, we stash the trigger events and resolve at the subsequent
    # rebalance bar.
    pending_stops = []  # list of dicts: {asset, trigger_idx, entry_px, stop_px, side}

    start_idx = LOOKBACK_H

    for i in range(start_idx, n_bars):
        r = simple_ret[i]
        o_i = open_v[i]
        h_i = high_v[i]
        l_i = low_v[i]
        c_i = close_v[i]

        # 1. Full bar close-to-close P&L for any active leg
        if np.isnan(r).any():
            bar_pnl = 0.0
        else:
            bar_pnl = float(np.sum(prev_w * eq * r))

        # 2. Stop-loss intrabar check & adjustment
        stop_adjust = 0.0
        if apply_stoploss:
            for a in range(n_assets):
                w = prev_w[a]
                if w == 0.0 or entry_px[a] == 0.0:
                    continue
                ep = entry_px[a]
                if w > 0:
                    trigger_level = ep * STOP_LONG_MULT
                    if l_i[a] < trigger_level:
                        # Fill: gap-through -> bar.open; else stop level
                        fill_px = o_i[a] if o_i[a] < trigger_level else trigger_level
                        held_ret_c2c = (c_i[a] / prev_close[i, a]) - 1.0
                        # realized ret for the bar = open -> fill? actually prev_close->fill
                        stop_ret_bar = (fill_px / prev_close[i, a]) - 1.0
                        # replace full-bar held return with stop return on this leg
                        delta_ret = stop_ret_bar - held_ret_c2c
                        stop_adjust += w * eq * delta_ret
                        # telemetry: per-trigger realized pct return from entry to stop
                        trig_count[a] += 1
                        realized_pct = (fill_px / ep) - 1.0
                        realized_stop_ret[a] += realized_pct
                        pending_stops.append({
                            "asset": a, "trigger_idx": i, "entry_px": ep,
                            "stop_px": fill_px, "side": "long",
                        })
                        # zero leg going forward
                        prev_w[a] = 0.0
                        entry_px[a] = 0.0
                elif w < 0:
                    trigger_level = ep * STOP_SHORT_MULT
                    if h_i[a] > trigger_level:
                        fill_px = o_i[a] if o_i[a] > trigger_level else trigger_level
                        held_ret_c2c = (c_i[a] / prev_close[i, a]) - 1.0
                        stop_ret_bar = (fill_px / prev_close[i, a]) - 1.0
                        delta_ret = stop_ret_bar - held_ret_c2c
                        stop_adjust += w * eq * delta_ret
                        trig_count[a] += 1
                        realized_pct = (fill_px / ep) - 1.0  # negative of leg pnl (short); we'll sign-correct in loss_avoided
                        realized_stop_ret[a] += -realized_pct  # short's realized leg return
                        pending_stops.append({
                            "asset": a, "trigger_idx": i, "entry_px": ep,
                            "stop_px": fill_px, "side": "short",
                        })
                        prev_w[a] = 0.0
                        entry_px[a] = 0.0

        total_bar_pnl = bar_pnl + stop_adjust
        gross_pnl_total += total_bar_pnl
        eq_before = eq + total_bar_pnl

        # 3. Rebalance check: every 24h at UTC 08:00
        do_rebalance = (ts[i].hour == REBALANCE_HOUR_UTC) and not np.isnan(signal[i]).any()
        if do_rebalance:
            new_w = _rebuild_targets(signal[i], prev_w)
            turnover = float(np.sum(np.abs(new_w - prev_w)))
            if turnover > 1e-9:
                fee = eq_before * turnover * COST_PER_SIDE
                fee_total += fee
                eq = eq_before - fee
                # Update entry prices for legs that are newly opened or flipped
                for a in range(n_assets):
                    if new_w[a] != 0.0 and prev_w[a] != new_w[a]:
                        entry_px[a] = c_i[a]  # fill at close of rebalance bar
                        positions_taken[a] += 1
                    elif new_w[a] == 0.0:
                        entry_px[a] = 0.0
                    # else: same direction retained -> keep entry_px
                prev_w = new_w
                turnover_sum += turnover
                n_rebalances += 1
            else:
                eq = eq_before
        else:
            eq = eq_before

        equity[i] = eq

    # Resolve loss-avoided proxy: for each pending stop, find the next rebalance
    # bar strictly after trigger_idx and compute the counterfactual leg return.
    rebalance_bar_mask = np.array([
        ts[i].hour == REBALANCE_HOUR_UTC for i in range(n_bars)
    ])
    for ev in pending_stops:
        a = ev["asset"]
        ti = ev["trigger_idx"]
        ep = ev["entry_px"]
        # find next rebalance bar index > ti
        j = ti + 1
        next_rb = -1
        while j < n_bars:
            if rebalance_bar_mask[j]:
                next_rb = j
                break
            j += 1
        if next_rb < 0:
            continue
        hypo_close = close_v[next_rb, a]
        if ev["side"] == "long":
            hypo_ret = (hypo_close / ep) - 1.0
            stop_ret = (ev["stop_px"] / ep) - 1.0
        else:
            hypo_ret = -((hypo_close / ep) - 1.0)
            stop_ret = -((ev["stop_px"] / ep) - 1.0)
        # "loss avoided" = stop_ret - hypo_ret (positive means stop saved us)
        loss_avoided[a] += (stop_ret - hypo_ret)

    return {
        "ts": ts,
        "equity": equity,
        "fee_total": fee_total,
        "gross_pnl_total": gross_pnl_total,
        "n_rebalances": n_rebalances,
        "turnover_sum": turnover_sum,
        "trig_count": trig_count,
        "loss_avoided": loss_avoided,
        "realized_stop_ret": realized_stop_ret,
        "positions_taken": positions_taken,
        "start_idx": start_idx,
        "cols": cols,
        "n_stop_events": len(pending_stops),
    }


def compute_metrics(result: dict, start_ts=None, end_ts=None) -> dict:
    ts = result["ts"]
    eq = result["equity"]
    if start_ts is None:
        start = result["start_idx"]
        eq_slice = eq[start:]
        ts_slice = ts[start:]
    else:
        mask = (ts >= start_ts) & (ts < end_ts)
        idx = np.where(mask)[0]
        if len(idx) < 2:
            return {"n_bars": 0}
        eq_slice = eq[idx]
        ts_slice = ts[idx]

    rr = np.diff(eq_slice) / eq_slice[:-1]
    rr = rr[np.isfinite(rr)]
    ann = 24 * 365
    mean_r = rr.mean() if len(rr) else 0.0
    std_r = rr.std(ddof=1) if len(rr) > 1 else 0.0
    sharpe = (mean_r * ann) / (std_r * np.sqrt(ann)) if std_r > 0 else 0.0

    peak = np.maximum.accumulate(eq_slice)
    dd = (eq_slice - peak) / peak
    max_dd = dd.min() if len(dd) else 0.0

    eq_start = float(eq_slice[0])
    eq_end = float(eq_slice[-1])
    ret_pct = (eq_end / eq_start - 1.0) * 100
    # CAGR: annualize based on elapsed hours
    hours = max((ts_slice[-1] - ts_slice[0]).total_seconds() / 3600.0, 1.0)
    years = hours / (24 * 365)
    if years > 0 and eq_start > 0 and eq_end > 0:
        cagr = ((eq_end / eq_start) ** (1 / years) - 1.0) * 100
    else:
        cagr = float("nan")

    return {
        "n_bars": len(eq_slice),
        "eq_start": eq_start,
        "eq_end": eq_end,
        "ret_pct": ret_pct,
        "sharpe": float(sharpe),
        "max_dd_pct": float(max_dd * 100),
        "cagr_pct": float(cagr),
    }


def _fmt_num(x, suffix=""):
    if isinstance(x, float) and (np.isnan(x) or np.isinf(x)):
        return "n/a"
    return f"{x}{suffix}"


def write_report(path: Path, baseline: dict, sl15: dict, ohlc: dict,
                 m_base_full: dict, m_sl_full: dict,
                 m_base_train: dict, m_sl_train: dict,
                 m_base_test: dict, m_sl_test: dict) -> None:
    lines = []
    lines.append("# Plan E SL-15 — fixed 15% stop-loss overlay")
    lines.append("")
    lines.append(f"**Generated:** {pd.Timestamp.now(tz='UTC').isoformat()}")
    lines.append(f"**Universe:** {', '.join(UNIVERSE)}")
    closes = ohlc["close"]
    lines.append(f"**Range:** {closes.index[0]} -> {closes.index[-1]} ({len(closes)} bars)")
    lines.append(f"**Config:** lb={LOOKBACK_H}h, rb={REBALANCE_H}h @ UTC{REBALANCE_HOUR_UTC:02d}, "
                 f"k_exit={K_EXIT}, sign=REV, fee+slip={COST_PER_SIDE*10000:.0f}bps/side")
    lines.append(f"**SL-15 rule:** long stop at entry*{STOP_LONG_MULT}, "
                 f"short stop at entry*{STOP_SHORT_MULT}; gap-through filled at bar.open; "
                 f"flat until next rebalance.")
    lines.append("")

    lines.append("## Full-period comparison")
    lines.append("")
    lines.append("| Metric | Baseline | SL-15 | Delta |")
    lines.append("|--------|----------|-------|-------|")
    delta = lambda a, b: (b - a)
    lines.append(f"| Sharpe | {m_base_full['sharpe']:+.2f} | {m_sl_full['sharpe']:+.2f} | "
                 f"{delta(m_base_full['sharpe'], m_sl_full['sharpe']):+.2f} |")
    lines.append(f"| Return % | {m_base_full['ret_pct']:+.1f}% | {m_sl_full['ret_pct']:+.1f}% | "
                 f"{delta(m_base_full['ret_pct'], m_sl_full['ret_pct']):+.1f}pp |")
    lines.append(f"| CAGR % | {m_base_full['cagr_pct']:+.1f}% | {m_sl_full['cagr_pct']:+.1f}% | "
                 f"{delta(m_base_full['cagr_pct'], m_sl_full['cagr_pct']):+.1f}pp |")
    lines.append(f"| Max DD % | {m_base_full['max_dd_pct']:+.1f}% | {m_sl_full['max_dd_pct']:+.1f}% | "
                 f"{delta(m_base_full['max_dd_pct'], m_sl_full['max_dd_pct']):+.1f}pp |")
    lines.append(f"| Final equity | ${m_base_full['eq_end']:,.0f} | ${m_sl_full['eq_end']:,.0f} | "
                 f"${m_sl_full['eq_end']-m_base_full['eq_end']:+,.0f} |")
    lines.append(f"| Rebalances | {baseline['n_rebalances']} | {sl15['n_rebalances']} | "
                 f"{sl15['n_rebalances']-baseline['n_rebalances']:+d} |")
    lines.append(f"| Sum turnover | {baseline['turnover_sum']:.1f} | {sl15['turnover_sum']:.1f} | "
                 f"{sl15['turnover_sum']-baseline['turnover_sum']:+.1f} |")
    lines.append(f"| Total fees | ${baseline['fee_total']:,.2f} | ${sl15['fee_total']:,.2f} | "
                 f"${sl15['fee_total']-baseline['fee_total']:+,.2f} |")
    lines.append(f"| Stop events | - | {sl15['n_stop_events']} | - |")
    lines.append("")

    lines.append("## Walk-forward (train < 2026-01-01 | test >= 2026-01-01)")
    lines.append("")
    lines.append("| Slice | Metric | Baseline | SL-15 | Delta |")
    lines.append("|-------|--------|----------|-------|-------|")
    for label, mb, ms in (("TRAIN", m_base_train, m_sl_train), ("TEST", m_base_test, m_sl_test)):
        if mb.get("n_bars", 0) == 0:
            continue
        lines.append(f"| {label} | Sharpe | {mb['sharpe']:+.2f} | {ms['sharpe']:+.2f} | "
                     f"{ms['sharpe']-mb['sharpe']:+.2f} |")
        lines.append(f"| {label} | Return % | {mb['ret_pct']:+.1f}% | {ms['ret_pct']:+.1f}% | "
                     f"{ms['ret_pct']-mb['ret_pct']:+.1f}pp |")
        lines.append(f"| {label} | Max DD % | {mb['max_dd_pct']:+.1f}% | {ms['max_dd_pct']:+.1f}% | "
                     f"{ms['max_dd_pct']-mb['max_dd_pct']:+.1f}pp |")
    lines.append("")

    lines.append("## Per-asset trigger stats (SL-15)")
    lines.append("")
    lines.append("| Asset | Positions taken | Triggers | Trigger rate | Avg loss avoided (pct pts) |")
    lines.append("|-------|-----------------|----------|--------------|-----------------------------|")
    n_events_total = int(sl15["trig_count"].sum())
    for idx, sym in enumerate(sl15["cols"]):
        tc = int(sl15["trig_count"][idx])
        pt = int(sl15["positions_taken"][idx])
        tr = (tc / pt * 100) if pt > 0 else 0.0
        avoided = (sl15["loss_avoided"][idx] / tc * 100) if tc > 0 else 0.0
        lines.append(f"| {sym} | {pt} | {tc} | {tr:.1f}% | {avoided:+.2f}pp |")
    lines.append(f"\nTotal stop events: {n_events_total}")
    lines.append("")

    # Insights
    lines.append("## Insights")
    lines.append("")
    total_trig = sl15["n_stop_events"]
    total_pos = int(sl15["positions_taken"].sum())
    global_trig_rate = (total_trig / total_pos * 100) if total_pos > 0 else 0.0
    sharpe_delta = m_sl_full["sharpe"] - m_base_full["sharpe"]
    dd_delta = m_sl_full["max_dd_pct"] - m_base_full["max_dd_pct"]
    ret_delta = m_sl_full["ret_pct"] - m_base_full["ret_pct"]

    lines.append(f"- Total stop events across 12mo: {total_trig} "
                 f"({global_trig_rate:.1f}% of positions taken).")
    lines.append(f"- Sharpe delta (full): {sharpe_delta:+.2f}. "
                 f"Return delta: {ret_delta:+.1f}pp. "
                 f"Max DD delta: {dd_delta:+.1f}pp (positive = shallower DD).")
    lines.append(f"- OOS Sharpe delta: {m_sl_test['sharpe']-m_base_test['sharpe']:+.2f}.")
    # Sum loss-avoided across assets (sum of per-event pct points)
    total_avoided_pp = float(sl15["loss_avoided"].sum()) * 100
    lines.append(f"- Sum of per-event 'loss avoided' (stop pct return minus hold-to-next-rb pct return): "
                 f"{total_avoided_pp:+.1f}pp across all triggers. "
                 f"Positive => stops were, on net, protective at leg level; "
                 f"negative => stops booked losses that would have reverted.")
    lines.append("")

    # Verdict
    lines.append("## Verdict")
    lines.append("")
    # Composite: require non-negative full Sharpe delta AND non-worse OOS
    improved_full = sharpe_delta >= 0.05
    improved_oos = (m_sl_test["sharpe"] - m_base_test["sharpe"]) >= 0.0
    dd_helped = dd_delta >= 1.0  # at least 1pp DD improvement
    if improved_full and improved_oos and dd_helped:
        verdict = "ADD — SL-15 improves risk-adjusted return and tail DD without OOS degradation."
    elif dd_helped and not improved_full:
        verdict = ("MIXED — SL-15 trims tail DD but costs Sharpe/return "
                   "(cuts reverters). Consider wider stop or volatility-scaled stop.")
    elif not improved_full and not dd_helped:
        verdict = ("REJECT — SL-15 does not improve Sharpe, return, or DD. "
                   "Fixed symmetric 15% is not additive for the reversal signal.")
    else:
        verdict = ("INCONCLUSIVE — mixed signals across full-period and OOS; "
                   "rerun with wider/volatility-scaled stop before deciding.")
    lines.append(f"**{verdict}**")
    lines.append("")
    lines.append(f"- Full Sharpe improved by >=0.05: {'YES' if improved_full else 'NO'} "
                 f"({sharpe_delta:+.2f})")
    lines.append(f"- OOS Sharpe not worse: {'YES' if improved_oos else 'NO'} "
                 f"({m_sl_test['sharpe']-m_base_test['sharpe']:+.2f})")
    lines.append(f"- Max DD improved by >=1pp: {'YES' if dd_helped else 'NO'} "
                 f"({dd_delta:+.1f}pp)")
    lines.append("")
    path.write_text("\n".join(lines))
    print(f"\nReport written to {path}")


def main() -> int:
    ohlc = load_ohlc()
    closes = ohlc["close"]
    full_start = closes.index[LOOKBACK_H]
    end_ts = closes.index[-1] + pd.Timedelta(hours=1)

    print("\nRunning baseline (no stop)...")
    baseline = run_backtest(ohlc, apply_stoploss=False)
    print("Running SL-15...")
    sl15 = run_backtest(ohlc, apply_stoploss=True)

    m_base_full = compute_metrics(baseline)
    m_sl_full = compute_metrics(sl15)
    m_base_train = compute_metrics(baseline, full_start, SPLIT_DATE)
    m_sl_train = compute_metrics(sl15, full_start, SPLIT_DATE)
    m_base_test = compute_metrics(baseline, SPLIT_DATE, end_ts)
    m_sl_test = compute_metrics(sl15, SPLIT_DATE, end_ts)

    out = PROJECT_ROOT / "backtest" / "results" / "stoploss-SL15.md"
    write_report(out, baseline, sl15, ohlc,
                 m_base_full, m_sl_full,
                 m_base_train, m_sl_train,
                 m_base_test, m_sl_test)

    # 10-line stdout summary
    print("\n== Plan E SL-15 summary ==")
    print(f"Full    | Baseline: Sharpe {m_base_full['sharpe']:+.2f} ret {m_base_full['ret_pct']:+.1f}% "
          f"DD {m_base_full['max_dd_pct']:+.1f}% | "
          f"SL-15: Sharpe {m_sl_full['sharpe']:+.2f} ret {m_sl_full['ret_pct']:+.1f}% "
          f"DD {m_sl_full['max_dd_pct']:+.1f}%")
    print(f"Train   | Baseline Sharpe {m_base_train['sharpe']:+.2f} | "
          f"SL-15 Sharpe {m_sl_train['sharpe']:+.2f} "
          f"(Δ {m_sl_train['sharpe']-m_base_train['sharpe']:+.2f})")
    print(f"Test    | Baseline Sharpe {m_base_test['sharpe']:+.2f} | "
          f"SL-15 Sharpe {m_sl_test['sharpe']:+.2f} "
          f"(Δ {m_sl_test['sharpe']-m_base_test['sharpe']:+.2f})")
    print(f"Stop events: {sl15['n_stop_events']} over "
          f"{int(sl15['positions_taken'].sum())} positions taken")
    print(f"Fees: baseline ${baseline['fee_total']:,.2f} vs SL-15 ${sl15['fee_total']:,.2f} "
          f"(Δ ${sl15['fee_total']-baseline['fee_total']:+,.2f})")
    print(f"Turnover sum: baseline {baseline['turnover_sum']:.1f} vs SL-15 "
          f"{sl15['turnover_sum']:.1f}")
    print(f"Final equity: baseline ${m_base_full['eq_end']:,.0f} vs "
          f"SL-15 ${m_sl_full['eq_end']:,.0f}")
    print(f"Sharpe Δ: {m_sl_full['sharpe']-m_base_full['sharpe']:+.2f} | "
          f"DD Δ: {m_sl_full['max_dd_pct']-m_base_full['max_dd_pct']:+.1f}pp | "
          f"Ret Δ: {m_sl_full['ret_pct']-m_base_full['ret_pct']:+.1f}pp")
    print(f"Top trigger assets (count): "
          + ", ".join(f"{sl15['cols'][i]}={int(sl15['trig_count'][i])}"
                      for i in np.argsort(-sl15["trig_count"])[:3]))
    print(f"Report: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
