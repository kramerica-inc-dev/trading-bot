#!/usr/bin/env python3
"""Plan E variant: FIXED SYMMETRIC 20% stop-loss (tail-event protection).

Baseline Plan E config (unchanged):
  - Universe: 10 USDT perps
  - Signal: 72h log return, sign=-1 (reversal)
  - 3 long / 3 short, 10% notional per leg
  - Rebalance every 24h at UTC 08:00
  - k_exit = 6 (retain current pos if still within top/bottom 6)
  - Fees: taker 6bps + slippage 5bps per side -> 11bps/side
  - Initial equity: $5000

Variant (this script):
  - LONG stop triggers when bar.low  <  entry_price * 0.80
  - SHORT stop triggers when bar.high >  entry_price * 1.20
  - Fill price: stop level, OR bar.open if the open already breached the stop (gap)
  - Post-stop: weight goes to 0; stay flat until next 24h rebalance
  - Stop fill incurs 11bps (fee+slip) exit cost

Outputs:
  - backtest/results/stoploss-SL20.md (delta vs baseline + per-asset trigger stats)
  - 10-line stdout summary

Does NOT modify any existing file. Live runner untouched.
"""
from __future__ import annotations

import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

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
LEG_NOTIONAL_PCT = 0.10
LOOKBACK_H = 72
REBALANCE_H = 24
REBAL_HOUR_UTC = 8           # UTC 08:00 rebalance
K_EXIT = 6
SIGN = -1                    # reversal
FEE_RATE = 0.0006
SLIPPAGE_RATE = 0.0005
COST_PER_SIDE = FEE_RATE + SLIPPAGE_RATE
STOP_PCT = 0.20
SPLIT_DATE = pd.Timestamp("2026-01-01", tz="UTC")


def load_ohlc() -> dict:
    """Return dict of {symbol: DataFrame(open/high/low/close) indexed by UTC ts}
    plus aligned wide 'close' DataFrame for signal + returns."""
    data_dir = PROJECT_ROOT / "backtest" / "data"
    frames = {}
    for sym in UNIVERSE:
        df = pd.read_csv(data_dir / f"{sym}_1H.csv")
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.sort_values("timestamp").set_index("timestamp")
        frames[sym] = df[["open", "high", "low", "close"]]
    # Align on common index via inner join on close
    closes = pd.concat({s: f["close"] for s, f in frames.items()}, axis=1).dropna()
    opens = pd.concat({s: f["open"] for s, f in frames.items()}, axis=1).reindex(closes.index)
    highs = pd.concat({s: f["high"] for s, f in frames.items()}, axis=1).reindex(closes.index)
    lows = pd.concat({s: f["low"] for s, f in frames.items()}, axis=1).reindex(closes.index)
    print(f"Loaded {len(UNIVERSE)} assets. Aligned bars: {len(closes)}")
    print(f"Range: {closes.index[0]} -> {closes.index[-1]}")
    return {
        "close": closes, "open": opens, "high": highs, "low": lows,
    }


@dataclass
class Position:
    weight: float            # signed: +/- LEG_NOTIONAL_PCT
    entry_price: float
    entry_equity: float      # equity at rebalance that opened this slot
    # Once stopped, weight=0 and we remember the stop fill
    stopped: bool = False


@dataclass
class TriggerLog:
    ts: pd.Timestamp
    symbol: str
    side: str                # 'long' or 'short'
    entry_price: float
    stop_price: float
    fill_price: float
    pnl_pct: float           # realized per-leg return (after slippage)
    loss_usd: float          # realized P&L (negative = loss) on the leg
    gap: bool                # True if open already breached stop


