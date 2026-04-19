#!/usr/bin/env python3
"""Plan E — TRAILING stop-loss variant (10% trail, activated after 5% favorable move).

Derived from `plan_e_cross_sectional.py` / `plan_e_walkforward.py` but with
per-position bar-level stop management using OHLC data.

Config (fixed):
    lookback   = 72h log-return, sign = -1 (reversal)
    rebalance  = 24h at UTC 08:00
    k_exit     = 6      (retain if still in top/bottom 6)
    leg_pct    = 10%    gross 60%
    fee        = 0.0006, slippage = 0.0005 per side (COST_PER_SIDE = 0.0011)
    start eq   = $5000

Trailing stop logic (per-position, per-bar):
    LONG:
        initial_stop = entry * 0.90    (but DORMANT)
        high_since_entry = max(entry, highs seen)
        ACTIVATION condition: high_since_entry >= entry * 1.05  (once true, stays true)
        After activation: stop = max(stop, high_since_entry * 0.90)
        Trigger: bar.low < stop (use bar.open if bar.open < stop, i.e. gap)
    SHORT (mirror):
        initial_stop = entry * 1.10    (DORMANT)
        low_since_entry = min(entry, lows seen)
        ACTIVATION: low_since_entry <= entry * 0.95
        After activation: stop = min(stop, low_since_entry * 1.10)
        Trigger: bar.high > stop (use bar.open if bar.open > stop, i.e. gap)

Post-stop: position flat until next 24h rebalance.

Outputs:
    - Full-period metrics + walk-forward (train < 2026-01-01, test >= 2026-01-01)
    - Per-asset trigger stats + avg lock-in gain vs "held to rebalance" counterfactual
    - Markdown report: backtest/results/stoploss-TRAIL.md
    - 10-line stdout summary

No modification to existing files; this is a pure research script.
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
LEG_PCT = 0.10
LOOKBACK_H = 72
REBALANCE_H = 24
REBALANCE_HOUR = 8      # UTC 08:00
SIGN = -1               # reversal
K_EXIT = 6
FEE = 0.0006
SLIP = 0.0005
COST = FEE + SLIP       # 0.0011 per side

# Trailing-stop parameters
TRAIL_PCT = 0.10        # 10% trail
ACTIVATE_GAIN = 0.05    # activate stop after 5% favorable move

SPLIT_DATE = pd.Timestamp("2026-01-01", tz="UTC")


def load_ohlc() -> dict[str, pd.DataFrame]:
    """Return dict[sym] -> DataFrame indexed by ts with open/high/low/close."""
    data_dir = PROJECT_ROOT / "backtest" / "data"
    out = {}
    for sym in UNIVERSE:
        df = pd.read_csv(data_dir / f"{sym}_1H.csv")
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.sort_values("timestamp").set_index("timestamp")
        out[sym] = df[["open", "high", "low", "close"]]
    # Align on common index
    common = None
    for sym, df in out.items():
        common = df.index if common is None else common.intersection(df.index)
    for sym in out:
        out[sym] = out[sym].loc[common].copy()
    print(f"Loaded {len(UNIVERSE)} assets, {len(common)} aligned bars: "
          f"{common[0]} -> {common[-1]}")
    return out


def build_panels(ohlc: dict[str, pd.DataFrame]) -> tuple[pd.DatetimeIndex, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Return (ts, open, high, low, close) aligned 2D arrays shape (n_bars, n_assets)."""
    syms = list(ohlc.keys())
    ts = ohlc[syms[0]].index
    O = np.stack([ohlc[s]["open"].values for s in syms], axis=1)
    H = np.stack([ohlc[s]["high"].values for s in syms], axis=1)
    L = np.stack([ohlc[s]["low"].values for s in syms], axis=1)
    C = np.stack([ohlc[s]["close"].values for s in syms], axis=1)
    return ts, O, H, L, C


