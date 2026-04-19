#!/usr/bin/env python3
"""Plan E variant: VOLATILITY-SCALED stop-loss.

Stop level per position computed at entry:
    sigma_24h = sqrt(24) * stdev(hourly log returns over prior 30d = 720h)
    LONG  stop = entry * exp(-2 * sigma_24h)     trigger if bar.low  < stop
    SHORT stop = entry * exp(+2 * sigma_24h)     trigger if bar.high > stop

Fill at stop level; if bar opened past the stop (gap), fill at open.
Post-stop: flat in that asset until the next 24h rebalance at UTC 08:00.

If <720h of prior data when entering, fall back to a fixed 5% stop.

Does NOT modify the live runner or any existing file.
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
LEG_NOTIONAL_PCT = 0.10
LOOKBACK_H = 72
REBALANCE_H = 24
REBALANCE_HOUR_UTC = 8
K_EXIT = 6
SIGN = -1
FEE_RATE = 0.0006
SLIPPAGE_RATE = 0.0005
COST_PER_SIDE = FEE_RATE + SLIPPAGE_RATE    # 0.0011

SIGMA_WINDOW_H = 24 * 30                     # 30d of hourly bars
SIGMA_K = 2.0                                # 2 sigma stop
FALLBACK_STOP_PCT = 0.05                     # 5% fixed if insufficient data

SPLIT_DATE = pd.Timestamp("2026-01-01", tz="UTC")


def load_ohlc() -> dict[str, pd.DataFrame]:
    data_dir = PROJECT_ROOT / "backtest" / "data"
    frames: dict[str, pd.DataFrame] = {}
    for sym in UNIVERSE:
        df = pd.read_csv(data_dir / f"{sym}_1H.csv")
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.sort_values("timestamp").reset_index(drop=True).set_index("timestamp")
        frames[sym] = df[["open", "high", "low", "close"]]
    return frames


def build_wide(ohlc: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Return aligned wide-frame per field on the common (dropna) index."""
    close = pd.DataFrame({s: ohlc[s]["close"] for s in UNIVERSE}).dropna(how="any")
    idx = close.index
    open_ = pd.DataFrame({s: ohlc[s]["open"]  for s in UNIVERSE}).reindex(idx)
    high  = pd.DataFrame({s: ohlc[s]["high"]  for s in UNIVERSE}).reindex(idx)
    low   = pd.DataFrame({s: ohlc[s]["low"]   for s in UNIVERSE}).reindex(idx)
    return {"open": open_, "high": high, "low": low, "close": close}


def precompute_sigma(close: pd.DataFrame) -> np.ndarray:
    """sigma_24h (decimal) at each bar, per asset. NaN until 720h of history."""
    log_ret = np.log(close / close.shift(1))
    sigma_1h = log_ret.rolling(window=SIGMA_WINDOW_H, min_periods=SIGMA_WINDOW_H).std(ddof=1)
    sigma_24h = sigma_1h * np.sqrt(24.0)
    return sigma_24h.values


