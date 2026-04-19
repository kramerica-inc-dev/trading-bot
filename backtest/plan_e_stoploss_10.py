#!/usr/bin/env python3
"""Plan E — SL-10 variant evaluation.

Evaluates whether a FIXED SYMMETRIC 10% stop-loss adds edge to the Plan E
cross-sectional-reversal strategy (lb=72h, rb=24h, SIGN=-1, k_exit=6).

Baseline: no stops.
SL-10 variant:
  - LONG leg: trigger if intra-bar low < entry * 0.90
  - SHORT leg: trigger if intra-bar high > entry * 1.10
  - Fill price: entry*0.90 (long) / entry*1.10 (short), UNLESS the bar's
    open gapped through the stop -> fill at bar open.
  - Post-stop: leg stays flat until the next 24h rebalance (no re-entry).

Writes:
  backtest/results/stoploss-SL10.md
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

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
K_EXIT = 6
SIGN = -1
FEE_RATE = 0.0006
SLIPPAGE_RATE = 0.0005
COST_PER_SIDE = FEE_RATE + SLIPPAGE_RATE
STOP_PCT = 0.10
SPLIT_DATE = pd.Timestamp("2026-01-01", tz="UTC")


def load_ohlc() -> dict[str, pd.DataFrame]:
    """Return dict {symbol: DataFrame(open/high/low/close indexed by ts)}."""
    data_dir = PROJECT_ROOT / "backtest" / "data"
    out = {}
    for sym in UNIVERSE:
        df = pd.read_csv(data_dir / f"{sym}_1H.csv")
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.sort_values("timestamp").set_index("timestamp")
        out[sym] = df[["open", "high", "low", "close"]]
    return out


def align(ohlc: dict[str, pd.DataFrame]) -> tuple:
    """Align on common index; return (ts, open, high, low, close) ndarrays."""
    close_wide = pd.DataFrame({s: ohlc[s]["close"] for s in UNIVERSE}).dropna(how="any")
    idx = close_wide.index
    opens = np.stack([ohlc[s]["open"].reindex(idx).values for s in UNIVERSE], axis=1)
    highs = np.stack([ohlc[s]["high"].reindex(idx).values for s in UNIVERSE], axis=1)
    lows = np.stack([ohlc[s]["low"].reindex(idx).values for s in UNIVERSE], axis=1)
    closes = close_wide.values
    return idx, opens, highs, lows, closes


def select_targets(signal_row: np.ndarray, prev_w: np.ndarray) -> np.ndarray:
    """Hysteresis-based target weights (same logic as plan_e_walkforward)."""
    n_assets = len(signal_row)
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
    return new_w


def run(ts, opens, highs, lows, closes, use_stop: bool) -> dict:
    """Run backtest with or without 10% stop-loss.

    When use_stop=True we track (entry_price, stopped) per asset.
    Each bar we check intra-bar breach before applying PnL:
      - long: if low < entry*0.9 -> stop this bar
              fill = entry*0.9 if open >= entry*0.9 else open (gap-through)
      - short: if high > entry*1.1 -> stop this bar
              fill = entry*1.1 if open <= entry*1.1 else open (gap-through)
    Stopped leg's PnL this bar = weight * (fill/entry_close_prev - 1).
    After stop we zero its weight and do not re-enter until next rebalance.
    """
    n_bars, n_assets = closes.shape
    prev_w = np.zeros(n_assets)
    entry_px = np.full(n_assets, np.nan)   # per-asset entry price
    stopped = np.zeros(n_assets, dtype=bool)

    equity = np.full(n_bars, INITIAL_BALANCE)
    eq = INITIAL_BALANCE
    fee_total = 0.0
    gross_pnl_total = 0.0
    turnover_total = 0.0
    n_rebalances = 0

    # Signal
    log_close = np.log(closes)
    signal = np.full_like(closes, np.nan)
    signal[LOOKBACK_H:] = (log_close[LOOKBACK_H:] - log_close[:-LOOKBACK_H]) * SIGN

    # Per-asset stop diagnostics
    trigger_count = np.zeros(n_assets, dtype=int)
    # avoided = (counterfactual_pnl_from_stop_to_next_rebalance
    #            minus realized_stopped_pnl_this_bar)
    # We record running state so we can compute after the fact.
    avoided_pnl_pct = np.zeros(n_assets)   # cumulative %-of-equity avoided
    # For counterfactual: at stop time, remember (asset, weight_pct, stop_price, stop_idx, next_rebalance_idx)
    pending_cf: list[dict] = []

    # Precompute next-rebalance index lookup
    rebalance_mask = np.array([t.hour % REBALANCE_H == 0 for t in ts])
    # For any bar i, "next rebalance" is first j>i with rebalance_mask[j].
    # We build next_rb_idx[i] = j.
    next_rb_idx = np.full(n_bars, n_bars, dtype=int)
    last = n_bars
    for i in range(n_bars - 1, -1, -1):
        next_rb_idx[i] = last
        if rebalance_mask[i]:
            last = i

    for i in range(LOOKBACK_H, n_bars):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        c_prev = closes[i - 1]

        # 1. Apply PnL this bar, checking stops first (if use_stop)
        bar_pnl = 0.0
        if not (np.isnan(c_prev).any() or np.isnan(c).any()):
            for k in range(n_assets):
                w = prev_w[k]
                if w == 0:
                    continue
                stop_hit = False
                fill = c[k]   # default: close-to-close PnL
                if use_stop and not np.isnan(entry_px[k]):
                    ep = entry_px[k]
                    if w > 0:       # long
                        trigger = ep * (1 - STOP_PCT)
                        if l[k] <= trigger:
                            stop_hit = True
                            fill = o[k] if o[k] < trigger else trigger
                    else:           # short
                        trigger = ep * (1 + STOP_PCT)
                        if h[k] >= trigger:
                            stop_hit = True
                            fill = o[k] if o[k] > trigger else trigger

                if stop_hit:
                    # PnL for this asset this bar = w * eq * (fill/c_prev - 1)
                    leg_ret = fill / c_prev[k] - 1.0
                    bar_pnl += w * eq * leg_ret
                    trigger_count[k] += 1
                    # record CF: from stop to next rebalance
                    pending_cf.append({
                        "asset": k,
                        "w": w,
                        "stop_price": fill,
                        "next_rb": next_rb_idx[i],
                    })
                    prev_w[k] = 0.0
                    stopped[k] = True
                    entry_px[k] = np.nan
                else:
                    leg_ret = c[k] / c_prev[k] - 1.0
                    bar_pnl += w * eq * leg_ret

        gross_pnl_total += bar_pnl
        eq_before = eq + bar_pnl

        # 2. Resolve any pending counterfactuals whose next_rb == i
        remaining_cf = []
        for cf in pending_cf:
            if cf["next_rb"] == i:
                # counterfactual fill at this bar's close (the rebalance close)
                # would-have-been pnl from stop_price to close:
                cf_ret = c[cf["asset"]] / cf["stop_price"] - 1.0
                realized_if_no_stop = cf["w"] * cf_ret
                # "avoided loss" = realized_if_no_stop (negative means we
                # avoided a loss of that magnitude; positive means we missed
                # upside). We want negative-of-counterfactual for "loss avoided".
                # Convention: avoided_pnl_pct accumulates the loss we avoided
                # (positive = good). So add -(realized_if_no_stop).
                avoided_pnl_pct[cf["asset"]] += -realized_if_no_stop
            else:
                remaining_cf.append(cf)
        pending_cf = remaining_cf

        # 3. Rebalance check
        if rebalance_mask[i] and not np.isnan(signal[i]).any():
            new_w = select_targets(signal[i], prev_w)
            turnover = float(np.sum(np.abs(new_w - prev_w)))
            if turnover > 1e-9:
                fee = eq_before * turnover * COST_PER_SIDE
                fee_total += fee
                turnover_total += turnover
                n_rebalances += 1
                eq = eq_before - fee
                # Update entry_px for assets that changed position
                for k in range(n_assets):
                    if new_w[k] != prev_w[k]:
                        if new_w[k] != 0:
                            entry_px[k] = c[k]
                        else:
                            entry_px[k] = np.nan
                prev_w = new_w
                stopped[:] = False   # reset stopped flags at rebalance
            else:
                eq = eq_before
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
        "trigger_count": trigger_count,
        "avoided_pnl_pct": avoided_pnl_pct,
        "start_idx": LOOKBACK_H,
    }


def slice_metrics(ts, eq, start_ts, end_ts):
    ts_arr = np.asarray(ts)
    mask = (ts_arr >= start_ts) & (ts_arr < end_ts)
    idx = np.where(mask)[0]
    if len(idx) < 2:
        return {"n_bars": 0, "sharpe": 0.0, "max_dd_pct": 0.0, "ret_pct": 0.0}
    es = eq[idx]
    rr = np.diff(es) / es[:-1]
    rr = rr[np.isfinite(rr)]
    ann = 24 * 365
    mu = rr.mean() if len(rr) else 0.0
    sd = rr.std(ddof=1) if len(rr) > 1 else 0.0
    sharpe = (mu * ann) / (sd * np.sqrt(ann)) if sd > 0 else 0.0
    peak = np.maximum.accumulate(es)
    dd = (es - peak) / peak
    return {
        "n_bars": len(idx),
        "sharpe": float(sharpe),
        "max_dd_pct": float(dd.min() * 100) if len(dd) else 0.0,
        "ret_pct": float((es[-1] / es[0] - 1) * 100),
    }


def full_metrics(res) -> dict:
    eq = res["equity"]
    start = res["start_idx"]
    rr = np.diff(eq[start:]) / eq[start:-1]
    rr = rr[np.isfinite(rr)]
    ann = 24 * 365
    mu = rr.mean() if len(rr) else 0.0
    sd = rr.std(ddof=1) if len(rr) > 1 else 0.0
    sharpe = (mu * ann) / (sd * np.sqrt(ann)) if sd > 0 else 0.0
    peak = np.maximum.accumulate(eq[start:])
    dd = (eq[start:] - peak) / peak

    # CAGR from start index to end
    years = (res["ts"][-1] - res["ts"][start]).total_seconds() / (365.25 * 86400)
    total_ret = eq[-1] / eq[start]
    cagr = (total_ret ** (1 / years) - 1) if years > 0 and total_ret > 0 else 0.0

    return {
        "final_eq": float(eq[-1]),
        "net_pnl": float(eq[-1] - INITIAL_BALANCE),
        "ret_pct": float((eq[-1] / INITIAL_BALANCE - 1) * 100),
        "cagr_pct": float(cagr * 100),
        "sharpe": float(sharpe),
        "max_dd_pct": float(dd.min() * 100) if len(dd) else 0.0,
        "fee_total": float(res["fee_total"]),
        "turnover_total": float(res["turnover_total"]),
        "n_rebalances": int(res["n_rebalances"]),
    }


def main() -> int:
    print("Loading OHLC for 10 assets...")
    ohlc = load_ohlc()
    ts, opens, highs, lows, closes = align(ohlc)
    print(f"Aligned bars: {len(ts)}  range: {ts[0]} -> {ts[-1]}")

    print("Running baseline (no stops)...")
    res_base = run(ts, opens, highs, lows, closes, use_stop=False)
    print("Running SL-10 variant...")
    res_sl = run(ts, opens, highs, lows, closes, use_stop=True)

    m_base = full_metrics(res_base)
    m_sl = full_metrics(res_sl)

    # Walk-forward slices
    start_ts = ts[LOOKBACK_H]
    end_ts = ts[-1] + pd.Timedelta(hours=1)
    is_base = slice_metrics(ts, res_base["equity"], start_ts, SPLIT_DATE)
    oos_base = slice_metrics(ts, res_base["equity"], SPLIT_DATE, end_ts)
    is_sl = slice_metrics(ts, res_sl["equity"], start_ts, SPLIT_DATE)
    oos_sl = slice_metrics(ts, res_sl["equity"], SPLIT_DATE, end_ts)

    # Per-asset
    trig = res_sl["trigger_count"]
    avoided = res_sl["avoided_pnl_pct"]   # fraction-of-equity avoided
    n_rb = res_sl["n_rebalances"]
    trig_rate = trig / max(n_rb, 1)
    # Convert avoided (fractional) -> dollars using INITIAL_BALANCE as rough scale
    avoided_usd = avoided * INITIAL_BALANCE
    per_asset = []
    for k, sym in enumerate(UNIVERSE):
        avg_avoid = (avoided[k] / trig[k] * 100) if trig[k] > 0 else 0.0
        per_asset.append({
            "sym": sym,
            "triggers": int(trig[k]),
            "trig_rate": float(trig_rate[k]),
            "avoided_pct_equity": float(avoided[k] * 100),
            "avoided_usd": float(avoided_usd[k]),
            "avg_loss_avoided_pct": float(avg_avoid),
        })

    # Verdict logic
    d_sharpe_full = m_sl["sharpe"] - m_base["sharpe"]
    d_dd_full = m_sl["max_dd_pct"] - m_base["max_dd_pct"]   # less negative = better
    d_sharpe_oos = oos_sl["sharpe"] - oos_base["sharpe"]
    d_dd_oos = oos_sl["max_dd_pct"] - oos_base["max_dd_pct"]

    if d_sharpe_full > 0.1 and d_sharpe_oos > 0.05 and d_dd_full >= -0.5:
        verdict = "WINNER"
    elif d_sharpe_full < -0.1 or d_sharpe_oos < -0.1:
        verdict = "REJECTED"
    else:
        verdict = "MARGINAL"

    _write_report(m_base, m_sl, is_base, oos_base, is_sl, oos_sl,
                  per_asset, verdict, d_sharpe_full, d_dd_full,
                  d_sharpe_oos, d_dd_oos)

    # 10-line stdout summary
    print("")
    print("=== SL-10 variant summary ===")
    print(f"Baseline: Sharpe {m_base['sharpe']:+.2f}  DD {m_base['max_dd_pct']:+.1f}%  CAGR {m_base['cagr_pct']:+.1f}%  Fees ${m_base['fee_total']:.0f}")
    print(f"SL-10:    Sharpe {m_sl['sharpe']:+.2f}  DD {m_sl['max_dd_pct']:+.1f}%  CAGR {m_sl['cagr_pct']:+.1f}%  Fees ${m_sl['fee_total']:.0f}")
    print(f"Delta:    dSharpe {d_sharpe_full:+.2f}  dDD {d_dd_full:+.1f}pp")
    print(f"OOS base: Sharpe {oos_base['sharpe']:+.2f}  DD {oos_base['max_dd_pct']:+.1f}%")
    print(f"OOS SL10: Sharpe {oos_sl['sharpe']:+.2f}  DD {oos_sl['max_dd_pct']:+.1f}%")
    print(f"OOS delta: dSharpe {d_sharpe_oos:+.2f}  dDD {d_dd_oos:+.1f}pp")
    total_trig = int(trig.sum())
    print(f"Total stop triggers: {total_trig} across {n_rb} rebalances")
    print(f"Verdict: {verdict}")
    return 0 if verdict != "REJECTED" else 1


def _write_report(m_base, m_sl, is_base, oos_base, is_sl, oos_sl,
                  per_asset, verdict, d_sharpe_full, d_dd_full,
                  d_sharpe_oos, d_dd_oos):
    path = PROJECT_ROOT / "backtest" / "results" / "stoploss-SL10.md"
    L = []
    L.append("# Plan E — SL-10 fixed symmetric 10% stop-loss evaluation")
    L.append("")
    L.append(f"**Generated:** {pd.Timestamp.now(tz='UTC').isoformat()}")
    L.append(f"**Config:** lb=72h, rb=24h, SIGN=-1 (REV), k_exit={K_EXIT}, "
             f"10%/leg, fee={FEE_RATE}, slip={SLIPPAGE_RATE}, "
             f"init=${INITIAL_BALANCE:.0f}")
    L.append(f"**Stop:** symmetric {STOP_PCT*100:.0f}%, 1h OHLC granularity, "
             f"gap-through handled at bar open, no re-entry until next rebalance")
    L.append("")
    L.append("## Full-period delta")
    L.append("")
    L.append("| Metric | Baseline | SL-10 | Delta |")
    L.append("|--------|----------|-------|-------|")
    L.append(f"| Final equity | ${m_base['final_eq']:,.2f} | ${m_sl['final_eq']:,.2f} | ${m_sl['final_eq']-m_base['final_eq']:+,.2f} |")
    L.append(f"| Return %     | {m_base['ret_pct']:+.1f}% | {m_sl['ret_pct']:+.1f}% | {m_sl['ret_pct']-m_base['ret_pct']:+.1f}pp |")
    L.append(f"| CAGR         | {m_base['cagr_pct']:+.1f}% | {m_sl['cagr_pct']:+.1f}% | {m_sl['cagr_pct']-m_base['cagr_pct']:+.1f}pp |")
    L.append(f"| Sharpe       | {m_base['sharpe']:+.2f} | {m_sl['sharpe']:+.2f} | {d_sharpe_full:+.2f} |")
    L.append(f"| Max DD       | {m_base['max_dd_pct']:+.1f}% | {m_sl['max_dd_pct']:+.1f}% | {d_dd_full:+.1f}pp |")
    L.append(f"| Turnover (cum) | {m_base['turnover_total']:.2f} | {m_sl['turnover_total']:.2f} | {m_sl['turnover_total']-m_base['turnover_total']:+.2f} |")
    L.append(f"| Fees (cum)   | ${m_base['fee_total']:.2f} | ${m_sl['fee_total']:.2f} | ${m_sl['fee_total']-m_base['fee_total']:+.2f} |")
    L.append(f"| Rebalances   | {m_base['n_rebalances']} | {m_sl['n_rebalances']} | — |")
    L.append("")
    L.append("## Walk-forward (split 2026-01-01)")
    L.append("")
    L.append("| Slice | Baseline Sharpe | SL-10 Sharpe | dSharpe | Baseline DD | SL-10 DD | dDD |")
    L.append("|-------|-----------------|---------------|---------|-------------|----------|-----|")
    L.append(f"| IS    | {is_base['sharpe']:+.2f} | {is_sl['sharpe']:+.2f} | {is_sl['sharpe']-is_base['sharpe']:+.2f} | {is_base['max_dd_pct']:+.1f}% | {is_sl['max_dd_pct']:+.1f}% | {is_sl['max_dd_pct']-is_base['max_dd_pct']:+.1f}pp |")
    L.append(f"| OOS   | {oos_base['sharpe']:+.2f} | {oos_sl['sharpe']:+.2f} | {d_sharpe_oos:+.2f} | {oos_base['max_dd_pct']:+.1f}% | {oos_sl['max_dd_pct']:+.1f}% | {d_dd_oos:+.1f}pp |")
    L.append("")
    L.append("## Per-asset SL-10 activity")
    L.append("")
    L.append("| Asset | Triggers | Trig rate (/rb) | Cum avoided (%eq) | Avg loss avoided/trig |")
    L.append("|-------|----------|-----------------|-------------------|------------------------|")
    for pa in per_asset:
        L.append(f"| {pa['sym']} | {pa['triggers']} | {pa['trig_rate']:.3f} | "
                 f"{pa['avoided_pct_equity']:+.2f}% | {pa['avg_loss_avoided_pct']:+.2f}% |")
    L.append("")
    # Top-5 by |avoided|
    top5 = sorted(per_asset, key=lambda p: abs(p["avoided_pct_equity"]), reverse=True)[:5]
    L.append("## Top-5 asset insights (by |cum avoided|)")
    L.append("")
    for pa in top5:
        tag = "helpful" if pa["avoided_pct_equity"] > 0 else "hurtful"
        L.append(f"- **{pa['sym']}**: {pa['triggers']} triggers, "
                 f"cum avoided {pa['avoided_pct_equity']:+.2f}% of equity "
                 f"({tag}); avg per-trigger {pa['avg_loss_avoided_pct']:+.2f}%")
    L.append("")
    L.append("## Verdict")
    L.append("")
    L.append(f"**{verdict}**")
    L.append("")
    if verdict == "WINNER":
        L.append("SL-10 improves Sharpe materially both full-period and OOS "
                 "without DD regression. Consider promoting to live runner "
                 "after paper validation.")
    elif verdict == "REJECTED":
        L.append("SL-10 degrades the core signal. The 10% stop cuts reversals "
                 "too early — leg often continues to mean-revert after the "
                 "stop trigger, so we realize loss then miss recovery. Keep "
                 "baseline (no stops).")
    else:
        L.append("SL-10 is approximately neutral. Benefits (tail control) "
                 "roughly cancel missed reversal recoveries. Not worth the "
                 "implementation complexity unless capped-loss is a hard "
                 "risk constraint.")
    L.append("")
    path.write_text("\n".join(L))
    print(f"Report written to {path}")


if __name__ == "__main__":
    sys.exit(main())