def simulate(ts, O, H, L, C,
             use_stops: bool,
             cost_per_side: float = COST) -> dict:
    """Hour-by-hour simulator with per-position trailing stops.

    When `use_stops=False`, replicates the baseline (no stop management).

    Intrabar convention for stops:
        - If stop is ACTIVE at the start of bar i:
            LONG: if O[i] < stop -> fill at O[i] (gap), else if L[i] < stop -> fill at stop.
            SHORT: if O[i] > stop -> fill at O[i] (gap), else if H[i] > stop -> fill at stop.
        - After fill, asset position goes flat until the next rebalance.
        - P&L for the bar: prev_weight[a] * eq * (fill_price / prev_close[a] - 1),
          then weight becomes 0 for the rest of the bar (no further close-to-close P&L).

    Non-stopped assets: standard close-to-close P&L using prev_w * eq * simple_ret.
    """
    n_bars, n_assets = C.shape
    # close-to-close returns, C[i]/C[i-1] - 1
    simple_ret = np.vstack([np.full((1, n_assets), np.nan),
                            C[1:] / C[:-1] - 1.0])
    # signal: log(C / C.shift(lookback)) * SIGN
    sig = np.full_like(C, np.nan)
    sig[LOOKBACK_H:] = np.log(C[LOOKBACK_H:] / C[:-LOOKBACK_H]) * SIGN

    equity = np.full(n_bars, INITIAL_BALANCE)
    prev_w = np.zeros(n_assets)
    entry_price = np.full(n_assets, np.nan)     # where position entered
    hi_since = np.full(n_assets, -np.inf)       # running max high since entry
    lo_since = np.full(n_assets, np.inf)        # running min low since entry
    stop_px = np.full(n_assets, np.nan)         # current stop level
    stop_active = np.zeros(n_assets, dtype=bool)
    stop_dir = np.zeros(n_assets, dtype=int)    # +1 long, -1 short, 0 none
    # Per-asset trigger log
    triggers = [[] for _ in range(n_assets)]    # list of dicts per asset

    eq = INITIAL_BALANCE
    fee_total = 0.0
    start_idx = LOOKBACK_H
    n_rebalances = 0

    for i in range(start_idx, n_bars):
        # --- 1. Handle intra-bar stop triggers BEFORE close-to-close PnL ---
        # We decompose the bar's contribution:
        #   if stop fires, that asset's PnL = prev_w * eq * (fill / prev_close - 1),
        #     then weight set to 0 (no further PnL this bar).
        #   otherwise, asset PnL = prev_w * eq * simple_ret[i].
        bar_pnl = 0.0
        stopped_this_bar = np.zeros(n_assets, dtype=bool)
        if use_stops:
            for a in range(n_assets):
                if prev_w[a] == 0.0 or not stop_active[a]:
                    continue
                if stop_dir[a] == 1:  # LONG
                    # Gap: open below stop -> fill at open
                    if O[i, a] < stop_px[a]:
                        fill = O[i, a]
                    elif L[i, a] < stop_px[a]:
                        fill = stop_px[a]
                    else:
                        continue
                    prev_close = C[i - 1, a]
                    leg_pnl = prev_w[a] * eq * (fill / prev_close - 1.0)
                    bar_pnl += leg_pnl
                    # held-to-close counterfactual PnL (for lock-in stat)
                    held_leg_pnl = prev_w[a] * eq * (C[i, a] / prev_close - 1.0)
                    triggers[a].append({
                        "ts": ts[i],
                        "dir": "L",
                        "entry": float(entry_price[a]),
                        "fill": float(fill),
                        "stop_pnl": float(leg_pnl),
                        "held_close_pnl": float(held_leg_pnl),
                    })
                    stopped_this_bar[a] = True
                elif stop_dir[a] == -1:  # SHORT
                    if O[i, a] > stop_px[a]:
                        fill = O[i, a]
                    elif H[i, a] > stop_px[a]:
                        fill = stop_px[a]
                    else:
                        continue
                    prev_close = C[i - 1, a]
                    leg_pnl = prev_w[a] * eq * (fill / prev_close - 1.0)
                    bar_pnl += leg_pnl
                    held_leg_pnl = prev_w[a] * eq * (C[i, a] / prev_close - 1.0)
                    triggers[a].append({
                        "ts": ts[i],
                        "dir": "S",
                        "entry": float(entry_price[a]),
                        "fill": float(fill),
                        "stop_pnl": float(leg_pnl),
                        "held_close_pnl": float(held_leg_pnl),
                    })
                    stopped_this_bar[a] = True

        # --- 2. Close-to-close PnL for assets not stopped this bar ---
        r = simple_ret[i]
        if not np.isnan(r).any():
            active = (~stopped_this_bar) & (prev_w != 0.0)
            if active.any():
                bar_pnl += float(np.sum(prev_w[active] * eq * r[active]))

        # Apply stop fees and flatten stopped positions (small: 1 side fee on notional)
        if use_stops and stopped_this_bar.any():
            # Cost for stop-out: |weight| * (eq + bar_pnl) * cost
            # Conservative: charge against the pre-rebalance equity after PnL
            eq_interim = eq + bar_pnl
            stop_notional_frac = float(np.sum(np.abs(prev_w[stopped_this_bar])))
            stop_fee = eq_interim * stop_notional_frac * cost_per_side
            fee_total += stop_fee
            bar_pnl -= stop_fee
            # Flatten stopped assets
            prev_w = prev_w.copy()
            prev_w[stopped_this_bar] = 0.0
            entry_price[stopped_this_bar] = np.nan
            hi_since[stopped_this_bar] = -np.inf
            lo_since[stopped_this_bar] = np.inf
            stop_px[stopped_this_bar] = np.nan
            stop_active[stopped_this_bar] = False
            stop_dir[stopped_this_bar] = 0

        eq_before_rb = eq + bar_pnl

        # --- 3. Update trailing-stop state for still-open positions ---
        if use_stops:
            for a in range(n_assets):
                if prev_w[a] == 0.0:
                    continue
                if stop_dir[a] == 1:  # LONG
                    hi_since[a] = max(hi_since[a], H[i, a])
                    # Activation: high_since_entry >= entry * (1 + ACTIVATE_GAIN)
                    if (not stop_active[a]) and hi_since[a] >= entry_price[a] * (1.0 + ACTIVATE_GAIN):
                        stop_active[a] = True
                    if stop_active[a]:
                        # Trail up only: stop = max(stop, hi_since * (1 - TRAIL_PCT))
                        new_stop = hi_since[a] * (1.0 - TRAIL_PCT)
                        if np.isnan(stop_px[a]) or new_stop > stop_px[a]:
                            stop_px[a] = new_stop
                elif stop_dir[a] == -1:  # SHORT
                    lo_since[a] = min(lo_since[a], L[i, a])
                    if (not stop_active[a]) and lo_since[a] <= entry_price[a] * (1.0 - ACTIVATE_GAIN):
                        stop_active[a] = True
                    if stop_active[a]:
                        new_stop = lo_since[a] * (1.0 + TRAIL_PCT)
                        if np.isnan(stop_px[a]) or new_stop < stop_px[a]:
                            stop_px[a] = new_stop

        # --- 4. 24h rebalance at UTC 08:00 ---
        do_rb = (ts[i].hour == REBALANCE_HOUR) and not np.isnan(sig[i]).any()
        if do_rb:
            ranks = np.argsort(-sig[i])
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
                new_w[a] = LEG_PCT
            for a in new_short:
                new_w[a] = -LEG_PCT

            turnover = float(np.sum(np.abs(new_w - prev_w)))
            if turnover > 1e-9:
                fee = eq_before_rb * turnover * cost_per_side
                fee_total += fee
                eq = eq_before_rb - fee
                # For any asset whose position CHANGED, reset trail state using close as entry
                changed = new_w != prev_w
                for a in np.where(changed)[0]:
                    if new_w[a] > 0:
                        entry_price[a] = C[i, a]
                        hi_since[a] = H[i, a]
                        lo_since[a] = np.inf
                        stop_px[a] = C[i, a] * (1.0 - TRAIL_PCT)   # dormant initial
                        stop_active[a] = False
                        stop_dir[a] = 1
                    elif new_w[a] < 0:
                        entry_price[a] = C[i, a]
                        lo_since[a] = L[i, a]
                        hi_since[a] = -np.inf
                        stop_px[a] = C[i, a] * (1.0 + TRAIL_PCT)
                        stop_active[a] = False
                        stop_dir[a] = -1
                    else:
                        entry_price[a] = np.nan
                        hi_since[a] = -np.inf
                        lo_since[a] = np.inf
                        stop_px[a] = np.nan
                        stop_active[a] = False
                        stop_dir[a] = 0
                prev_w = new_w
                n_rebalances += 1
            else:
                eq = eq_before_rb
        else:
            eq = eq_before_rb

        equity[i] = eq

    return {
        "ts": ts,
        "equity": equity,
        "fee_total": fee_total,
        "n_rebalances": n_rebalances,
        "start_idx": start_idx,
        "triggers": triggers,
        "symbols": UNIVERSE,
    }


