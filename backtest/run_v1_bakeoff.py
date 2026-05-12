#!/usr/bin/env python3
"""v1 trend-rule bake-off + random-entry null gate — milestone M3 / spec §7.1–§7.3.

Reproduces, on the daily BTC series:
  1. the **bake-off table** — each of the three §3 candidate rules at sensible
     default params, with the vol-target overlay and a "plain" (no overlay)
     variant, scored on full-series return / Calmar / max-DD / time-in-market;
  2. the **random-entry null gate** (§7.1, binding per DECISIONS.md) — for each
     rule, build a ~1000-rep random in/out null matched to that rule's realized
     time-in-market AND mean holding length AND in-market exposure profile, then
     place the rule's full-series total-return AND Calmar in the null
     distribution.  A rule inside the 5–95 band on Calmar has no demonstrated
     edge → it is dropped, not tuned;
  3. the §7.2 coarse grid + walk-forward and §7.3 single-holdout comparison
     vs B1/B2 — run only for rules that survive the null gate (`--full`).

Saves plots to `backtest/results/v1_*.png`.

Usage:
    python -m backtest.run_v1_bakeoff                 # bake-off + null gate
    python -m backtest.run_v1_bakeoff --reps 2000     # more null reps
    python -m backtest.run_v1_bakeoff --full          # + grid/walk-forward/holdout
    python -m backtest.run_v1_bakeoff --no-funding
"""

from __future__ import annotations

import argparse
import itertools
import os
import sys
from dataclasses import dataclass
from typing import List, Optional

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "backtest"))

from daily_backtester import (DailyBacktester, DailyBacktestConfig,  # noqa: E402
                              DEFAULT_FUNDING_PATH, load_daily_btc)
from daily_strategies import BuyAndHold, TrailingStopBH  # noqa: E402
from random_entry_null import (random_entry_null, percentile_of)  # noqa: E402
from v1_strategies import make_candidate, VolTarget  # noqa: E402

RESULTS_DIR = os.path.join(ROOT, "backtest", "results")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _exposure_runs(exposures: List[float], eps: float = 1e-9):
    """Lengths of consecutive in-market (exposure > eps) runs."""
    runs, cur = [], 0
    for e in exposures:
        if e > eps:
            cur += 1
        elif cur:
            runs.append(cur); cur = 0
    if cur:
        runs.append(cur)
    return runs


@dataclass
class RuleStats:
    name: str
    rule: str
    params: dict
    plain: bool
    ret: float
    calmar: float
    max_dd: float
    tim: float            # time-in-market fraction
    mean_hold: float      # mean in-run length (days)
    avg_exposure: float   # mean exposure while in-market
    n_rebalances: int
    fees: float
    funding: float
    result: object        # the BacktestResult

    # filled in by the null gate
    null_ret_pct: Optional[float] = None
    null_calmar_pct: Optional[float] = None
    null_calmar_p5: Optional[float] = None
    null_calmar_p95: Optional[float] = None
    survived: Optional[bool] = None


def run_rule(df, cfg, name, rule, *, plain=False, **params) -> RuleStats:
    strat = make_candidate(rule, plain=plain, **params)
    res = DailyBacktester(strat, cfg).run(df)
    exps = list(res.daily_exposure)
    runs = _exposure_runs(exps)
    mean_hold = float(np.mean(runs)) if runs else 0.0
    in_exps = [e for e in exps if e > 1e-9]
    avg_in_exp = float(np.mean(in_exps)) if in_exps else 0.0
    return RuleStats(name=name, rule=rule, params=params, plain=plain,
                     ret=res.total_roi, calmar=res.calmar_ratio,
                     max_dd=res.max_drawdown_pct, tim=res.time_in_market_frac,
                     mean_hold=mean_hold, avg_exposure=avg_in_exp,
                     n_rebalances=res.n_rebalances, fees=res.total_fees,
                     funding=res.total_funding, result=res)


def null_gate(df, cfg, rs: RuleStats, reps: int, seed: int = 12345) -> RuleStats:
    """Place a rule's full-series return AND Calmar in a matched random-entry null."""
    in_exp = rs.avg_exposure if rs.avg_exposure > 0 else 1.0
    null = random_entry_null(df, time_in_market=max(rs.tim, 1e-3),
                             mean_hold_days=max(rs.mean_hold, 1.0), reps=reps,
                             config=cfg, in_exposure=in_exp, seed=seed)
    rs.null_ret_pct = percentile_of(rs.ret, null.total_return_pct)
    rs.null_calmar_pct = percentile_of(rs.calmar, null.calmar)
    rs.null_calmar_p5 = float(np.percentile(null.calmar, 5))
    rs.null_calmar_p95 = float(np.percentile(null.calmar, 95))
    rs.survived = rs.null_calmar_pct > 95.0
    return rs


