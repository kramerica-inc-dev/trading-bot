#!/usr/bin/env python3
"""Plan E SL-5 variant: fixed symmetric 5% stop-loss backtest.

Replicates the Plan E walk-forward baseline (72h log-return signal, sign=-1
reversal, top-3 long / bottom-3 short, 10% notional per leg, 24h rebalance
at UTC 08:00, k_exit=6 hysteresis) and adds an intrabar 5% stop:

  - LONG leg stops if bar.low <= entry * 0.95
  - SHORT leg stops if bar.high >= entry * 1.05
  - Fill at (entry * 0.95) / (entry * 1.05) UNLESS the bar's OPEN already
    gapped through the level, in which case we fill at that bar's open
    (worst-case gap fill)
  - Once stopped, the leg stays flat until the next 24h rebalance

Produces a report at backtest/results/stoploss-SL5.md comparing SL-5 vs the
no-stop baseline over the full 12-month window and a 70/30 walk-forward
split aligned with plan_e_walkforward.SPLIT_DATE.

This script does NOT modify the live runner or any existing backtest script.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# --- Configuration (matches plan_e_walkforward.py selected config) ----------
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
REBALANCE_ANCHOR_HOUR = 8          # UTC 08:00
SIGN = -1                          # reversal
K_EXIT = 6

FEE_RATE = 0.0006
SLIPPAGE_RATE = 0.0005
COST_PER_SIDE = FEE_RATE + SLIPPAGE_RATE    # 0.0011

STOP_PCT = 0.05
SPLIT_DATE = pd.Timestamp("2026-01-01", tz="UTC")   # matches walk-forward


# --- Data loading -----------------------------------------------------------
def load_ohlc() -> dict:
    """Load 1h OHLC for each universe symbol, intersected index across assets."""
    data_dir = PROJECT_ROOT / "backtest" / "data"
    raw = {}
    for sym in UNIVERSE:
        path = data_dir / f"{sym}_1H.csv"
        df = pd.read_csv(path)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.sort_values("timestamp").reset_index(drop=True)
        df = df.set_index("timestamp")
        raw[sym] = df[["open", "high", "low", "close"]]

    # Intersect timestamps
    common = None
    for sym, df in raw.items():
        common = df.index if common is None else common.intersection(df.index)

    opens = pd.DataFrame({s: raw[s].loc[common, "open"]  for s in UNIVERSE})
    highs = pd.DataFrame({s: raw[s].loc[common, "high"]  for s in UNIVERSE})
    lows  = pd.DataFrame({s: raw[s].loc[common, "low"]   for s in UNIVERSE})
    closes= pd.DataFrame({s: raw[s].loc[common, "close"] for s in UNIVERSE})
    # Drop any residual NaNs
    mask = ~(opens.isna().any(axis=1) | highs.isna().any(axis=1) |
             lows.isna().any(axis=1) | closes.isna().any(axis=1))
    opens, highs, lows, closes = opens[mask], highs[mask], lows[mask], closes[mask]
    print(f"Loaded {len(UNIVERSE)} assets, {len(closes)} common bars, "
          f"{closes.index[0]} -> {closes.index[-1]}")
    return {"open": opens, "high": highs, "low": lows, "close": closes}


# --- Simulator --------------------------------------------------------------
def simulate(data: dict, use_stop: bool) -> dict:
    """Runs the Plan E loop; if use_stop=True, applies SL-5 intrabar logic.

    Mimics plan_e_walkforward.run for rebalance/hysteresis accounting. For
    the stop variant, we track entry prices per leg and, on every bar after
    entry, check if low/high triggered the stop. Stopped leg:
      - realises PnL = weight_at_entry * eq_at_entry * (fill/entry - 1)   long
                     = weight_at_entry * eq_at_entry * (1 - fill/entry)   short
        where weight_at_entry is positive-magnitude for both (we use the
        stored signed weight at entry).
      - pays exit fee on |stopped_notional| at COST_PER_SIDE
      - stays at w=0 until next rebalance; no re-entry this cycle.

    To keep the flat-until-rebalance accounting consistent with the vectorized
    bar_pnl formula, we override the bar's simple_ret for the stopped asset
    with the realised move to the stop fill (since at end of bar the position
    is flat). This correctly produces the PnL and leaves w=0 going forward.
    """
    opens  = data["open"].values
    highs  = data["high"].values
    lows   = data["low"].values
    closes = data["close"].values
    ts     = data["close"].index

    simple_ret = data["close"].pct_change().values
    signal = (np.log(data["close"] / data["close"].shift(LOOKBACK_H)) * SIGN).values

    n_bars, n_assets = closes.shape
    cols = data["close"].columns.tolist()

    equity = np.full(n_bars, INITIAL_BALANCE)
    prev_w = np.zeros(n_assets)
    entry_price = np.zeros(n_assets)      # price at leg open; 0 if flat
    entry_weight = np.zeros(n_assets)     # signed weight at entry (for sizing)
    entry_equity = np.zeros(n_assets)     # eq at entry (for sizing)
    stopped_this_cycle = np.zeros(n_assets, dtype=bool)

    eq = INITIAL_BALANCE
    fee_total = 0.0
    start_idx = LOOKBACK_H

    rebalance_log = []    # cycle entries
    stop_log = []         # stop-trigger events
    # per-asset counters
    n_legs_opened = np.zeros(n_assets, dtype=int)
    n_stops = np.zeros(n_assets, dtype=int)
    loss_avoided_sum = np.zeros(n_assets, dtype=float)  # PnL_no_stop - PnL_stopped, per trigger

    # For loss-avoided: once stopped, track hypothetical position close at next rebalance.
    # Store (asset, stop_bar, stop_fill_price, entry_price, entry_weight, entry_equity, sign)
    # then on next rebalance compute hypothetical pnl using close-at-rebalance.
    pending_counterfactuals = []

    for i in range(start_idx, n_bars):
        # ---- 1. Bar PnL from positions held from i-1 into i ----
        r = simple_ret[i].copy()

        # Check stops intrabar (only if use_stop and we have open positions)
        stopped_mask = np.zeros(n_assets, dtype=bool)
        if use_stop:
            for a in range(n_assets):
                if prev_w[a] == 0 or entry_price[a] == 0:
                    continue
                ep = entry_price[a]
                if prev_w[a] > 0:
                    # long stop at ep * 0.95
                    stop_level = ep * (1.0 - STOP_PCT)
                    if lows[i, a] <= stop_level:
                        # Gap? If open already below stop, fill at open.
                        fill = opens[i, a] if opens[i, a] < stop_level else stop_level
                        # Override r[a] so bar PnL reflects the fill, then flatten.
                        # Position entered at some earlier close; prev_w * eq * r is the
                        # bar contribution. We want realised return for THIS bar to be
                        # (fill / close[i-1]) - 1 so that combined with prev bars gives
                        # fill/entry - 1 ... but prev bars already accrued. So for this
                        # bar specifically we want r = fill / prev_close - 1.
                        prev_close = closes[i - 1, a]
                        r[a] = (fill / prev_close) - 1.0
                        stopped_mask[a] = True
                        # log trigger
                        stop_log.append({
                            "ts": ts[i], "asset": cols[a], "side": "long",
                            "entry": ep, "stop_level": stop_level,
                            "bar_open": opens[i, a], "bar_low": lows[i, a],
                            "fill": fill, "gap": opens[i, a] < stop_level,
                        })
                        n_stops[a] += 1
                        pending_counterfactuals.append({
                            "asset_idx": a, "stop_bar": i, "fill": fill,
                            "entry": ep, "weight": entry_weight[a],
                            "eq_at_entry": entry_equity[a], "side": 1,
                        })
                else:  # short
                    stop_level = ep * (1.0 + STOP_PCT)
                    if highs[i, a] >= stop_level:
                        fill = opens[i, a] if opens[i, a] > stop_level else stop_level
                        prev_close = closes[i - 1, a]
                        r[a] = (fill / prev_close) - 1.0
                        stopped_mask[a] = True
                        stop_log.append({
                            "ts": ts[i], "asset": cols[a], "side": "short",
                            "entry": ep, "stop_level": stop_level,
                            "bar_open": opens[i, a], "bar_high": highs[i, a],
                            "fill": fill, "gap": opens[i, a] > stop_level,
                        })
                        n_stops[a] += 1
                        pending_counterfactuals.append({
                            "asset_idx": a, "stop_bar": i, "fill": fill,
                            "entry": ep, "weight": entry_weight[a],
                            "eq_at_entry": entry_equity[a], "side": -1,
                        })

        if np.isnan(r).any():
            bar_pnl = 0.0
        else:
            bar_pnl = float(np.sum(prev_w * eq * r))
        eq_before = eq + bar_pnl

        # Pay exit fee + flatten any stopped legs (after PnL applied)
        if stopped_mask.any():
            stopped_notional = float(np.sum(np.abs(prev_w[stopped_mask]) * eq_before))
            exit_fee = stopped_notional * COST_PER_SIDE
            fee_total += exit_fee
            eq_before -= exit_fee
            # Flatten and mark as stopped for this cycle
            prev_w = prev_w.copy()
            prev_w[stopped_mask] = 0.0
            stopped_this_cycle[stopped_mask] = True
            entry_price[stopped_mask] = 0.0
            entry_weight[stopped_mask] = 0.0
            entry_equity[stopped_mask] = 0.0

        # ---- 2. Rebalance at UTC 08:00 every 24h ----
        hour = ts[i].hour
        is_rebalance = (hour == REBALANCE_ANCHOR_HOUR) and not np.isnan(signal[i]).any()

        if is_rebalance:
            # Resolve any pending counterfactuals (evaluate hypothetical PnL had we
            # held the stopped position all the way to rebalance close).
            for cf in pending_counterfactuals:
                a = cf["asset_idx"]
                # hypothetical close-out price = close at this rebalance bar
                cf_close = closes[i, a]
                ep = cf["entry"]
                w_mag = abs(cf["weight"])
                eq_e = cf["eq_at_entry"]
                # PnL if we'd kept position from entry to this close
                if cf["side"] == 1:
                    pnl_hold = w_mag * eq_e * (cf_close / ep - 1.0)
                    pnl_stop = w_mag * eq_e * (cf["fill"] / ep - 1.0)
                else:
                    pnl_hold = w_mag * eq_e * (1.0 - cf_close / ep)
                    pnl_stop = w_mag * eq_e * (1.0 - cf["fill"] / ep)
                # loss-avoided = pnl_stop - pnl_hold  (positive => stop helped)
                loss_avoided_sum[a] += (pnl_stop - pnl_hold)
            pending_counterfactuals = []

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
                fee = eq_before * turnover * COST_PER_SIDE
                fee_total += fee
                eq = eq_before - fee

                # Record entries for legs that are now open (or changed side)
                for a in range(n_assets):
                    if new_w[a] != 0 and new_w[a] != prev_w[a]:
                        # newly opened (or flipped) -> record entry at close[i]
                        entry_price[a] = closes[i, a]
                        entry_weight[a] = new_w[a]
                        entry_equity[a] = eq
                        n_legs_opened[a] += 1
                    elif new_w[a] == 0 and prev_w[a] != 0:
                        entry_price[a] = 0.0
                        entry_weight[a] = 0.0
                        entry_equity[a] = 0.0
                    # retained (same sign, same weight) -> keep entry_price as is
                # Any asset that was stopped and is now being re-entered gets a new entry above.
                stopped_this_cycle = np.zeros(n_assets, dtype=bool)

                prev_w = new_w
                rebalance_log.append({
                    "ts": ts[i], "eq": eq, "turnover": turnover, "fee": fee,
                    "longs": [cols[j] for j in sorted(new_long)],
                    "shorts": [cols[j] for j in sorted(new_short)],
                })
            else:
                eq = eq_before
        else:
            eq = eq_before

        equity[i] = eq

    return {
        "ts": ts, "equity": equity, "fee_total": fee_total,
        "start_idx": start_idx, "rebalances": rebalance_log,
        "stop_log": stop_log, "n_legs_opened": n_legs_opened,
        "n_stops": n_stops, "loss_avoided_sum": loss_avoided_sum,
        "cols": cols,
    }


# --- Metrics ----------------------------------------------------------------
def slice_metrics(res: dict, start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> dict:
    ts = res["ts"]
    eq = res["equity"]
    mask = (ts >= start_ts) & (ts < end_ts)
    idx = np.where(mask)[0]
    if len(idx) < 2:
        return {"n_bars": 0, "sharpe": 0.0, "max_dd_pct": 0.0,
                "ret_pct": 0.0, "cagr_pct": 0.0, "eq_start": 0.0, "eq_end": 0.0}
    eq_slice = eq[idx]
    rr = np.diff(eq_slice) / eq_slice[:-1]
    rr = rr[np.isfinite(rr)]
    ann = 24 * 365
    mean_r = rr.mean() if len(rr) else 0.0
    std_r = rr.std(ddof=1) if len(rr) > 1 else 0.0
    sharpe = (mean_r * ann) / (std_r * np.sqrt(ann)) if std_r > 0 else 0.0
    peak = np.maximum.accumulate(eq_slice)
    dd = (eq_slice - peak) / peak
    max_dd = dd.min() if len(dd) else 0.0
    ret_pct = (eq_slice[-1] / eq_slice[0] - 1.0) * 100
    hours = len(eq_slice)
    years = hours / (24 * 365)
    cagr = ((eq_slice[-1] / eq_slice[0]) ** (1.0 / years) - 1.0) * 100 if years > 0 else 0.0
    return {
        "n_bars": len(idx),
        "eq_start": float(eq_slice[0]),
        "eq_end": float(eq_slice[-1]),
        "ret_pct": float(ret_pct),
        "cagr_pct": float(cagr),
        "sharpe": float(sharpe),
        "max_dd_pct": float(max_dd * 100),
    }


def turnover_trades_per_month(res: dict, start_ts, end_ts) -> float:
    rbs = [r for r in res["rebalances"] if start_ts <= r["ts"] < end_ts]
    if not rbs:
        return 0.0
    span_days = (end_ts - start_ts).total_seconds() / 86400
    months = span_days / 30.4375
    return len(rbs) / months if months > 0 else 0.0


def fees_in_slice(res: dict, start_ts, end_ts) -> float:
    # approximate: sum rebalance fees in slice; exit-stop fees not timestamped
    # per rebalance-log, so we estimate by scaling fee_total by bar ratio.
    ts = res["ts"]
    total_bars = len(ts) - res["start_idx"]
    mask = (ts >= start_ts) & (ts < end_ts)
    slice_bars = int(mask.sum())
    if total_bars <= 0:
        return 0.0
    return res["fee_total"] * (slice_bars / total_bars)


# --- Report -----------------------------------------------------------------
def fmt_delta(new: float, base: float, unit: str = "") -> str:
    d = new - base
    sign = "+" if d >= 0 else ""
    return f"{sign}{d:.2f}{unit}"


def build_report(base: dict, sl5: dict, data: dict) -> str:
    closes = data["close"]
    full_start = closes.index[LOOKBACK_H]
    full_end = closes.index[-1] + pd.Timedelta(hours=1)
    test_end = full_end

    base_full = slice_metrics(base, full_start, full_end)
    sl5_full  = slice_metrics(sl5,  full_start, full_end)
    base_is   = slice_metrics(base, full_start, SPLIT_DATE)
    sl5_is    = slice_metrics(sl5,  full_start, SPLIT_DATE)
    base_oos  = slice_metrics(base, SPLIT_DATE, test_end)
    sl5_oos   = slice_metrics(sl5,  SPLIT_DATE, test_end)

    base_tpm = turnover_trades_per_month(base, full_start, full_end)
    sl5_tpm  = turnover_trades_per_month(sl5,  full_start, full_end)

    lines = []
    lines.append("# Plan E SL-5 (symmetric 5% stop-loss) — backtest report")
    lines.append("")
    lines.append(f"**Generated:** {pd.Timestamp.now(tz='UTC').isoformat()}")
    lines.append(f"**Universe:** {', '.join(UNIVERSE)}")
    lines.append(f"**Range:** {closes.index[0]} -> {closes.index[-1]}")
    lines.append(f"**Bars:** {len(closes)} (1h)")
    lines.append(f"**Config:** lb={LOOKBACK_H}h, rb={REBALANCE_H}h @ UTC {REBALANCE_ANCHOR_HOUR:02d}:00, "
                 f"sign={SIGN}, k_exit={K_EXIT}, {LEG_NOTIONAL_PCT:.0%}/leg")
    lines.append(f"**Friction:** fee {FEE_RATE:.2%} + slip {SLIPPAGE_RATE:.2%} = "
                 f"{COST_PER_SIDE:.2%} per side")
    lines.append(f"**Stop:** symmetric {STOP_PCT:.0%} intrabar (long low, short high); "
                 "fill at stop or worse-case open gap; flat until next rebalance.")
    lines.append(f"**Train/Test split:** {SPLIT_DATE.date()}")
    lines.append("")

    lines.append("## Full-period comparison (12 months)")
    lines.append("")
    lines.append("| Metric | Baseline (no stop) | SL-5 | Delta |")
    lines.append("|--------|-------------------:|-----:|------:|")
    lines.append(f"| Final equity | ${base_full['eq_end']:,.2f} | "
                 f"${sl5_full['eq_end']:,.2f} | "
                 f"{fmt_delta(sl5_full['eq_end'], base_full['eq_end'])} |")
    lines.append(f"| Return % | {base_full['ret_pct']:+.2f}% | "
                 f"{sl5_full['ret_pct']:+.2f}% | "
                 f"{fmt_delta(sl5_full['ret_pct'], base_full['ret_pct'], 'pp')} |")
    lines.append(f"| CAGR % | {base_full['cagr_pct']:+.2f}% | "
                 f"{sl5_full['cagr_pct']:+.2f}% | "
                 f"{fmt_delta(sl5_full['cagr_pct'], base_full['cagr_pct'], 'pp')} |")
    lines.append(f"| Sharpe | {base_full['sharpe']:+.2f} | "
                 f"{sl5_full['sharpe']:+.2f} | "
                 f"{fmt_delta(sl5_full['sharpe'], base_full['sharpe'])} |")
    lines.append(f"| Max DD | {base_full['max_dd_pct']:+.2f}% | "
                 f"{sl5_full['max_dd_pct']:+.2f}% | "
                 f"{fmt_delta(sl5_full['max_dd_pct'], base_full['max_dd_pct'], 'pp')} |")
    lines.append(f"| Trades/month | {base_tpm:.1f} | {sl5_tpm:.1f} | "
                 f"{fmt_delta(sl5_tpm, base_tpm)} |")
    lines.append(f"| Total fees | ${base['fee_total']:,.2f} | "
                 f"${sl5['fee_total']:,.2f} | "
                 f"{fmt_delta(sl5['fee_total'], base['fee_total'], ' USD')} |")
    lines.append("")

    lines.append("## Walk-forward split")
    lines.append("")
    lines.append("| Slice | Metric | Baseline | SL-5 | Delta |")
    lines.append("|-------|--------|---------:|-----:|------:|")
    for tag, bm, sm in [("IS (train)", base_is, sl5_is), ("OOS (test)", base_oos, sl5_oos)]:
        lines.append(f"| {tag} | Sharpe | {bm['sharpe']:+.2f} | {sm['sharpe']:+.2f} | "
                     f"{fmt_delta(sm['sharpe'], bm['sharpe'])} |")
        lines.append(f"| {tag} | Max DD  | {bm['max_dd_pct']:+.2f}% | {sm['max_dd_pct']:+.2f}% | "
                     f"{fmt_delta(sm['max_dd_pct'], bm['max_dd_pct'], 'pp')} |")
        lines.append(f"| {tag} | Return  | {bm['ret_pct']:+.2f}% | {sm['ret_pct']:+.2f}% | "
                     f"{fmt_delta(sm['ret_pct'], bm['ret_pct'], 'pp')} |")
    lines.append("")

    # Per-asset trigger stats
    cols = sl5["cols"]
    lines.append("## Per-asset stop statistics (SL-5)")
    lines.append("")
    lines.append("| Asset | n_legs | n_triggers | trigger_rate | total_loss_avoided ($) | avg_loss_avoided ($/trig) |")
    lines.append("|-------|-------:|-----------:|-------------:|-----------------------:|--------------------------:|")
    per_asset_rows = []
    for a, sym in enumerate(cols):
        legs = int(sl5["n_legs_opened"][a])
        trigs = int(sl5["n_stops"][a])
        rate = (trigs / legs) if legs > 0 else 0.0
        total_av = float(sl5["loss_avoided_sum"][a])
        avg_av = (total_av / trigs) if trigs > 0 else 0.0
        per_asset_rows.append((sym, legs, trigs, rate, total_av, avg_av))
        lines.append(f"| {sym} | {legs} | {trigs} | {rate:.1%} | "
                     f"${total_av:+,.2f} | ${avg_av:+,.2f} |")
    lines.append("")

    # Top-5 insights
    ranked = sorted(per_asset_rows, key=lambda r: r[4], reverse=True)
    top5_benefit = ranked[:5]
    top5_suffer  = sorted(per_asset_rows, key=lambda r: r[4])[:5]
    lines.append("## Top-5 asset-level insights")
    lines.append("")
    lines.append("**Biggest beneficiaries (total_loss_avoided > 0 means stop helped):**")
    for sym, legs, trigs, rate, tot, avg in top5_benefit:
        lines.append(f"- {sym}: {trigs} triggers / {legs} legs ({rate:.1%}); "
                     f"total avoided ${tot:+,.2f} (avg ${avg:+,.2f}/trigger)")
    lines.append("")
    lines.append("**Worst sufferers (negative = stop fired too early, hurt PnL):**")
    for sym, legs, trigs, rate, tot, avg in top5_suffer:
        lines.append(f"- {sym}: {trigs} triggers / {legs} legs ({rate:.1%}); "
                     f"total avoided ${tot:+,.2f} (avg ${avg:+,.2f}/trigger)")
    lines.append("")

    # Verdict
    sharpe_delta_full = sl5_full["sharpe"] - base_full["sharpe"]
    sharpe_delta_oos  = sl5_oos["sharpe"]  - base_oos["sharpe"]
    dd_improve_full   = sl5_full["max_dd_pct"] - base_full["max_dd_pct"]   # +pp = smaller DD
    dd_improve_oos    = sl5_oos["max_dd_pct"]  - base_oos["max_dd_pct"]

    # Label: WINNER = Sharpe improves meaningfully OOS (>=+0.2) without return collapse,
    # or DD reduced by >=+3pp with OOS Sharpe loss <=0.15.
    # MARGINAL = mixed (small Sharpe gain/loss, small DD improvement).
    # REJECTED = OOS Sharpe drops >0.15 AND DD barely changes (<1pp improvement).
    if sharpe_delta_oos >= 0.2 and sl5_oos["ret_pct"] >= 0.5 * base_oos["ret_pct"]:
        label = "WINNER"
    elif dd_improve_oos >= 3.0 and sharpe_delta_oos >= -0.15:
        label = "WINNER"
    elif sharpe_delta_oos < -0.15 and dd_improve_oos < 1.0:
        label = "REJECTED"
    else:
        label = "MARGINAL"

    lines.append("## Verdict")
    lines.append("")
    lines.append(f"- Full-period Sharpe delta: **{sharpe_delta_full:+.2f}**")
    lines.append(f"- OOS Sharpe delta: **{sharpe_delta_oos:+.2f}**")
    lines.append(f"- Full-period DD delta: **{dd_improve_full:+.2f}pp** (positive = smaller DD)")
    lines.append(f"- OOS DD delta: **{dd_improve_oos:+.2f}pp**")
    lines.append(f"- Total stop triggers: {int(sl5['n_stops'].sum())} across "
                 f"{int(sl5['n_legs_opened'].sum())} legs "
                 f"({sl5['n_stops'].sum() / max(1, sl5['n_legs_opened'].sum()):.1%})")
    lines.append(f"- Sum loss_avoided (stop - hold) across all triggers: "
                 f"${sl5['loss_avoided_sum'].sum():+,.2f}")
    lines.append("")
    lines.append(f"**Risk-profile label: {label}**")
    lines.append("")

    return "\n".join(lines), {
        "base_full": base_full, "sl5_full": sl5_full,
        "base_oos": base_oos,   "sl5_oos": sl5_oos,
        "sharpe_delta_full": sharpe_delta_full,
        "sharpe_delta_oos": sharpe_delta_oos,
        "dd_improve_full": dd_improve_full,
        "dd_improve_oos": dd_improve_oos,
        "label": label,
        "n_triggers": int(sl5["n_stops"].sum()),
        "n_legs": int(sl5["n_legs_opened"].sum()),
    }


def main() -> int:
    data = load_ohlc()

    print("\nRunning baseline (no stop)...")
    base = simulate(data, use_stop=False)
    print(f"  final eq: ${base['equity'][-1]:,.2f}  fees: ${base['fee_total']:,.2f}  "
          f"rebalances: {len(base['rebalances'])}")

    print("\nRunning SL-5...")
    sl5 = simulate(data, use_stop=True)
    print(f"  final eq: ${sl5['equity'][-1]:,.2f}  fees: ${sl5['fee_total']:,.2f}  "
          f"rebalances: {len(sl5['rebalances'])}  stops: {int(sl5['n_stops'].sum())}")

    report, summary = build_report(base, sl5, data)
    out_path = PROJECT_ROOT / "backtest" / "results" / "stoploss-SL5.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report)
    print(f"\nReport written to {out_path}")

    # 10-line stdout summary
    print("\n" + "=" * 60)
    print("SL-5 BACKTEST SUMMARY")
    print("=" * 60)
    print(f"Full-period  Sharpe: base {summary['base_full']['sharpe']:+.2f}  "
          f"SL-5 {summary['sl5_full']['sharpe']:+.2f}  "
          f"(Δ{summary['sharpe_delta_full']:+.2f})")
    print(f"Full-period  MaxDD : base {summary['base_full']['max_dd_pct']:+.2f}%  "
          f"SL-5 {summary['sl5_full']['max_dd_pct']:+.2f}%  "
          f"(Δ{summary['dd_improve_full']:+.2f}pp)")
    print(f"OOS          Sharpe: base {summary['base_oos']['sharpe']:+.2f}  "
          f"SL-5 {summary['sl5_oos']['sharpe']:+.2f}  "
          f"(Δ{summary['sharpe_delta_oos']:+.2f})")
    print(f"OOS          MaxDD : base {summary['base_oos']['max_dd_pct']:+.2f}%  "
          f"SL-5 {summary['sl5_oos']['max_dd_pct']:+.2f}%  "
          f"(Δ{summary['dd_improve_oos']:+.2f}pp)")
    print(f"Full return : base {summary['base_full']['ret_pct']:+.1f}%  "
          f"SL-5 {summary['sl5_full']['ret_pct']:+.1f}%")
    print(f"OOS return  : base {summary['base_oos']['ret_pct']:+.1f}%  "
          f"SL-5 {summary['sl5_oos']['ret_pct']:+.1f}%")
    print(f"Triggers    : {summary['n_triggers']} / {summary['n_legs']} legs "
          f"({summary['n_triggers']/max(1,summary['n_legs']):.1%})")
    print(f"Loss-avoided: ${sl5['loss_avoided_sum'].sum():+,.2f} "
          f"(positive = stop helped in aggregate)")
    print(f"VERDICT     : {summary['label']}")
    print(f"Report      : {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