def slice_metrics(ts, equity, start_ts, end_ts, start_idx) -> dict:
    mask = (ts >= start_ts) & (ts < end_ts)
    idx = np.where(mask)[0]
    idx = idx[idx >= start_idx]
    if len(idx) < 2:
        return {"n_bars": 0, "sharpe": 0.0, "ret_pct": 0.0, "max_dd_pct": 0.0,
                "cagr_pct": 0.0, "eq_start": 0.0, "eq_end": 0.0}
    eq = equity[idx]
    rr = np.diff(eq) / eq[:-1]
    rr = rr[np.isfinite(rr)]
    ann = 24 * 365
    mean_r = rr.mean() if len(rr) else 0.0
    std_r = rr.std(ddof=1) if len(rr) > 1 else 0.0
    sharpe = (mean_r * ann) / (std_r * np.sqrt(ann)) if std_r > 0 else 0.0
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / peak
    max_dd = dd.min() if len(dd) else 0.0
    ret_pct = (eq[-1] / eq[0] - 1) * 100
    years = len(idx) / ann
    cagr = ((eq[-1] / eq[0]) ** (1 / years) - 1) * 100 if years > 0 and eq[0] > 0 else 0.0
    return {
        "n_bars": int(len(idx)),
        "eq_start": float(eq[0]),
        "eq_end": float(eq[-1]),
        "ret_pct": float(ret_pct),
        "cagr_pct": float(cagr),
        "sharpe": float(sharpe),
        "max_dd_pct": float(max_dd * 100),
    }