# ---------------------------------------------------------------------------
# §7.2 walk-forward grid + §7.3 holdout (only for survivors)
# ---------------------------------------------------------------------------

GRIDS = {
    "trailing": [{"trail_pct": x, "breakout_days": n}
                 for x in (0.10, 0.125, 0.15, 0.20) for n in (20, 35, 50)],
    "ma": [{"ma_days": m, "ema": e}
           for m in (50, 100, 150, 200) for e in (False, True)],
    "donchian": [{"entry_days": n, "exit_days": k}
                 for n in (30, 50, 75, 100) for k in (10, 20, 30)],
}


def _eval(df, cfg, rule, params, *, plain=False):
    strat = make_candidate(rule, plain=plain, **params)
    return DailyBacktester(strat, cfg).run(df)


def walk_forward(df, cfg, rule, *, n_splits=3, train_frac=0.70):
    """Expanding/rolling walk-forward over a coarse grid; optimize Calmar on train.

    Returns (rows, picks): `rows` = per-split (best train config, its train &
    test Calmar/return), `picks` = the configs chosen, plus the spread of Calmar
    across the whole grid on the full series (flat spread ⇒ params don't matter).
    """
    n = len(df)
    grid = GRIDS[rule]
    # Build n_splits contiguous windows; in each, first train_frac is train.
    rows = []
    if n_splits < 1:
        n_splits = 1
    seg = n // n_splits
    for i in range(n_splits):
        lo = i * seg
        hi = n if i == n_splits - 1 else (i + 1) * seg
        sub = df.iloc[lo:hi].reset_index(drop=True)
        cut = int(len(sub) * train_frac)
        tr, te = sub.iloc[:cut].reset_index(drop=True), sub.iloc[cut:].reset_index(drop=True)
        if len(tr) < 60 or len(te) < 30:
            continue
        best, best_cal = None, -1e9
        for p in grid:
            r = _eval(tr, cfg, rule, p)
            if r.calmar_ratio > best_cal:
                best_cal, best = r.calmar_ratio, p
        rte = _eval(te, cfg, rule, best)
        rows.append({"split": i, "n_train": len(tr), "n_test": len(te),
                     "config": best, "train_calmar": best_cal,
                     "train_ret": _eval(tr, cfg, rule, best).total_roi,
                     "test_calmar": rte.calmar_ratio, "test_ret": rte.total_roi})
    # full-series grid spread
    full_cals = [_eval(df, cfg, rule, p).calmar_ratio for p in grid]
    spread = {"min": float(np.min(full_cals)), "max": float(np.max(full_cals)),
              "mean": float(np.mean(full_cals)), "std": float(np.std(full_cals)),
              "n": len(grid)}
    return rows, spread


def deflated_sharpe(sharpe: float, n_trials: int, n_obs: int) -> float:
    """A rough deflated-Sharpe haircut: subtract the expected max of n_trials
    independent N(0, 1/n_obs) Sharpe estimates.  Not the full Bailey/Lopez de
    Prado statistic — just a sanity haircut for the grid-search multiplicity."""
    if n_trials < 2 or n_obs < 2:
        return sharpe
    # E[max of n iid standard normals] ≈ sqrt(2 ln n) - (ln ln n + ln 4π)/(2 sqrt(2 ln n))
    ln_n = np.log(n_trials)
    em = np.sqrt(2 * ln_n) - (np.log(ln_n) + np.log(4 * np.pi)) / (2 * np.sqrt(2 * ln_n))
    return float(sharpe - em / np.sqrt(n_obs))


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------