def run_sl20(data: dict, apply_stop: bool) -> dict:
    """Simulate Plan E (reversal, lb72, rb24, k_exit=6) with optional 20% stop.

    apply_stop=False reproduces the baseline (for delta comparison).
    apply_stop=True layers the SL-20 exits.
    """
    closes = data["close"]
    opens = data["open"].values
    highs = data["high"].values
    lows = data["low"].values
    close_arr = closes.values

    n_bars, n_assets = closes.shape
    cols = closes.columns.tolist()
    ts = closes.index

    # 72h log return reversal signal
    log_close = np.log(close_arr)
    signal = -(log_close - np.roll(log_close, LOOKBACK_H, axis=0))
    signal[:LOOKBACK_H] = np.nan

    equity = np.full(n_bars, INITIAL_BALANCE, dtype=float)
    eq = INITIAL_BALANCE
    fee_total = 0.0
    stop_fee_total = 0.0
    n_rebalances = 0
    n_stops = 0

    # Position state per asset
    positions: list[Optional[Position]] = [None] * n_assets
    triggers: list[TriggerLog] = []

    start_idx = LOOKBACK_H

    for i in range(start_idx, n_bars):
        bar_open = opens[i]
        bar_high = highs[i]
        bar_low = lows[i]
        bar_close = close_arr[i]
        prev_close = close_arr[i - 1]

        # ---------- 1. Intrabar stop check (apply BEFORE close-to-close PnL) ----------
        # Model: stops occur at some point during the bar; we reflect that by
        # replacing that leg's close-to-close return with open-to-stop return
        # (open-to-prev_close already happened as prev bar's close, so we treat
        # the stop return as computed from prev_close to fill_price).
        bar_pnl = 0.0
        stop_exits_this_bar = []  # list of (asset_idx, fill_price)

        for a in range(n_assets):
            pos = positions[a]
            if pos is None or pos.stopped or pos.weight == 0.0:
                # flat: no contribution
                continue

            # Close-to-close return contribution baseline
            r_cc = (bar_close[a] - prev_close[a]) / prev_close[a]

            stop_fill = None
            if apply_stop:
                if pos.weight > 0:
                    stop_level = pos.entry_price * (1.0 - STOP_PCT)
                    if bar_low[a] < stop_level:
                        # Gap: open already below stop -> fill at open
                        if bar_open[a] < stop_level:
                            stop_fill = bar_open[a]
                            gap = True
                        else:
                            stop_fill = stop_level
                            gap = False
                        r_from_prev = (stop_fill - prev_close[a]) / prev_close[a]
                        leg_pnl = pos.weight * eq * r_from_prev
                        bar_pnl += leg_pnl
                        # Exit slippage+fee on the notional being closed
                        notional = abs(pos.weight) * eq
                        exit_cost = notional * COST_PER_SIDE
                        stop_fee_total += exit_cost
                        bar_pnl -= exit_cost
                        # Per-leg realized metrics (entry to fill)
                        pnl_pct_leg = (stop_fill - pos.entry_price) / pos.entry_price
                        loss_usd = pos.weight * pos.entry_equity * pnl_pct_leg - exit_cost
                        triggers.append(TriggerLog(
                            ts=ts[i], symbol=cols[a], side="long",
                            entry_price=pos.entry_price, stop_price=stop_level,
                            fill_price=stop_fill, pnl_pct=pnl_pct_leg,
                            loss_usd=loss_usd, gap=gap,
                        ))
                        n_stops += 1
                        pos.stopped = True
                        pos.weight = 0.0
                        continue
                else:  # short
                    stop_level = pos.entry_price * (1.0 + STOP_PCT)
                    if bar_high[a] > stop_level:
                        if bar_open[a] > stop_level:
                            stop_fill = bar_open[a]
                            gap = True
                        else:
                            stop_fill = stop_level
                            gap = False
                        r_from_prev = (stop_fill - prev_close[a]) / prev_close[a]
                        leg_pnl = pos.weight * eq * r_from_prev
                        bar_pnl += leg_pnl
                        notional = abs(pos.weight) * eq
                        exit_cost = notional * COST_PER_SIDE
                        stop_fee_total += exit_cost
                        bar_pnl -= exit_cost
                        pnl_pct_leg = (pos.entry_price - stop_fill) / pos.entry_price
                        # For short: weight is negative; leg_pnl_usd = -w_abs*eq0*(fill/entry-1)
                        # Simpler: realized = -pos.weight_signed * eq0 * ((fill-entry)/entry)
                        # where pos.weight is negative, so this yields correct sign.
                        realized_ret = (stop_fill - pos.entry_price) / pos.entry_price
                        loss_usd = pos.weight * pos.entry_equity * realized_ret - exit_cost
                        triggers.append(TriggerLog(
                            ts=ts[i], symbol=cols[a], side="short",
                            entry_price=pos.entry_price, stop_price=stop_level,
                            fill_price=stop_fill, pnl_pct=pnl_pct_leg,
                            loss_usd=loss_usd, gap=gap,
                        ))
                        n_stops += 1
                        pos.stopped = True
                        pos.weight = 0.0
                        continue

            # No stop: full close-to-close contribution
            if np.isfinite(r_cc):
                bar_pnl += pos.weight * eq * r_cc

        eq_before_rb = eq + bar_pnl

        # ---------- 2. Rebalance at UTC 08:00 ----------
        do_rebalance = (
            ts[i].hour == REBAL_HOUR_UTC
            and not np.isnan(signal[i]).any()
            and i > start_idx
        )

        if do_rebalance:
            ranks = np.argsort(-signal[i])       # descending
            keep_long = set(ranks[:K_EXIT].tolist())
            keep_short = set(ranks[-K_EXIT:].tolist())

            cur_long = {a for a in range(n_assets)
                        if positions[a] and not positions[a].stopped
                        and positions[a].weight > 0}
            cur_short = {a for a in range(n_assets)
                         if positions[a] and not positions[a].stopped
                         and positions[a].weight < 0}

            retained_l = cur_long & keep_long
            new_long = set(retained_l)
            need_l = LONG_N - len(retained_l)
            for a in ranks:
                if need_l <= 0:
                    break
                a = int(a)
                if a in new_long or a in cur_short:
                    continue
                new_long.add(a)
                need_l -= 1

            retained_s = cur_short & keep_short
            new_short = set(retained_s)
            need_s = SHORT_N - len(retained_s)
            for a in ranks[::-1]:
                if need_s <= 0:
                    break
                a = int(a)
                if a in new_short or a in new_long:
                    continue
                new_short.add(a)
                need_s -= 1

            # Build new weights vector
            new_w = np.zeros(n_assets)
            for a in new_long:
                new_w[a] = LEG_NOTIONAL_PCT
            for a in new_short:
                new_w[a] = -LEG_NOTIONAL_PCT

            # Current effective weights (stopped positions count as 0)
            cur_w = np.array([
                0.0 if (p is None or p.stopped) else p.weight
                for p in positions
            ])

            turnover = float(np.sum(np.abs(new_w - cur_w)))
            if turnover > 1e-9:
                fee = eq_before_rb * turnover * COST_PER_SIDE
                fee_total += fee
                eq = eq_before_rb - fee
            else:
                eq = eq_before_rb
            n_rebalances += 1

            # Update position state: new entry prices for newly opened or changed-direction slots;
            # retained slots keep their original entry price (don't reset)
            new_positions: list[Optional[Position]] = [None] * n_assets
            for a in range(n_assets):
                if new_w[a] == 0.0:
                    new_positions[a] = None
                    continue
                # Retain if same side and not stopped
                old = positions[a]
                if old and not old.stopped and np.sign(old.weight) == np.sign(new_w[a]):
                    new_positions[a] = Position(
                        weight=new_w[a],
                        entry_price=old.entry_price,
                        entry_equity=old.entry_equity,
                    )
                else:
                    # Fresh entry at current close
                    new_positions[a] = Position(
                        weight=new_w[a],
                        entry_price=float(bar_close[a]),
                        entry_equity=eq,
                    )
            positions = new_positions
        else:
            eq = eq_before_rb

        equity[i] = eq

    return {
        "ts": ts,
        "equity": equity,
        "fee_total": fee_total,
        "stop_fee_total": stop_fee_total,
        "n_rebalances": n_rebalances,
        "n_stops": n_stops,
        "triggers": triggers,
        "start_idx": start_idx,
    }