def per_asset_stats(result: dict) -> list[dict]:
    out = []
    # total bars-in-position per asset is hard to attribute exactly; we use
    # trigger counts and per-trigger avg lock-in
    ts = result["ts"]
    start = result["start_idx"]
    total_bars = len(ts) - start
    for a, sym in enumerate(result["symbols"]):
        trigs = result["triggers"][a]
        n = len(trigs)
        avg_lock_in = float(np.mean([t["stop_pnl"] - t["held_close_pnl"]
                                     for t in trigs])) if trigs else 0.0
        avg_stop_pnl = float(np.mean([t["stop_pnl"] for t in trigs])) if trigs else 0.0
        total_lock_in = float(np.sum([t["stop_pnl"] - t["held_close_pnl"]
                                      for t in trigs])) if trigs else 0.0
        out.append({
            "symbol": sym,
            "n_triggers": n,
            "trigger_rate_per_1kbars": n / total_bars * 1000 if total_bars else 0.0,
            "avg_stop_pnl": avg_stop_pnl,
            "avg_lock_in_gain": avg_lock_in,
            "total_lock_in_gain": total_lock_in,
        })
    return out


def write_report(path: Path,
                 full_base: dict, full_stop: dict,
                 train_base: dict, test_base: dict,
                 train_stop: dict, test_stop: dict,
                 base_result: dict, stop_result: dict,
                 asset_stats: list[dict]) -> None:
    def delta(a, b):
        return b - a

    lines = []
    lines.append("# Plan E — stoploss variant: TRAILING 10% (activate after +5%)")
    lines.append("")
    lines.append(f"**Generated:** {pd.Timestamp.now(tz='UTC').isoformat()}")
    lines.append(f"**Config:** lookback=72h, sign=-1 (reversal), k_exit={K_EXIT}, "
                 f"leg={LEG_PCT:.0%}, rebalance=24h @ UTC {REBALANCE_HOUR:02d}:00, "
                 f"start=${INITIAL_BALANCE:,.0f}")
    lines.append(f"**Costs:** fee={FEE:.4f} + slip={SLIP:.4f} = {COST:.4f} per side")
    lines.append(f"**Stop rule:** trail={TRAIL_PCT:.0%}, activation gain="
                 f"+{ACTIVATE_GAIN:.0%} (long) / -{ACTIVATE_GAIN:.0%} (short); "
                 f"fills at stop or at open on gap; flat until next rebalance.")
    lines.append("")
    lines.append("## Full-period: baseline (no stop) vs TRAIL-SL")
    lines.append("")
    lines.append("| Metric | Baseline | TRAIL-SL | Δ |")
    lines.append("|--------|---------:|---------:|---:|")
    for key, label, fmt in [
        ("eq_end",      "Final equity",  "${:,.2f}"),
        ("ret_pct",     "Return %",      "{:+.2f}%"),
        ("cagr_pct",    "CAGR %",        "{:+.2f}%"),
        ("sharpe",      "Sharpe",        "{:+.3f}"),
        ("max_dd_pct",  "Max DD %",      "{:+.2f}%"),
    ]:
        b = full_base[key]; s = full_stop[key]
        lines.append(f"| {label} | {fmt.format(b)} | {fmt.format(s)} | "
                     f"{fmt.format(delta(b, s))} |")
    lines.append(f"| Rebalances | {base_result['n_rebalances']} | "
                 f"{stop_result['n_rebalances']} | "
                 f"{stop_result['n_rebalances'] - base_result['n_rebalances']} |")
    lines.append(f"| Fees paid | ${base_result['fee_total']:,.2f} | "
                 f"${stop_result['fee_total']:,.2f} | "
                 f"${stop_result['fee_total'] - base_result['fee_total']:+,.2f} |")
    lines.append("")
    lines.append("## Walk-forward (train < 2026-01-01, test >= 2026-01-01)")
    lines.append("")
    lines.append("### Train (IS)")
    lines.append("")
    lines.append("| Metric | Baseline | TRAIL-SL | Δ |")
    lines.append("|--------|---------:|---------:|---:|")
    for key, label, fmt in [
        ("ret_pct", "Return %", "{:+.2f}%"),
        ("sharpe", "Sharpe", "{:+.3f}"),
        ("max_dd_pct", "Max DD %", "{:+.2f}%"),
    ]:
        b, s = train_base[key], train_stop[key]
        lines.append(f"| {label} | {fmt.format(b)} | {fmt.format(s)} | "
                     f"{fmt.format(delta(b, s))} |")
    lines.append("")
    lines.append("### Test (OOS)")
    lines.append("")
    lines.append("| Metric | Baseline | TRAIL-SL | Δ |")
    lines.append("|--------|---------:|---------:|---:|")
    for key, label, fmt in [
        ("ret_pct", "Return %", "{:+.2f}%"),
        ("sharpe", "Sharpe", "{:+.3f}"),
        ("max_dd_pct", "Max DD %", "{:+.2f}%"),
    ]:
        b, s = test_base[key], test_stop[key]
        lines.append(f"| {label} | {fmt.format(b)} | {fmt.format(s)} | "
                     f"{fmt.format(delta(b, s))} |")
    lines.append("")
    lines.append("## Per-asset trigger stats")
    lines.append("")
    lines.append("`avg_lock_in_gain` = mean PnL at stop − PnL had we held to next rebalance close "
                 "(positive = stop saved us). Units: USDT per trigger.")
    lines.append("")
    lines.append("| Symbol | Triggers | Rate /1k bars | Avg stop PnL ($) | Avg lock-in ($) | Total lock-in ($) |")
    lines.append("|--------|---------:|--------------:|-----------------:|----------------:|------------------:|")
    total_trig = 0
    total_lock = 0.0
    for s in asset_stats:
        total_trig += s["n_triggers"]
        total_lock += s["total_lock_in_gain"]
        lines.append(f"| {s['symbol']} | {s['n_triggers']} | "
                     f"{s['trigger_rate_per_1kbars']:.2f} | "
                     f"{s['avg_stop_pnl']:+.2f} | "
                     f"{s['avg_lock_in_gain']:+.2f} | "
                     f"{s['total_lock_in_gain']:+.2f} |")
    lines.append(f"| **TOTAL** | **{total_trig}** | — | — | — | **{total_lock:+.2f}** |")
    lines.append("")
    lines.append("## Insights")
    lines.append("")
    d_sharpe_full = full_stop["sharpe"] - full_base["sharpe"]
    d_dd_full = full_stop["max_dd_pct"] - full_base["max_dd_pct"]
    d_ret_full = full_stop["ret_pct"] - full_base["ret_pct"]
    d_sharpe_oos = test_stop["sharpe"] - test_base["sharpe"]
    d_dd_oos = test_stop["max_dd_pct"] - test_base["max_dd_pct"]
    lines.append(f"- Full period: Δ Sharpe {d_sharpe_full:+.3f}, "
                 f"Δ MaxDD {d_dd_full:+.2f}%, Δ Return {d_ret_full:+.2f}%.")
    lines.append(f"- OOS: Δ Sharpe {d_sharpe_oos:+.3f}, Δ MaxDD {d_dd_oos:+.2f}%.")
    lines.append(f"- Triggers: {total_trig} total across 10 assets. "
                 f"Aggregate lock-in gain vs held-to-rebalance: ${total_lock:+.2f}.")
    lines.append(f"- Extra fees paid (vs baseline): "
                 f"${stop_result['fee_total'] - base_result['fee_total']:+,.2f}.")
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    improved_sharpe = d_sharpe_full > 0.05 and d_sharpe_oos > 0
    improved_dd = d_dd_full > 1.0 and d_dd_oos >= 0  # DD is negative; larger (less negative) better
    if improved_sharpe and improved_dd:
        verdict = "PROMOTE — trailing stop improves both Sharpe and MaxDD IS & OOS."
    elif improved_sharpe and d_dd_oos >= -1.0:
        verdict = "LEAN PROMOTE — Sharpe up meaningfully, DD roughly flat."
    elif improved_dd and d_sharpe_oos >= -0.1:
        verdict = "DEFENSIVE ONLY — trims DD but not Sharpe; use if risk budget tight."
    elif total_trig == 0:
        verdict = "NO-OP — stop never activated (never 5% in favor); equivalent to baseline."
    else:
        verdict = "REJECT — trailing stop does not reliably improve risk-adjusted OOS."
    lines.append(verdict)
    lines.append("")
    lines.append("### Thesis check")
    lines.append("")
    lines.append("Thesis: 'trailing locks in winners that would have reverted, without "
                 "cutting losers prematurely (stop only arms after +5% favorable move).'")
    lines.append("")
    held_signed = "positive" if total_lock > 0 else ("neutral" if total_lock == 0 else "negative")
    lines.append(f"- Aggregate lock-in (stop-PnL − held-PnL across triggers) = "
                 f"${total_lock:+.2f} → {held_signed}.")
    lines.append(f"- If lock-in ≫ extra fees, thesis supported; if ~equal or negative, "
                 f"trail just churns.")
    lines.append("")
    lines.append("_No existing fixed-SL report found in `backtest/results/` at time of "
                 "writing; direct comparison pending. Baseline here is the no-stop variant._")

    path.write_text("\n".join(lines))
    print(f"\nReport written to {path}")


