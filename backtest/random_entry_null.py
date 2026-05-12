#!/usr/bin/env python3
"""Random-entry null harness — milestone M2 / spec §7.1.

The gate every future v1 trend rule must clear *before* any parameter tuning
(per `DECISIONS.md` 2026-05-12 and `docs/STRATEGY-V1-TREND-VOLTARGET.md` §7.1):
does "long-or-flat by rule X" beat a null of *random* in/out periods with the
same average time-in-market, the same average holding-period length, and the
same friction + funding model — out of sample?  If a candidate sits inside the
null band, the rule has no edge; stop, don't tune it.

This module:
  * generates N random in/out schedules whose time-in-market and mean holding
    length match the requested targets (entry days uniform; holding lengths
    geometric, so mean ≈ `mean_hold_days`; gaps geometric so the long-run
    in-fraction ≈ `time_in_market`);
  * runs each schedule through the M0 harness (`DailyBacktester`) with a chosen
    exposure profile (flat `in_exposure` while "in", 0 while "out") and the
    same cost / funding config;
  * returns the distribution of final equity / total return / Calmar;
  * `percentile_of(observed, dist)` places a candidate in the null band;
  * `run_null` also prints a summary and saves a histogram PNG to
    `backtest/results/`.

CLI:
    python -m backtest.random_entry_null --tim 0.6 --hold 30 --reps 1000
"""

import argparse
import os
import sys
from dataclasses import dataclass
from typing import List, Optional, Sequence

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from daily_backtester import (DailyBacktester, DailyBacktestConfig,  # noqa: E402
                              DEFAULT_FUNDING_PATH, load_daily_btc)
from daily_strategies import ScheduleStrategy  # noqa: E402

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


# ---------------------------------------------------------------------------
# Schedule generation
# ---------------------------------------------------------------------------

def make_random_schedule(n_bars: int, time_in_market: float,
                         mean_hold_days: float, rng: np.random.Generator) -> np.ndarray:
    """A length-`n_bars` boolean array: True == "be invested over the next day".

    Construction: alternate "in" and "out" runs.  In-runs have geometric length
    with mean `mean_hold_days`; out-runs have geometric length scaled so the
    long-run in-fraction equals `time_in_market`.  The first run is "in" or
    "out" chosen with probability `time_in_market`, so the *average* schedule
    over many draws has the right time-in-market even for short series.
    """
    tim = float(min(max(time_in_market, 1e-3), 1.0 - 1e-3))
    mh = max(float(mean_hold_days), 1.0)
    # mean out-run length so that mh / (mh + mo) == tim  ->  mo = mh*(1-tim)/tim
    mo = max(mh * (1.0 - tim) / tim, 1.0)

    def geom(mean_len: float) -> int:
        # geometric on {1,2,...} with mean `mean_len`: p = 1/mean_len
        p = 1.0 / mean_len
        return int(rng.geometric(p))

    sched = np.zeros(n_bars, dtype=bool)
    i = 0
    state_in = rng.random() < tim
    while i < n_bars:
        run_len = geom(mh if state_in else mo)
        j = min(n_bars, i + run_len)
        if state_in:
            sched[i:j] = True
        i = j
        state_in = not state_in
    return sched


# ---------------------------------------------------------------------------
# Null distribution
# ---------------------------------------------------------------------------

@dataclass
class NullResult:
    total_return_pct: np.ndarray   # one per replication
    final_equity: np.ndarray
    calmar: np.ndarray
    time_in_market: np.ndarray     # realized TiM of each schedule
    mean_hold_days: np.ndarray     # realized mean in-run length of each schedule
    reps: int
    target_tim: float
    target_hold: float

    def summary(self) -> dict:
        def pct(a):
            return {"mean": float(np.mean(a)), "median": float(np.median(a)),
                    "p5": float(np.percentile(a, 5)), "p25": float(np.percentile(a, 25)),
                    "p75": float(np.percentile(a, 75)), "p95": float(np.percentile(a, 95)),
                    "std": float(np.std(a))}
        return {
            "reps": self.reps,
            "target_time_in_market": self.target_tim,
            "target_mean_hold_days": self.target_hold,
            "realized_time_in_market_mean": float(np.mean(self.time_in_market)),
            "realized_mean_hold_days_mean": float(np.mean(self.mean_hold_days)),
            "total_return_pct": pct(self.total_return_pct),
            "calmar": pct(self.calmar),
            "final_equity": pct(self.final_equity),
        }


def _realized_mean_hold(sched: np.ndarray) -> float:
    """Mean length of the True-runs in a boolean schedule (0 if no True run)."""
    runs = []
    cur = 0
    for v in sched:
        if v:
            cur += 1
        elif cur:
            runs.append(cur); cur = 0
    if cur:
        runs.append(cur)
    return float(np.mean(runs)) if runs else 0.0


def random_entry_null(daily_df: pd.DataFrame, time_in_market: float,
                      mean_hold_days: float, reps: int = 1000,
                      config: Optional[DailyBacktestConfig] = None,
                      in_exposure: float = 1.0, seed: int = 12345) -> NullResult:
    """Run `reps` random in/out schedules through the daily harness."""
    cfg = config or DailyBacktestConfig()
    df = daily_df.reset_index(drop=True)
    n = len(df)
    rng = np.random.default_rng(seed)

    rets = np.empty(reps); fins = np.empty(reps); cals = np.empty(reps)
    tims = np.empty(reps); mhs = np.empty(reps)
    for k in range(reps):
        sched = make_random_schedule(n, time_in_market, mean_hold_days, rng)
        strat = ScheduleStrategy(sched, in_exposure=in_exposure)
        res = DailyBacktester(strat, cfg).run(df)
        rets[k] = res.total_roi
        fins[k] = res.equity_curve[-1]
        cals[k] = res.calmar_ratio
        tims[k] = float(np.mean(sched))
        mhs[k] = _realized_mean_hold(sched)
    return NullResult(rets, fins, cals, tims, mhs, reps,
                      float(time_in_market), float(mean_hold_days))