def run(frames: dict[str, pd.DataFrame]) -> dict:
    close = frames["close"]
    open_ = frames["open"].values
    high = frames["high"].values
    low = frames["low"].values
    closes_v = close.values
    ts = close.index
    n_bars, n_assets = close.shape
    cols = close.columns.tolist()

    simple_ret = close.pct_change().values
    signal = np.log(close / close.shift(LOOKBACK_H)).values * SIGN
    sigma_arr = precompute_sigma(close)  # (n_bars, n_assets)

    start_idx = max(LOOKBACK_H, 1)

    equity = np.full(n_bars, INITIAL_BALANCE)
    eq = INITIAL_BALANCE
    prev_w = np.zeros(n_assets)
    entry_px = np.zeros(n_assets)
    stop_px = np.zeros(n_assets)
    stopped = np.zeros(n_assets, dtype=bool)  # flat until next rebalance if True
    sigma_at_entry = np.full(n_assets, np.nan)
    stop_dist_at_entry = np.full(n_assets, np.nan)

    fee_total = 0.0
    gross_pnl_total = 0.0
    turnover_total = 0.0
    n_rebalances = 0

    # Per-asset diagnostics
    per_asset_triggers = np.zeros(n_assets, dtype=int)
    per_asset_entries = np.zeros(n_assets, dtype=int)
    per_asset_stop_dist_sum = np.zeros(n_assets)
    per_asset_sigma_sum = np.zeros(n_assets)
    per_asset_fallback_count = np.zeros(n_assets, dtype=int)

    for i in range(start_idx, n_bars):
        r = simple_ret[i]
        eff_w = prev_w.copy()

        # 1) Check intrabar stop triggers on open positions (before P&L).
        #    The bar P&L is computed close-to-close via simple_ret; to keep it
        #    consistent we override the per-asset return when a stop fires:
        #    return(asset) = (exit_px - prev_close) / prev_close.
        overridden_ret = r.copy()
        for a in range(n_assets):
            if prev_w[a] == 0.0 or stopped[a]:
                continue
            stop = stop_px[a]
            if prev_w[a] > 0:
                # LONG: stop if low < stop
                if not np.isnan(low[i, a]) and low[i, a] < stop:
                    exit_px = min(open_[i, a], stop) if open_[i, a] < stop else stop
                    prev_close = closes_v[i - 1, a]
                    if np.isfinite(prev_close) and prev_close > 0:
                        overridden_ret[a] = (exit_px - prev_close) / prev_close
                    per_asset_triggers[a] += 1
                    stopped[a] = True
                    eff_w[a] = 0.0
            else:
                # SHORT: stop if high > stop
                if not np.isnan(high[i, a]) and high[i, a] > stop:
                    exit_px = max(open_[i, a], stop) if open_[i, a] > stop else stop
                    prev_close = closes_v[i - 1, a]
                    if np.isfinite(prev_close) and prev_close > 0:
                        overridden_ret[a] = (exit_px - prev_close) / prev_close
                    per_asset_triggers[a] += 1
                    stopped[a] = True
                    eff_w[a] = 0.0

        # 2) Bar P&L using original weights (stops exit DURING the bar).
        if not np.isnan(overridden_ret).any():
            bar_pnl = float(np.sum(prev_w * eq * overridden_ret))
        else:
            bar_pnl = 0.0
        gross_pnl_total += bar_pnl
        eq_before = eq + bar_pnl

        # After the bar, positions that stopped out have zero weight going forward.
        prev_w = eff_w

        # 3) Rebalance at UTC 08:00 daily.
        hour = ts[i].hour
        do_rebalance = (hour == REBALANCE_HOUR_UTC) and not np.isnan(signal[i]).any()

        if do_rebalance:
            ranks = np.argsort(-signal[i])
            keep_long = set(ranks[:K_EXIT].tolist())
            keep_short = set(ranks[-K_EXIT:].tolist())
            cur_long = set(np.where(prev_w > 0)[0].tolist())
            cur_short = set(np.where(prev_w < 0)[0].tolist())

            # Retention logic mirrors walk-forward script (k_exit=6).
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
            if turnover > 1e-12:
                fee = eq_before * turnover * COST_PER_SIDE
                fee_total += fee
                turnover_total += turnover
                n_rebalances += 1
                eq = eq_before - fee

                # Update entry px, stop px, sigma for any newly-opened or flipped leg.
                for a in range(n_assets):
                    if new_w[a] == 0.0:
                        entry_px[a] = 0.0
                        stop_px[a] = 0.0
                        sigma_at_entry[a] = np.nan
                        stop_dist_at_entry[a] = np.nan
                        stopped[a] = False
                        continue
                    # Opening a fresh leg, or sign flipped, or previously stopped:
                    is_new = (prev_w[a] == 0.0) or (np.sign(new_w[a]) != np.sign(prev_w[a])) or stopped[a]
                    if is_new:
                        ep = closes_v[i, a]
                        entry_px[a] = ep
                        sig = sigma_arr[i, a]
                        used_fallback = False
                        if not np.isfinite(sig) or sig <= 0:
                            # Fallback: fixed 5% stop
                            if new_w[a] > 0:
                                sp = ep * (1.0 - FALLBACK_STOP_PCT)
                                dist = FALLBACK_STOP_PCT
                            else:
                                sp = ep * (1.0 + FALLBACK_STOP_PCT)
                                dist = FALLBACK_STOP_PCT
                            used_fallback = True
                            sigma_at_entry[a] = np.nan
                        else:
                            if new_w[a] > 0:
                                sp = ep * np.exp(-SIGMA_K * sig)
                                dist = 1.0 - np.exp(-SIGMA_K * sig)
                            else:
                                sp = ep * np.exp(+SIGMA_K * sig)
                                dist = np.exp(+SIGMA_K * sig) - 1.0
                            sigma_at_entry[a] = sig
                        stop_px[a] = sp
                        stop_dist_at_entry[a] = dist
                        stopped[a] = False

                        per_asset_entries[a] += 1
                        per_asset_stop_dist_sum[a] += dist
                        if used_fallback:
                            per_asset_fallback_count[a] += 1
                        else:
                            per_asset_sigma_sum[a] += sig

                prev_w = new_w
            else:
                eq = eq_before
                # Even if no turnover, clear stopped flags at the rebalance mark
                # (the signal said: keep these; treat as a fresh interval).
                for a in range(n_assets):
                    if stopped[a] and new_w[a] != 0.0:
                        # Re-open at current close with fresh stop.
                        ep = closes_v[i, a]
                        entry_px[a] = ep
                        sig = sigma_arr[i, a]
                        if not np.isfinite(sig) or sig <= 0:
                            if new_w[a] > 0:
                                sp = ep * (1.0 - FALLBACK_STOP_PCT)
                                dist = FALLBACK_STOP_PCT
                            else:
                                sp = ep * (1.0 + FALLBACK_STOP_PCT)
                                dist = FALLBACK_STOP_PCT
                        else:
                            if new_w[a] > 0:
                                sp = ep * np.exp(-SIGMA_K * sig)
                                dist = 1.0 - np.exp(-SIGMA_K * sig)
                            else:
                                sp = ep * np.exp(+SIGMA_K * sig)
                                dist = np.exp(+SIGMA_K * sig) - 1.0
                        stop_px[a] = sp
                        stop_dist_at_entry[a] = dist
                        stopped[a] = False
                        prev_w[a] = new_w[a]
        else:
            eq = eq_before

        equity[i] = eq

    return {
        "ts": ts,
        "equity": equity,
        "fee_total": fee_total,
        "gross_pnl_total": gross_pnl_total,
        "turnover_total": turnover_total,
        "n_rebalances": n_rebalances,
        "start_idx": start_idx,
        "cols": cols,
        "per_asset_triggers": per_asset_triggers,
        "per_asset_entries": per_asset_entries,
        "per_asset_stop_dist_sum": per_asset_stop_dist_sum,
        "per_asset_sigma_sum": per_asset_sigma_sum,
        "per_asset_fallback_count": per_asset_fallback_count,
    }