def main() -> int:
    ohlc = load_ohlc()
    ts, O, H, L, C = build_panels(ohlc)

    print("Running baseline (no stops)...")
    base_result = simulate(ts, O, H, L, C, use_stops=False)
    print("Running TRAIL-SL variant...")
    stop_result = simulate(ts, O, H, L, C, use_stops=True)

    full_end = ts[-1] + pd.Timedelta(hours=1)
    full_start = ts[base_result["start_idx"]]

    full_base = slice_metrics(ts, base_result["equity"], full_start, full_end, base_result["start_idx"])
    full_stop = slice_metrics(ts, stop_result["equity"], full_start, full_end, stop_result["start_idx"])
    train_base = slice_metrics(ts, base_result["equity"], full_start, SPLIT_DATE, base_result["start_idx"])
    test_base  = slice_metrics(ts, base_result["equity"], SPLIT_DATE, full_end, base_result["start_idx"])
    train_stop = slice_metrics(ts, stop_result["equity"], full_start, SPLIT_DATE, stop_result["start_idx"])
    test_stop  = slice_metrics(ts, stop_result["equity"], SPLIT_DATE, full_end, stop_result["start_idx"])

    asset_stats = per_asset_stats(stop_result)

    report_path = PROJECT_ROOT / "backtest" / "results" / "stoploss-TRAIL.md"
    write_report(report_path, full_base, full_stop, train_base, test_base,
                 train_stop, test_stop, base_result, stop_result, asset_stats)

    # 10-line stdout summary
    total_trig = sum(s["n_triggers"] for s in asset_stats)
    total_lock = sum(s["total_lock_in_gain"] for s in asset_stats)
    print("")
    print("=== Plan E TRAIL-SL (10% trail, activate +5%) vs baseline ===")
    print(f"Full  | Base: Sharpe {full_base['sharpe']:+.2f} Ret {full_base['ret_pct']:+.1f}% DD {full_base['max_dd_pct']:+.1f}%")
    print(f"Full  | Stop: Sharpe {full_stop['sharpe']:+.2f} Ret {full_stop['ret_pct']:+.1f}% DD {full_stop['max_dd_pct']:+.1f}%")
    print(f"Train | Base: Sharpe {train_base['sharpe']:+.2f}  Stop: Sharpe {train_stop['sharpe']:+.2f}")
    print(f"Test  | Base: Sharpe {test_base['sharpe']:+.2f}  Stop: Sharpe {test_stop['sharpe']:+.2f}")
    print(f"OOS DD | Base: {test_base['max_dd_pct']:+.2f}%  Stop: {test_stop['max_dd_pct']:+.2f}%")
    print(f"Triggers: {total_trig} total across {len(asset_stats)} assets")
    print(f"Aggregate lock-in (stop vs held-to-rb): ${total_lock:+.2f}")
    print(f"Extra fees vs baseline: ${stop_result['fee_total'] - base_result['fee_total']:+,.2f}")
    print(f"Report: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