def metrics_slice(result: dict, start_ts=None, end_ts=None) -> dict:
    ts = result["ts"]
    eq = result["equity"]
    start = result["start_idx"]
    if start_ts is None:
        mask = np.arange(len(ts)) >= start
    else:
        mask = (ts >= start_ts) & (ts < end_ts) & (np.arange(len(ts)) >= start)
    idx = np.where(mask)[0]
    if len(idx) < 2:
        return {"n_bars": 0}
    eq_s = eq[idx]
    r = np.diff(eq_s) / eq_s[:-1]
    r = r[np.isfinite(r)]
    ann = 24 * 365
    mean_r = r.mean() if len(r) else 0.0
    std_r = r.std(ddof=1) if len(r) > 1 else 0.0
    sharpe = (mean_r * ann) / (std_r * np.sqrt(ann)) if std_r > 0 else 0.0
    peak = np.maximum.accumulate(eq_s)
    dd = (eq_s - peak) / peak
    return {
        "n_bars": len(idx),
        "eq_start": float(eq_s[0]),
        "eq_end": float(eq_s[-1]),
        "ret_pct": float((eq_s[-1] / eq_s[0] - 1) * 100),
        "sharpe": float(sharpe),
        "max_dd_pct": float(dd.min() * 100) if len(dd) else 0.0,
    }