def slice_metrics(ts, eq, start_ts, end_ts) -> dict:
    mask = (ts >= start_ts) & (ts < end_ts)
    idx = np.where(mask)[0]
    if len(idx) < 2:
        return {"n_bars": 0}
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
    eq_start, eq_end = float(eq_slice[0]), float(eq_slice[-1])
    ret_pct = (eq_end / eq_start - 1.0) * 100.0
    # CAGR over slice
    hours = len(idx)
    years = hours / (24.0 * 365.0)
    cagr = ((eq_end / eq_start) ** (1.0 / years) - 1.0) * 100.0 if years > 0 and eq_start > 0 else 0.0
    return {
        "n_bars": len(idx),
        "eq_start": eq_start,
        "eq_end": eq_end,
        "ret_pct": ret_pct,
        "sharpe": float(sharpe),
        "max_dd_pct": float(max_dd * 100.0),
        "cagr_pct": float(cagr),
    }


# ---- Baseline replay (no stops) for delta comparison ----
def run_baseline(frames: dict[str, pd.DataFrame]) -> dict:
    """Same config as Plan E walk-forward winner (lb=72, rb=24h@08:00, k_exit=6, SIGN=-1)
    but without stops. Used to compute delta vs SL-VOL variant."""
    close = frames["close"]
    closes_v = close.values
    ts = close.index
    n_bars, n_assets = close.shape
    simple_ret = close.pct_change().values
    signal = np.log(close / close.shift(LOOKBACK_H)).values * SIGN

    equity = np.full(n_bars, INITIAL_BALANCE)
    eq = INITIAL_BALANCE
    prev_w = np.zeros(n_assets)
    fee_total = 0.0
    gross_pnl_total = 0.0

    start_idx = LOOKBACK_H
    for i in range(start_idx, n_bars):
        r = simple_ret[i]
        bar_pnl = 0.0 if np.isnan(r).any() else float(np.sum(prev_w * eq * r))
        gross_pnl_total += bar_pnl
        eq_before = eq + bar_pnl

        if ts[i].hour == REBALANCE_HOUR_UTC and not np.isnan(signal[i]).any():
            ranks = np.argsort(-signal[i])
            keep_long = set(ranks[:K_EXIT].tolist())
            keep_short = set(ranks[-K_EXIT:].tolist())
            cur_long = set(np.where(prev_w > 0)[0].tolist())
            cur_short = set(np.where(prev_w < 0)[0].tolist())
            retained_l = cur_long & keep_long
            new_long = set(retained_l)
            need_l = LONG_N - len(retained_l)
            for a in ranks:
                if need_l <= 0: break
                if int(a) in new_long: continue
                new_long.add(int(a)); need_l -= 1
            retained_s = cur_short & keep_short
            new_short = set(retained_s)
            need_s = SHORT_N - len(retained_s)
            for a in ranks[::-1]:
                if need_s <= 0: break
                if int(a) in new_short: continue
                new_short.add(int(a)); need_s -= 1
            new_w = np.zeros(n_assets)
            for a in new_long: new_w[a] = LEG_NOTIONAL_PCT
            for a in new_short: new_w[a] = -LEG_NOTIONAL_PCT
            turnover = float(np.sum(np.abs(new_w - prev_w)))
            if turnover > 1e-12:
                fee = eq_before * turnover * COST_PER_SIDE
                fee_total += fee
                eq = eq_before - fee
                prev_w = new_w
            else:
                eq = eq_before
        else:
            eq = eq_before
        equity[i] = eq

    return {"ts": ts, "equity": equity, "fee_total": fee_total,
            "gross_pnl_total": gross_pnl_total, "start_idx": start_idx}