def percentile_of(observed_value: float, null_distribution: Sequence[float]) -> float:
    """Percentile (0–100) of `observed_value` within `null_distribution`.

    Fraction of null samples strictly below `observed_value`, scaled to 0–100.
    50 ≈ median of the null; >95 ⇒ above the 95th-pct null band (a real signal
    is expected to land there); 5–95 ⇒ inside the band (no demonstrated edge).
    """
    arr = np.asarray(list(null_distribution), dtype=float)
    if arr.size == 0:
        return float("nan")
    return float(np.mean(arr < observed_value) * 100.0)


# ---------------------------------------------------------------------------
# CLI / convenience runner with a histogram
# ---------------------------------------------------------------------------

def run_null(daily_df: pd.DataFrame, time_in_market: float, mean_hold_days: float,
             reps: int = 1000, config: Optional[DailyBacktestConfig] = None,
             in_exposure: float = 1.0, seed: int = 12345, label: str = "btc_daily",
             observed_return_pct: Optional[float] = None,
             save_png: bool = True) -> NullResult:
    res = random_entry_null(daily_df, time_in_market, mean_hold_days, reps=reps,
                            config=config, in_exposure=in_exposure, seed=seed)
    s = res.summary()
    print("=" * 78)
    print(f"RANDOM-ENTRY NULL  ({reps} reps)  —  target TiM {time_in_market:.0%}, "
          f"mean hold {mean_hold_days:.0f}d, in-exposure {in_exposure:.2f}")
    print(f"  realized TiM mean   : {s['realized_time_in_market_mean']:.1%} "
          f"(target {time_in_market:.1%})")
    print(f"  realized hold mean  : {s['realized_mean_hold_days_mean']:.1f}d "
          f"(target {mean_hold_days:.1f}d)")
    r = s["total_return_pct"]; c = s["calmar"]
    print(f"  total return %      : mean {r['mean']:+.2f}  median {r['median']:+.2f}  "
          f"p5 {r['p5']:+.2f}  p95 {r['p95']:+.2f}  std {r['std']:.2f}")
    print(f"  Calmar              : mean {c['mean']:+.2f}  median {c['median']:+.2f}  "
          f"p5 {c['p5']:+.2f}  p95 {c['p95']:+.2f}")
    if observed_return_pct is not None:
        p = percentile_of(observed_return_pct, res.total_return_pct)
        verdict = "INSIDE the null band — no demonstrated edge" if 5 <= p <= 95 \
            else ("ABOVE the 95th-pct band — clears the §7.1 gate" if p > 95
                  else "BELOW the 5th-pct band — actively worse than random")
        print(f"  observed return     : {observed_return_pct:+.2f}%  "
              f"-> {p:.1f}th percentile of the null  [{verdict}]")
    print("=" * 78)

    if save_png:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            os.makedirs(RESULTS_DIR, exist_ok=True)
            fig, ax = plt.subplots(figsize=(10, 5))
            ax.hist(res.total_return_pct, bins=40, color="steelblue",
                    edgecolor="k", alpha=0.8,
                    label=f"random in/out (TiM {time_in_market:.0%}, hold {mean_hold_days:.0f}d, n={reps})")
            ax.axvline(np.percentile(res.total_return_pct, 5), color="orange", ls="--", label="5th pct")
            ax.axvline(np.percentile(res.total_return_pct, 95), color="orange", ls="--", label="95th pct")
            ax.axvline(np.median(res.total_return_pct), color="navy", ls=":", label="median")
            if observed_return_pct is not None:
                ax.axvline(observed_return_pct, color="red", lw=2,
                           label=f"observed ({observed_return_pct:+.1f}%)")
            ax.set_title(f"Random-entry null — total return distribution ({label})")
            ax.set_xlabel("final total return %"); ax.legend(fontsize=8)
            plt.tight_layout()
            out = os.path.join(RESULTS_DIR, f"random_entry_null_{label}.png")
            plt.savefig(out, dpi=110)
            plt.close(fig)
            print(f"histogram -> {out}")
        except Exception as e:  # pragma: no cover
            print(f"(histogram skipped: {e})")
    return res


def main() -> int:
    p = argparse.ArgumentParser(description="Random-entry null harness (§7.1)")
    p.add_argument("--csv", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                 "data", "BTC-USDT_1d.csv"))
    p.add_argument("--tim", type=float, default=0.6, help="target time-in-market fraction")
    p.add_argument("--hold", type=float, default=30.0, help="target mean holding-period (days)")
    p.add_argument("--reps", type=int, default=1000)
    p.add_argument("--in-exposure", type=float, default=1.0)
    p.add_argument("--seed", type=int, default=12345)
    p.add_argument("--maker-fraction", type=float, default=0.80)
    p.add_argument("--no-funding", action="store_true")
    p.add_argument("--observed", type=float, default=None,
                   help="a candidate's observed total return %% to locate in the null")
    args = p.parse_args()

    if not os.path.exists(args.csv):
        print(f"Daily CSV not found: {args.csv}\nBuild it: python -m backtest.build_daily_csv")
        return 1
    df = load_daily_btc(args.csv)
    cfg = DailyBacktestConfig(initial_balance=5000.0, maker_fraction=args.maker_fraction,
                              funding_series_path=(None if args.no_funding else DEFAULT_FUNDING_PATH))
    run_null(df, args.tim, args.hold, reps=args.reps, config=cfg,
             in_exposure=args.in_exposure, seed=args.seed,
             observed_return_pct=args.observed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