def _plot_bakeoff(df, cfg, candidates: List[RuleStats]):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:  # pragma: no cover
        print(f"(plots skipped: {e})")
        return
    os.makedirs(RESULTS_DIR, exist_ok=True)
    # equity curves
    fig, ax = plt.subplots(figsize=(12, 6))
    b1 = DailyBacktester(BuyAndHold(1.0), cfg).run(df)
    ts = b1.timestamps
    ax.plot(ts, b1.equity_curve, label="B1 buy-and-hold", color="black", lw=1.4)
    b2 = DailyBacktester(TrailingStopBH(0.10, 20, reenter=True), cfg).run(df)
    ax.plot(b2.timestamps, b2.equity_curve, label="B2 trailing-stop-BH (re-enter)",
            color="gray", ls="--", lw=1.2)
    colours = ["tab:blue", "tab:green", "tab:orange", "tab:red", "tab:purple"]
    for c, rs in zip(colours, [x for x in candidates if not x.plain]):
        ax.plot(rs.result.timestamps, rs.result.equity_curve,
                label=f"{rs.name} (Cal {rs.calmar:+.2f})", lw=1.2, color=c)
    ax.set_yscale("log"); ax.legend(fontsize=8); ax.set_title("v1 bake-off — equity curves (log)")
    plt.tight_layout(); plt.savefig(os.path.join(RESULTS_DIR, "v1_bakeoff_equity.png"), dpi=110)
    plt.close(fig)
    # null gate panel — Calmar of each rule vs its matched null band
    fig, ax = plt.subplots(figsize=(10, 5))
    names = [rs.name for rs in candidates if not rs.plain and rs.survived is not None]
    cals = [rs.calmar for rs in candidates if not rs.plain and rs.survived is not None]
    p5 = [rs.null_calmar_p5 for rs in candidates if not rs.plain and rs.survived is not None]
    p95 = [rs.null_calmar_p95 for rs in candidates if not rs.plain and rs.survived is not None]
    x = np.arange(len(names))
    ax.bar(x, np.array(p95) - np.array(p5), bottom=p5, color="lightsteelblue",
           label="random-entry null 5–95% Calmar band", width=0.5)
    ax.scatter(x, cals, color="red", zorder=5, label="rule's Calmar")
    ax.set_xticks(x); ax.set_xticklabels(names, rotation=15, fontsize=8)
    ax.legend(fontsize=8); ax.set_title("§7.1 random-entry null gate — Calmar")
    ax.axhline(0, color="k", lw=0.5)
    plt.tight_layout(); plt.savefig(os.path.join(RESULTS_DIR, "v1_null_gate.png"), dpi=110)
    plt.close(fig)
    print(f"plots -> {os.path.join(RESULTS_DIR, 'v1_bakeoff_equity.png')}, "
          f"{os.path.join(RESULTS_DIR, 'v1_null_gate.png')}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="v1 bake-off + random-entry null gate (M3)")
    p.add_argument("--csv", default=os.path.join(ROOT, "backtest", "data", "BTC-USDT_1d.csv"))
    p.add_argument("--balance", type=float, default=5000.0)
    p.add_argument("--reps", type=int, default=1000, help="random-entry null reps")
    p.add_argument("--maker-fraction", type=float, default=0.80)
    p.add_argument("--sigma-target", type=float, default=0.20)
    p.add_argument("--vol-window", type=int, default=30)
    p.add_argument("--no-funding", action="store_true")
    p.add_argument("--full", action="store_true",
                   help="also run §7.2 walk-forward + §7.3 holdout for survivors")
    p.add_argument("--seed", type=int, default=12345)
    args = p.parse_args()

    if not os.path.exists(args.csv):
        print(f"Daily CSV not found: {args.csv}\nBuild it: python -m backtest.build_daily_csv")
        return 1
    df = load_daily_btc(args.csv)
    cfg = DailyBacktestConfig(initial_balance=args.balance, maker_fraction=args.maker_fraction,
                              funding_series_path=(None if args.no_funding else DEFAULT_FUNDING_PATH))
    span_days = (df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]).days
    print(f"Daily BTC: {len(df)} bars  {df['timestamp'].iloc[0].date()} → "
          f"{df['timestamp'].iloc[-1].date()}  (~{span_days/365.25:.2f}y)")
    print(f"Cost: blended fee {cfg.blended_fee*100:.4f}%/side ({cfg.maker_fraction:.0%} maker), "
          f"slip {cfg.slippage_pct:.2f}%/fill, no-trade band {cfg.no_trade_band_pct:.0f}%, "
          f"funding {'OFF' if args.no_funding else 'ON'};  σ_target {args.sigma_target:.0%}, "
          f"vol window {args.vol_window}d, L_max 1.0")
    print()

    # --- benchmarks ---
    b1 = DailyBacktester(BuyAndHold(1.0), cfg).run(df)
    b2r = DailyBacktester(TrailingStopBH(0.10, 20, reenter=True), cfg).run(df)
    b2o = DailyBacktester(TrailingStopBH(0.10, 20, reenter=False), cfg).run(df)
    print("BENCHMARKS")
    fmt = "  {:36s} ret {:+9.2f}%  Calmar {:+6.2f}  maxDD {:6.2f}%  TiM {:5.1%}"
    print(fmt.format("B1  buy-and-hold", b1.total_roi, b1.calmar_ratio, b1.max_drawdown_pct, b1.time_in_market_frac))
    print(fmt.format("B2  trailing-stop-BH (10%/20d re-enter)", b2r.total_roi, b2r.calmar_ratio, b2r.max_drawdown_pct, b2r.time_in_market_frac))
    print(fmt.format("B2' trailing-stop-BH (10% one-shot)", b2o.total_roi, b2o.calmar_ratio, b2o.max_drawdown_pct, b2o.time_in_market_frac))
    print()

    # --- bake-off: three rules × {vol-target, plain} at default params ---
    defaults = [
        ("(a) trailing-stop 10%/20d",       "trailing", {"trail_pct": 0.10, "breakout_days": 20}),
        ("(b) MA-filter SMA100",            "ma",       {"ma_days": 100}),
        ("(b) MA-filter SMA200",            "ma",       {"ma_days": 200}),
        ("(c) Donchian 50/20",              "donchian", {"entry_days": 50, "exit_days": 20}),
    ]
    candidates: List[RuleStats] = []
    for name, rule, params in defaults:
        for plain in (False, True):
            label = name + ("  [plain]" if plain else "  [vol-tgt]")
            rs = run_rule(df, cfg, label, rule, plain=plain,
                          sigma_target=args.sigma_target, vol_window=args.vol_window, **params)
            candidates.append(rs)

    print("BAKE-OFF (full series)")
    print("  " + "-" * 116)
    hdr = "  {:38s} {:>9s} {:>8s} {:>8s} {:>7s} {:>9s} {:>8s} {:>7s} {:>9s}"
    print(hdr.format("candidate", "ret%", "Calmar", "maxDD%", "TiM", "meanHold", "avgExp", "rebal", "fees$"))
    print("  " + "-" * 116)
    rowf = "  {:38s} {:+9.2f} {:+8.2f} {:8.2f} {:7.1%} {:8.1f}d {:8.2f} {:7d} {:9.2f}"
    for rs in candidates:
        print(rowf.format(rs.name, rs.ret, rs.calmar, rs.max_dd, rs.tim,
                          rs.mean_hold, rs.avg_exposure, rs.n_rebalances, rs.fees))
    print("  " + "-" * 116)
    print()

    # --- §7.1 random-entry null gate (vol-target variants only — that's the v1 design) ---
    print(f"§7.1 RANDOM-ENTRY NULL GATE  ({args.reps} reps each, matched TiM + mean-hold + in-exposure)")
    print("  " + "-" * 110)
    print("  {:38s} {:>10s} {:>14s} {:>22s} {:>14s}".format(
        "candidate (vol-tgt)", "ret %ile", "Calmar %ile", "null Calmar 5–95% band", "VERDICT"))
    print("  " + "-" * 110)
    survivors: List[RuleStats] = []
    for rs in [c for c in candidates if not c.plain]:
        null_gate(df, cfg, rs, reps=args.reps, seed=args.seed)
        band = f"[{rs.null_calmar_p5:+.2f}, {rs.null_calmar_p95:+.2f}]"
        verdict = "SURVIVES → grid" if rs.survived else "INSIDE band → DROP"
        print("  {:38s} {:9.1f}  {:13.1f}  {:>22s}  {:>14s}".format(
            rs.name, rs.null_ret_pct, rs.null_calmar_pct, band, verdict))
        if rs.survived:
            survivors.append(rs)
    print("  " + "-" * 110)
    if not survivors:
        print("\n  >>> NO v1 trend rule clears the random-entry null on Calmar OOS.")
        print("      Per DECISIONS.md (2026-05-12) these rules are NOT to be tuned.")
        print("      Recommendation: ship B2 (trailing-stop-on-BH) and/or collect a longer")
        print("      daily history before building v1.")
    else:
        print(f"\n  Survivors → §7.2 grid + walk-forward: {[s.name for s in survivors]}")
    print()

    _plot_bakeoff(df, cfg, candidates)

    # --- §7.2 / §7.3 for survivors (only with --full) ---
    if args.full and survivors:
        n = len(df)
        hold_cut = int(n * 0.78)   # reserve last ~22% as the §7.3 holdout
        df_dev, df_hold = df.iloc[:hold_cut].reset_index(drop=True), df.iloc[hold_cut:].reset_index(drop=True)
        print(f"§7.2/§7.3 — dev window {len(df_dev)} bars, holdout {len(df_hold)} bars "
              f"({df_hold['timestamp'].iloc[0].date()} → {df_hold['timestamp'].iloc[-1].date()})")
        # holdout benchmarks
        hb1 = DailyBacktester(BuyAndHold(1.0), cfg).run(df_hold)
        hb2r = DailyBacktester(TrailingStopBH(0.10, 20, reenter=True), cfg).run(df_hold)
        hb2o = DailyBacktester(TrailingStopBH(0.10, 20, reenter=False), cfg).run(df_hold)
        for rs in survivors:
            print(f"\n  --- {rs.name} ({rs.rule}) ---")
            rows, spread = walk_forward(df_dev, cfg, rs.rule, n_splits=3, train_frac=0.70)
            if not rows:
                rows, spread = walk_forward(df_dev, cfg, rs.rule, n_splits=2, train_frac=0.70)
            for r in rows:
                print(f"    split {r['split']}: train n={r['n_train']} Cal {r['train_calmar']:+.2f} "
                      f"ret {r['train_ret']:+.1f}%  →  test n={r['n_test']} Cal {r['test_calmar']:+.2f} "
                      f"ret {r['test_ret']:+.1f}%   config={r['config']}")
            print(f"    grid Calmar spread (full series, n={spread['n']}): "
                  f"[{spread['min']:+.2f}, {spread['max']:+.2f}] mean {spread['mean']:+.2f} std {spread['std']:.2f}")
            # pick the most-common winning config across splits (fallback: rule defaults)
            from collections import Counter
            if rows:
                cfg_counts = Counter(tuple(sorted(r["config"].items())) for r in rows)
                win_cfg = dict(cfg_counts.most_common(1)[0][0])
            else:
                win_cfg = rs.params
            # train Calmar (whole dev window) for the ±1σ check
            dev_res = _eval(df_dev, cfg, rs.rule, win_cfg)
            train_cals = [r["train_calmar"] for r in rows] or [dev_res.calmar_ratio]
            tr_mean, tr_std = float(np.mean(train_cals)), float(np.std(train_cals) or abs(dev_res.calmar_ratio) * 0.5 + 0.1)
            hold_res = _eval(df_hold, cfg, rs.rule, win_cfg)
            within = abs(hold_res.calmar_ratio - tr_mean) <= tr_std + 1e-9
            beats_b1 = hold_res.calmar_ratio > hb1.calmar_ratio and hold_res.max_drawdown_pct < hb1.max_drawdown_pct
            beats_b2r = hold_res.calmar_ratio > hb2r.calmar_ratio
            beats_b2o = hold_res.calmar_ratio > hb2o.calmar_ratio
            ds = deflated_sharpe(getattr(hold_res, "sharpe_ratio_bars", 0.0), len(GRIDS[rs.rule]), len(df_hold))
            print(f"    §7.3 HOLDOUT  config={win_cfg}:  Calmar {hold_res.calmar_ratio:+.2f}  "
                  f"ret {hold_res.total_roi:+.1f}%  maxDD {hold_res.max_drawdown_pct:.1f}%  "
                  f"Sharpe(bar) {getattr(hold_res,'sharpe_ratio_bars',0.0):+.2f} (deflated {ds:+.2f})")
            print(f"      within ±1σ of train Calmar ({tr_mean:+.2f}±{tr_std:.2f})? {within}")
            print(f"      holdout B1 Cal {hb1.calmar_ratio:+.2f}/DD {hb1.max_drawdown_pct:.1f}%  "
                  f"B2re Cal {hb2r.calmar_ratio:+.2f}/DD {hb2r.max_drawdown_pct:.1f}%  "
                  f"B2os Cal {hb2o.calmar_ratio:+.2f}/DD {hb2o.max_drawdown_pct:.1f}%")
            print(f"      beats B1? {beats_b1}   beats B2(re-enter)? {beats_b2r}   beats B2(one-shot)? {beats_b2o}")
            if within and beats_b1 and beats_b2r:
                print(f"      => {rs.name} {win_cfg} PASSES §7.3.")
            else:
                print(f"      => {rs.name} does NOT pass §7.3 → ship B2.")
    elif args.full:
        print("(--full: nothing to walk-forward — no rule survived the null gate.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