def write_report(path: Path, frames, res_v, res_b, m_full_v, m_train_v, m_test_v,
                 m_full_b, m_train_b, m_test_b):
    cols = res_v["cols"]
    ts = res_v["ts"]
    lines = []
    lines.append("# Plan E variant — VOLATILITY-SCALED stop-loss (2× 30d hourly σ, per asset)")
    lines.append("")
    lines.append(f"**Generated:** {pd.Timestamp.now(tz='UTC').isoformat()}")
    lines.append(f"**Base config:** lb=72h REV, rb=24h @ UTC {REBALANCE_HOUR_UTC:02d}:00, "
                 f"k_exit={K_EXIT}, 3L/3S, {LEG_NOTIONAL_PCT:.0%}/leg, cost/side={COST_PER_SIDE*1e4:.1f}bps")
    lines.append(f"**Stop rule:** σ_24h = √24 · stdev(log r_1h over prior {SIGMA_WINDOW_H}h); "
                 f"LONG=entry·exp(−{SIGMA_K:.0f}σ), SHORT=entry·exp(+{SIGMA_K:.0f}σ). "
                 f"Fallback {FALLBACK_STOP_PCT:.0%} if σ unavailable.")
    lines.append(f"**Range:** {ts[0]} → {ts[-1]}   (Train < {SPLIT_DATE.date()} | Test ≥ {SPLIT_DATE.date()})")
    lines.append("")

    lines.append("## Delta vs baseline (no stop)")
    lines.append("")
    lines.append("| Slice | Metric | Baseline | SL-VOL | Δ |")
    lines.append("|-------|--------|----------|--------|---|")
    for tag, mv, mb in [("Full", m_full_v, m_full_b), ("Train", m_train_v, m_train_b),
                         ("Test (OOS)", m_test_v, m_test_b)]:
        if mv.get("n_bars", 0) == 0 or mb.get("n_bars", 0) == 0:
            continue
        lines.append(f"| {tag} | Sharpe | {mb['sharpe']:+.2f} | {mv['sharpe']:+.2f} | {mv['sharpe']-mb['sharpe']:+.2f} |")
        lines.append(f"| {tag} | Return % | {mb['ret_pct']:+.1f}% | {mv['ret_pct']:+.1f}% | {mv['ret_pct']-mb['ret_pct']:+.1f}pp |")
        lines.append(f"| {tag} | CAGR % | {mb.get('cagr_pct', 0):+.1f}% | {mv.get('cagr_pct', 0):+.1f}% | {mv.get('cagr_pct',0)-mb.get('cagr_pct',0):+.1f}pp |")
        lines.append(f"| {tag} | Max DD % | {mb['max_dd_pct']:+.1f}% | {mv['max_dd_pct']:+.1f}% | {mv['max_dd_pct']-mb['max_dd_pct']:+.1f}pp |")
    lines.append("")
    lines.append(f"**Turnover (SL-VOL):** {res_v['turnover_total']:.2f} total across "
                 f"{res_v['n_rebalances']} rebalances | **Fees:** ${res_v['fee_total']:.2f} | "
                 f"Baseline fees: ${res_b['fee_total']:.2f}")
    lines.append("")

    # Per-asset
    lines.append("## Per-asset stop diagnostics")
    lines.append("")
    lines.append("| Asset | Entries | Triggers | Trigger rate | Avg σ_24h | Avg stop dist | Fallback |")
    lines.append("|-------|---------|----------|--------------|-----------|---------------|----------|")
    for a, sym in enumerate(cols):
        ent = res_v["per_asset_entries"][a]
        tri = res_v["per_asset_triggers"][a]
        fb = res_v["per_asset_fallback_count"][a]
        tr = (tri / ent) if ent > 0 else 0.0
        n_sigma_used = max(ent - fb, 1)
        avg_sigma = res_v["per_asset_sigma_sum"][a] / n_sigma_used if (ent - fb) > 0 else float("nan")
        avg_dist = (res_v["per_asset_stop_dist_sum"][a] / ent) if ent > 0 else float("nan")
        sigma_str = f"{avg_sigma*100:.2f}%" if np.isfinite(avg_sigma) else "n/a"
        dist_str = f"{avg_dist*100:.2f}%" if np.isfinite(avg_dist) else "n/a"
        lines.append(f"| {sym} | {ent} | {tri} | {tr*100:.1f}% | {sigma_str} | {dist_str} | {fb} |")
    lines.append("")

    # Insights
    lines.append("## Insights")
    lines.append("")
    dd_delta = m_full_v["max_dd_pct"] - m_full_b["max_dd_pct"]
    sh_delta = m_full_v["sharpe"] - m_full_b["sharpe"]
    ret_delta = m_full_v["ret_pct"] - m_full_b["ret_pct"]
    oos_sh_delta = m_test_v["sharpe"] - m_test_b["sharpe"]
    lines.append(f"- Full-period Sharpe Δ: {sh_delta:+.2f}; return Δ: {ret_delta:+.1f}pp; "
                 f"max-DD Δ: {dd_delta:+.1f}pp.")
    lines.append(f"- OOS Sharpe Δ: {oos_sh_delta:+.2f} — stops help/hurt OOS specifically.")
    # range of avg stop dist
    dists = [(cols[a], (res_v["per_asset_stop_dist_sum"][a] / res_v["per_asset_entries"][a])
              if res_v["per_asset_entries"][a] > 0 else np.nan) for a in range(len(cols))]
    dists = [(s, d) for s, d in dists if np.isfinite(d)]
    if dists:
        dists.sort(key=lambda t: t[1])
        tightest = dists[0]; widest = dists[-1]
        lines.append(f"- Stop width ranges from {tightest[0]} ({tightest[1]*100:.1f}%) "
                     f"to {widest[0]} ({widest[1]*100:.1f}%) — "
                     f"vol-scaling gives high-vol names wider buffers as intended.")
    trig_rates = [(cols[a], res_v["per_asset_triggers"][a] / res_v["per_asset_entries"][a])
                  for a in range(len(cols)) if res_v["per_asset_entries"][a] > 0]
    if trig_rates:
        trig_rates.sort(key=lambda t: -t[1])
        lines.append(f"- Highest trigger rate: {trig_rates[0][0]} ({trig_rates[0][1]*100:.1f}%); "
                     f"lowest: {trig_rates[-1][0]} ({trig_rates[-1][1]*100:.1f}%).")
    lines.append("")

    # Verdict
    passes = (m_test_v["sharpe"] > m_test_b["sharpe"]) and (m_full_v["max_dd_pct"] >= m_full_b["max_dd_pct"])
    lines.append("## Verdict")
    lines.append("")
    if passes:
        lines.append("**ADOPT** — vol-scaled stops improve OOS Sharpe without worsening max drawdown. "
                     "Proportional stop widths avoid the fixed-% regime problem.")
    else:
        lines.append("**REJECT** — vol-scaled stops do not clear the bar (no OOS Sharpe gain and/or "
                     "worse drawdown). Mean-reversion signal benefits from letting trades breathe; "
                     "premature stops truncate the reversion leg.")
    lines.append("")
    path.write_text("\n".join(lines))
    print(f"Report written to {path}")