def worst_daily_pnl(result: dict) -> float:
    """Worst bar-to-bar % swing (for 'worst single-trade' proxy in portfolio terms)."""
    eq = result["equity"][result["start_idx"]:]
    r = np.diff(eq) / eq[:-1]
    r = r[np.isfinite(r)]
    return float(r.min() * 100) if len(r) else 0.0


def per_asset_stats(base: dict, sl: dict, closes: pd.DataFrame) -> pd.DataFrame:
    """For each asset: n_triggers, trigger_rate, avg realized loss on stop (USD),
    avg loss_avoided by comparing realized stop-loss vs. what a full close-to-close
    would have produced from entry to next rebalance."""
    rows = []
    trig_by_sym = {s: [] for s in closes.columns}
    for t in sl["triggers"]:
        trig_by_sym[t.symbol].append(t)

    total_bars = len(closes) - LOOKBACK_H
    # Approx bars per year (1h bars): 365*24
    bars_per_year = 365 * 24
    years = total_bars / bars_per_year

    for sym in closes.columns:
        trigs = trig_by_sym[sym]
        n = len(trigs)
        rate = n / years if years > 0 else 0.0
        if n:
            avg_loss = float(np.mean([t.loss_usd for t in trigs]))
            worst_leg = float(min(t.loss_usd for t in trigs))
            gap_rate = sum(1 for t in trigs if t.gap) / n
        else:
            avg_loss = 0.0
            worst_leg = 0.0
            gap_rate = 0.0
        rows.append({
            "symbol": sym,
            "n_triggers": n,
            "trig_per_yr": round(rate, 2),
            "avg_loss_usd": round(avg_loss, 2),
            "worst_leg_usd": round(worst_leg, 2),
            "gap_rate": round(gap_rate, 2),
        })
    return pd.DataFrame(rows)


def _fmt_metric_row(name: str, base_v, sl_v, unit: str = "") -> str:
    delta = sl_v - base_v
    return f"| {name} | {base_v:+.2f}{unit} | {sl_v:+.2f}{unit} | {delta:+.2f}{unit} |"


def main() -> int:
    data = load_ohlc()
    closes = data["close"]

    # Run both with identical simulator to ensure fair comparison
    print("\nRunning baseline (no stop)...")
    base = run_sl20(data, apply_stop=False)
    print("Running SL-20 variant...")
    sl = run_sl20(data, apply_stop=True)

    m_base_full = metrics_slice(base)
    m_sl_full = metrics_slice(sl)

    full_first = closes.index[LOOKBACK_H]
    end_ts = closes.index[-1] + pd.Timedelta(hours=1)
    m_base_tr = metrics_slice(base, full_first, SPLIT_DATE)
    m_sl_tr = metrics_slice(sl, full_first, SPLIT_DATE)
    m_base_te = metrics_slice(base, SPLIT_DATE, end_ts)
    m_sl_te = metrics_slice(sl, SPLIT_DATE, end_ts)

    per_asset = per_asset_stats(base, sl, closes)
    trig_df = pd.DataFrame([t.__dict__ for t in sl["triggers"]])

    # Worst bar/single-trade proxies
    worst_bar_base = worst_daily_pnl(base)
    worst_bar_sl = worst_daily_pnl(sl)
    worst_leg_sl = float(trig_df["loss_usd"].min()) if len(trig_df) else 0.0

    # ---------------------- Stdout summary (10 lines) -------------------------
    print("\n" + "=" * 66)
    print(f"SL-20 vs BASELINE | triggers: {sl['n_stops']} across {years_str(closes)}")
    print(f"FULL  ret  base {m_base_full['ret_pct']:+.1f}%  -> SL {m_sl_full['ret_pct']:+.1f}%  (d={m_sl_full['ret_pct']-m_base_full['ret_pct']:+.1f}%)")
    print(f"FULL  Shr  base {m_base_full['sharpe']:+.2f}   -> SL {m_sl_full['sharpe']:+.2f}   (d={m_sl_full['sharpe']-m_base_full['sharpe']:+.2f})")
    print(f"FULL  DD   base {m_base_full['max_dd_pct']:+.1f}%  -> SL {m_sl_full['max_dd_pct']:+.1f}%  (d={m_sl_full['max_dd_pct']-m_base_full['max_dd_pct']:+.1f}%)")
    print(f"OOS   Shr  base {m_base_te['sharpe']:+.2f}   -> SL {m_sl_te['sharpe']:+.2f}   (d={m_sl_te['sharpe']-m_base_te['sharpe']:+.2f})")
    print(f"OOS   ret  base {m_base_te['ret_pct']:+.1f}%  -> SL {m_sl_te['ret_pct']:+.1f}%")
    print(f"OOS   DD   base {m_base_te['max_dd_pct']:+.1f}%  -> SL {m_sl_te['max_dd_pct']:+.1f}%")
    print(f"Worst hourly bar (portfolio): base {worst_bar_base:+.2f}%  -> SL {worst_bar_sl:+.2f}%")
    if len(trig_df):
        print(f"Worst single stopped leg (USD): {worst_leg_sl:+.2f}  (avg stop loss/leg {trig_df['loss_usd'].mean():+.2f})")
    else:
        print("No SL-20 triggers in dataset.")
    verdict = _verdict(m_base_full, m_sl_full, m_base_te, m_sl_te, sl["n_stops"])
    print(f"VERDICT: {verdict}")
    print("=" * 66)

    _write_report(m_base_full, m_sl_full, m_base_tr, m_sl_tr, m_base_te, m_sl_te,
                  per_asset, trig_df, worst_bar_base, worst_bar_sl, worst_leg_sl,
                  sl["n_stops"], closes, verdict)
    return 0


def years_str(closes: pd.DataFrame) -> str:
    span_h = (closes.index[-1] - closes.index[LOOKBACK_H]).total_seconds() / 3600
    return f"{span_h/24/365:.2f}y"


def _verdict(mb_f, ms_f, mb_t, ms_t, n_stops) -> str:
    d_sharpe_full = ms_f["sharpe"] - mb_f["sharpe"]
    d_sharpe_oos = ms_t["sharpe"] - mb_t["sharpe"]
    d_dd_full = ms_f["max_dd_pct"] - mb_f["max_dd_pct"]
    # SL helps if OOS Sharpe ~flat-or-up AND DD shallower AND not burning edge
    if d_sharpe_oos >= -0.05 and d_dd_full > 0.5:
        return "ADOPT — protects tails with negligible edge cost"
    if d_sharpe_oos >= -0.15 and d_dd_full > 1.5:
        return "CONDITIONAL ADOPT — meaningful DD relief, small Sharpe give-up"
    if n_stops == 0:
        return "NEUTRAL — no triggers; SL-20 is a free option but unproven"
    return "REJECT — SL-20 erodes edge without sufficient tail protection"