def main() -> int:
    ohlc = load_ohlc()
    frames = build_wide(ohlc)
    close = frames["close"]
    print(f"Universe: {len(close.columns)} assets | Bars: {len(close)} | "
          f"Range: {close.index[0]} → {close.index[-1]}")

    res_v = run(frames)
    res_b = run_baseline(frames)

    ts = res_v["ts"]
    full_start = ts[res_v["start_idx"]]
    end_plus = ts[-1] + pd.Timedelta(hours=1)

    m_full_v = slice_metrics(ts, res_v["equity"], full_start, end_plus)
    m_train_v = slice_metrics(ts, res_v["equity"], full_start, SPLIT_DATE)
    m_test_v = slice_metrics(ts, res_v["equity"], SPLIT_DATE, end_plus)

    m_full_b = slice_metrics(ts, res_b["equity"], full_start, end_plus)
    m_train_b = slice_metrics(ts, res_b["equity"], full_start, SPLIT_DATE)
    m_test_b = slice_metrics(ts, res_b["equity"], SPLIT_DATE, end_plus)

    # 10-line summary
    print("\n== SL-VOL (2σ, 30d) summary ==")
    print(f"Full   | Base Sharpe {m_full_b['sharpe']:+.2f} → SL {m_full_v['sharpe']:+.2f} "
          f"(Δ {m_full_v['sharpe']-m_full_b['sharpe']:+.2f}) | ret {m_full_v['ret_pct']:+.1f}% | "
          f"DD {m_full_v['max_dd_pct']:+.1f}% | CAGR {m_full_v['cagr_pct']:+.1f}%")
    print(f"Train  | Base Sharpe {m_train_b['sharpe']:+.2f} → SL {m_train_v['sharpe']:+.2f} | "
          f"ret {m_train_v['ret_pct']:+.1f}% | DD {m_train_v['max_dd_pct']:+.1f}%")
    print(f"Test   | Base Sharpe {m_test_b['sharpe']:+.2f} → SL {m_test_v['sharpe']:+.2f} "
          f"(Δ {m_test_v['sharpe']-m_test_b['sharpe']:+.2f}) | ret {m_test_v['ret_pct']:+.1f}% | "
          f"DD {m_test_v['max_dd_pct']:+.1f}%")
    print(f"Rebalances: {res_v['n_rebalances']} | turnover total: {res_v['turnover_total']:.2f} | "
          f"fees: ${res_v['fee_total']:.2f} (base ${res_b['fee_total']:.2f})")
    # Per-asset top lines
    cols = res_v["cols"]
    total_ent = int(res_v["per_asset_entries"].sum())
    total_trig = int(res_v["per_asset_triggers"].sum())
    print(f"Stops: {total_trig} triggers / {total_ent} entries "
          f"({(total_trig/total_ent*100) if total_ent else 0:.1f}% overall)")
    # Show narrowest and widest stops
    dists = [(cols[a], (res_v["per_asset_stop_dist_sum"][a] / res_v["per_asset_entries"][a])
              if res_v["per_asset_entries"][a] > 0 else float("nan")) for a in range(len(cols))]
    dists_ok = [(s, d) for s, d in dists if np.isfinite(d)]
    dists_ok.sort(key=lambda t: t[1])
    if dists_ok:
        print(f"Stop dist: tightest {dists_ok[0][0]} {dists_ok[0][1]*100:.1f}% / "
              f"widest {dists_ok[-1][0]} {dists_ok[-1][1]*100:.1f}%")
    # Trigger rate extremes
    trigs = [(cols[a], res_v["per_asset_triggers"][a], res_v["per_asset_entries"][a])
             for a in range(len(cols)) if res_v["per_asset_entries"][a] > 0]
    trigs.sort(key=lambda t: -(t[1] / t[2]))
    if trigs:
        t0 = trigs[0]; t1 = trigs[-1]
        print(f"Trigger rate: hi {t0[0]} {t0[1]}/{t0[2]} ({t0[1]/t0[2]*100:.1f}%) / "
              f"lo {t1[0]} {t1[1]}/{t1[2]} ({t1[1]/t1[2]*100:.1f}%)")
    oos_delta = m_test_v["sharpe"] - m_test_b["sharpe"]
    dd_delta = m_full_v["max_dd_pct"] - m_full_b["max_dd_pct"]
    verdict = "ADOPT" if (oos_delta > 0 and dd_delta >= 0) else "REJECT"
    print(f"Verdict: {verdict} (OOS Sharpe Δ {oos_delta:+.2f}, full DD Δ {dd_delta:+.1f}pp)")
    print("Report: backtest/results/stoploss-VOL.md")

    out = PROJECT_ROOT / "backtest" / "results" / "stoploss-VOL.md"
    write_report(out, frames, res_v, res_b, m_full_v, m_train_v, m_test_v,
                 m_full_b, m_train_b, m_test_b)
    return 0


if __name__ == "__main__":
    sys.exit(main())