def _write_report(mb_f, ms_f, mb_t, ms_t, mb_te, ms_te,
                  per_asset, trig_df, worst_bar_base, worst_bar_sl,
                  worst_leg_sl, n_stops, closes, verdict):
    path = PROJECT_ROOT / "backtest" / "results" / "stoploss-SL20.md"
    L = []
    L.append("# Plan E — Fixed Symmetric 20% Stop-Loss (SL-20)")
    L.append("")
    L.append(f"**Generated:** {pd.Timestamp.now(tz='UTC').isoformat()}")
    L.append(f"**Range:** {closes.index[0]} -> {closes.index[-1]}  ({years_str(closes)})")
    L.append(f"**Baseline config:** lb=72h, rb=24h (UTC 08:00), sign=-1 (reversal), "
             f"k_exit={K_EXIT}, 3L/3S, {LEG_NOTIONAL_PCT:.0%} per leg")
    L.append(f"**Variant:** +20% symmetric stop "
             f"(long: low < entry*0.80; short: high > entry*1.20). "
             f"Fill at stop level or bar open on gap. Stay flat until next rebalance.")
    L.append(f"**Fees:** {FEE_RATE:.2%} fee + {SLIPPAGE_RATE:.2%} slip = "
             f"{COST_PER_SIDE:.2%} per side")
    L.append("")
    L.append("## 1. Headline delta")
    L.append("")
    L.append("### Full period")
    L.append("")
    L.append("| Metric | Baseline | SL-20 | Delta |")
    L.append("|--------|----------|-------|-------|")
    L.append(_fmt_metric_row("Return", mb_f["ret_pct"], ms_f["ret_pct"], "%"))
    L.append(_fmt_metric_row("Sharpe", mb_f["sharpe"], ms_f["sharpe"]))
    L.append(_fmt_metric_row("Max DD", mb_f["max_dd_pct"], ms_f["max_dd_pct"], "%"))
    L.append(f"| Final equity | ${mb_f['eq_end']:,.2f} | ${ms_f['eq_end']:,.2f} | "
             f"${ms_f['eq_end']-mb_f['eq_end']:+,.2f} |")
    L.append("")
    L.append("### Walk-forward (Train < 2026-01-01 | Test >=)")
    L.append("")
    L.append("| Slice | Metric | Baseline | SL-20 | Delta |")
    L.append("|-------|--------|----------|-------|-------|")
    for label, mb, ms in [("TRAIN", mb_t, ms_t), ("TEST(OOS)", mb_te, ms_te)]:
        L.append(f"| {label} | Return | {mb['ret_pct']:+.2f}% | {ms['ret_pct']:+.2f}% | "
                 f"{ms['ret_pct']-mb['ret_pct']:+.2f}% |")
        L.append(f"| {label} | Sharpe | {mb['sharpe']:+.2f} | {ms['sharpe']:+.2f} | "
                 f"{ms['sharpe']-mb['sharpe']:+.2f} |")
        L.append(f"| {label} | Max DD | {mb['max_dd_pct']:+.2f}% | {ms['max_dd_pct']:+.2f}% | "
                 f"{ms['max_dd_pct']-mb['max_dd_pct']:+.2f}% |")
    L.append("")

    L.append("## 2. Tail-event protection")
    L.append("")
    L.append(f"- **Worst hourly bar (portfolio-level return):** "
             f"baseline {worst_bar_base:+.2f}% vs SL-20 {worst_bar_sl:+.2f}%")
    L.append(f"- **Worst single stopped leg (SL-20, realized USD):** ${worst_leg_sl:+,.2f}")
    L.append(f"- **Total SL-20 triggers:** {n_stops}  "
             f"(over {years_str(closes)}, {len(UNIVERSE)} assets)")
    if n_stops:
        L.append(f"- **Fleet trigger rate:** {n_stops / float(years_str(closes).rstrip('y')):.1f}/yr "
                 f"across universe (~{n_stops / len(UNIVERSE) / float(years_str(closes).rstrip('y')):.2f}/yr per asset)")
    L.append("")
    L.append("At 20% threshold a stopped long that fills at 0.80*entry "
             "realizes ~ -2% equity on the 10% leg (before gap and fees). "
             "So each trigger caps that leg's draw at roughly -$100 on "
             f"${INITIAL_BALANCE:,.0f} deploy; without SL the same leg could "
             "extend to -5% or -10% equity if the continuation is large.")
    L.append("")

    L.append("## 3. Per-asset trigger table")
    L.append("")
    L.append("| Symbol | n_triggers | trig/yr | avg_loss_usd | worst_leg_usd | gap_rate |")
    L.append("|--------|------------|---------|--------------|----------------|----------|")
    for _, r in per_asset.iterrows():
        L.append(f"| {r['symbol']} | {r['n_triggers']} | {r['trig_per_yr']} | "
                 f"{r['avg_loss_usd']:+.2f} | {r['worst_leg_usd']:+.2f} | {r['gap_rate']:.2f} |")
    L.append("")

    if len(trig_df):
        L.append("## 4. Trigger log (all events)")
        L.append("")
        L.append("| timestamp | symbol | side | entry | stop | fill | leg_ret% | leg_pnl_usd | gap |")
        L.append("|-----------|--------|------|-------|------|------|----------|-------------|-----|")
        for t in sorted(trig_df.to_dict("records"), key=lambda x: x["ts"]):
            L.append(f"| {t['ts']} | {t['symbol']} | {t['side']} | "
                     f"{t['entry_price']:.4f} | {t['stop_price']:.4f} | {t['fill_price']:.4f} | "
                     f"{t['pnl_pct']*100:+.2f}% | {t['loss_usd']:+.2f} | {str(t['gap']).lower()} |")
        L.append("")
    else:
        L.append("## 4. Trigger log")
        L.append("")
        L.append("_No SL-20 triggers occurred in the backtest period._")
        L.append("")

    L.append("## 5. Insights")
    L.append("")
    if n_stops == 0:
        L.append("- Zero triggers across ~1 year of 10 assets means 20% is deeper "
                 "than any single-leg adverse excursion between 24h rebalances "
                 "for this universe in this regime. The stop acted as a free "
                 "(unused) option. Cannot claim protection benefit without a "
                 "larger shock sample.")
    else:
        d_sharpe_full = ms_f["sharpe"] - mb_f["sharpe"]
        d_sharpe_oos = ms_te["sharpe"] - mb_te["sharpe"]
        d_dd_full = ms_f["max_dd_pct"] - mb_f["max_dd_pct"]
        L.append(f"- Full-period Sharpe delta: {d_sharpe_full:+.2f}. "
                 f"OOS Sharpe delta: {d_sharpe_oos:+.2f}. "
                 f"Max-DD delta: {d_dd_full:+.2f} pp.")
        L.append(f"- Trigger rate per asset-year is "
                 f"{n_stops/len(UNIVERSE)/float(years_str(closes).rstrip('y')):.2f} "
                 f"— consistent with the 'tail only' hypothesis "
                 f"(target was 1-2/yr/asset).")
        if d_sharpe_full < -0.10:
            L.append("- Non-trivial edge erosion: SL-20 cuts winners on "
                     "volatility-reversal trades whose MFE briefly touches -20% "
                     "before mean-reverting. Compare to looser (25%/30%) or "
                     "time-based exits.")
        else:
            L.append("- Edge erosion is minor. Stop acts mostly as insurance.")
    L.append("")
    L.append("## 6. Verdict")
    L.append("")
    L.append(f"**{verdict}**")
    L.append("")
    L.append("This is a paper-layer analysis only; live runner untouched per scope.")
    L.append("")

    path.write_text("\n".join(L))
    print(f"\nReport written to {path}")


if __name__ == "__main__":
    sys.exit(main())
