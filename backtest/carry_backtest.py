#!/usr/bin/env python3
"""Cash-and-carry (funding/basis carry) backtester — scoping for `docs/STRATEGY-CARRY.md`.

Simulates a *delta-neutral* position:

  * **spot leg**: long X USD of BTC (held passively, marks with BTC price);
  * **perp leg**: short the same X USD of BTC-USDT perpetual.

The two price legs cancel (delta ~= 0) so equity is *not* driven by BTC's
price — it is driven by:

  1. **funding accrual** every 8h: a perp *short* RECEIVES `notional * rate`
     when the funding rate is positive (the persistent regime on BTC), PAYS it
     when negative;
  2. **basis P&L**: spot and perp don't track perfectly.  If we model the perp
     as `spot * (1 + b_t)` where `b_t` is the (small, mean-reverting) basis,
     then a short-perp + long-spot pair earns `-notional * (b_T - b_0)` over a
     holding period — i.e. you make money if the perp's premium *shrinks*
     between entry and exit and lose if it widens.  We model `b_t` as a small
     OU-ish noise series (default sigma ~ a few bps) unless a real perp price
     series is supplied;
  3. **fees**: 2 fills to open (buy spot, sell perp) and 2 to close (sell spot,
     buy perp) = 4 fills, charged once over the run, plus optional re-leg costs
     if the funding on/off rule fires.

The on/off rule (`funding_gate_window`, `funding_gate_thresh`): if the trailing
`funding_gate_window` settlements' mean funding rate drops below
`funding_gate_thresh`, *unwind the carry* (sell spot, buy back perp), pay the
2 exit fills, and sit in cash earning 0 until the trailing mean climbs back
above the threshold, then re-enter (2 more fills).  "Always on" is the special
case `funding_gate_window = 0`.

This is a deliberately *thin* approximation — see `docs/STRATEGY-CARRY.md` §
"Approximations" for what is and isn't modelled.  Run:

    python -m backtest.carry_backtest                 # base case
    python -m backtest.carry_backtest --gate 21 0     # on/off rule, 7-day window
    python -m backtest.carry_backtest --plot
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backtest"))

from daily_backtester import load_funding_series, DEFAULT_FUNDING_PATH  # noqa: E402

RESULTS_DIR = os.path.join(ROOT, "backtest", "results")
SETTLEMENTS_PER_YEAR = 3 * 365  # 8h settlements


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class CarryConfig:
    """Economics of the carry trade.  Fees default to BloFin-typical values
    (perp taker ~0.06% / maker ~0.02%, spot similar) — override for OKX.

    `maker_fraction` blends maker/taker per fill (0.0 == always taker / worst,
    1.0 == always maker / best).  Carry entries can be patient (post-only) so
    a high maker fraction is realistic for the open/close fills.
    """
    initial_balance: float = 5000.0
    notional_fraction: float = 1.0       # fraction of equity deployed delta-neutral
                                         # (1.0 == fully deployed; <1 leaves idle cash)
    # fees, per fill, as a fraction of the leg notional
    fee_maker_spot: float = 0.0002
    fee_taker_spot: float = 0.0006
    fee_maker_perp: float = 0.0002
    fee_taker_perp: float = 0.0006
    maker_fraction: float = 0.50         # blended maker share on the open/close fills
    # basis-noise model (used only if no real perp series supplied)
    basis_sigma_bps: float = 4.0         # std-dev of the per-settlement basis (bps)
    basis_meanrev: float = 0.10          # OU pull-to-zero coefficient per settlement
    basis_seed: int = 12345
    # funding on/off gate
    funding_gate_window: int = 0         # 0 == always on; else N trailing settlements
    funding_gate_thresh: float = 0.0     # re-enter/stay when trailing mean rate > this

    @property
    def fee_per_leg_open(self) -> float:
        f = max(0.0, min(1.0, self.maker_fraction))
        spot = f * self.fee_maker_spot + (1 - f) * self.fee_taker_spot
        perp = f * self.fee_maker_perp + (1 - f) * self.fee_taker_perp
        return spot + perp  # buy spot + sell perp

    @property
    def fee_round_trip(self) -> float:
        # open (buy spot + sell perp) + close (sell spot + buy perp) == 4 fills,
        # symmetric, so 2 * fee_per_leg_open
        return 2.0 * self.fee_per_leg_open


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class CarryResult:
    equity_curve: List[float]
    timestamps: List
    funding_curve: List[float]          # rolling-annualized funding at each settlement
    on_flags: List[bool]                # carry active at each settlement?
    cfg: CarryConfig
    n_legs: int = 0                     # number of open/close fill *bursts* (2 legs each)
    total_funding: float = 0.0
    total_fees: float = 0.0
    total_basis_pnl: float = 0.0
    years: float = 0.0

    @property
    def total_return(self) -> float:
        return self.equity_curve[-1] / self.equity_curve[0] - 1.0

    @property
    def annualized_return(self) -> float:
        if self.years <= 0:
            return 0.0
        return (1.0 + self.total_return) ** (1.0 / self.years) - 1.0

    @property
    def max_drawdown(self) -> float:
        eq = np.asarray(self.equity_curve, float)
        peak = np.maximum.accumulate(eq)
        with np.errstate(divide="ignore", invalid="ignore"):
            dd = np.where(peak > 0, (peak - eq) / peak, 0.0)
        return float(np.max(dd)) if len(dd) else 0.0

    @property
    def sharpe(self) -> float:
        # per-settlement equity returns, annualized (rf=0)
        eq = np.asarray(self.equity_curve, float)
        rets = np.diff(eq) / eq[:-1]
        if rets.std(ddof=1) == 0 or len(rets) < 2:
            return 0.0
        return float(rets.mean() / rets.std(ddof=1) * np.sqrt(SETTLEMENTS_PER_YEAR))

    @property
    def calmar(self) -> float:
        dd = self.max_drawdown
        if dd <= 1e-9:
            return float("inf") if self.annualized_return > 0 else 0.0
        return self.annualized_return / dd


# ---------------------------------------------------------------------------
# Backtester
# ---------------------------------------------------------------------------

def _basis_series(n: int, cfg: CarryConfig) -> np.ndarray:
    """A small mean-reverting (OU) basis series, in *fractional* units."""
    rng = np.random.default_rng(cfg.basis_seed)
    sigma = cfg.basis_sigma_bps / 1e4
    b = np.zeros(n)
    for i in range(1, n):
        b[i] = b[i - 1] * (1 - cfg.basis_meanrev) + rng.normal(0.0, sigma)
    return b


def run_carry_backtest(funding_df: pd.DataFrame,
                       cfg: Optional[CarryConfig] = None,
                       perp_basis: Optional[np.ndarray] = None) -> CarryResult:
    """Replay an 8h funding series through the delta-neutral carry trade.

    `funding_df`: columns 'timestamp' (settlement) and 'funding_rate'.
    `perp_basis`: optional real (perp/spot - 1) series aligned to funding_df;
                  if None, a synthetic small OU basis is generated.
    """
    cfg = cfg or CarryConfig()
    fd = funding_df.sort_values("timestamp").reset_index(drop=True)
    n = len(fd)
    if n < 2:
        raise ValueError("carry backtest needs >= 2 funding settlements")
    rates = fd["funding_rate"].astype(float).to_numpy()
    ts = fd["timestamp"].tolist()
    basis = perp_basis if perp_basis is not None else _basis_series(n, cfg)

    equity = float(cfg.initial_balance)
    equity_curve = [equity]
    funding_curve = [0.0]
    on_flags = [False]

    # state
    active = False           # is the carry currently on?
    entry_basis = 0.0        # basis at the last entry
    n_legs = 0
    tot_funding = tot_fees = tot_basis = 0.0

    win = cfg.funding_gate_window
    thr = cfg.funding_gate_thresh
    half_open_fee = cfg.fee_per_leg_open  # buy spot + sell perp == 2 fills

    for i in range(n):
        # --- decide on/off for the *upcoming* settlement using only past data ---
        if win <= 0:
            want_on = True
        else:
            if i == 0:
                want_on = False  # need history first
            else:
                lo = max(0, i - win)
                trailing_mean = float(rates[lo:i].mean()) if i > lo else 0.0
                want_on = trailing_mean > thr

        notional = cfg.notional_fraction * equity

        # --- transitions: pay the 2-fill cost on enter or exit ---
        if want_on and not active:
            # enter: buy spot + sell perp
            cost = notional * half_open_fee
            equity -= cost
            tot_fees += cost
            n_legs += 1
            active = True
            entry_basis = basis[i]
            notional = cfg.notional_fraction * equity
        elif active and not want_on:
            # exit: mark this settlement's basis step (entry..i-1 was marked
            # over prior settlements), then pay the 2-fill close cost.
            if i > 0:
                db = basis[i] - basis[i - 1]
                bstep = -notional * db
                equity += bstep
                tot_basis += bstep
            cost = notional * half_open_fee
            equity -= cost
            tot_fees += cost
            n_legs += 1
            active = False

        # --- accrue this settlement's funding (short receives positive rate) ---
        f_pnl = 0.0
        if active:
            f_pnl = notional * rates[i]
            equity += f_pnl
            tot_funding += f_pnl
            # mark the basis change of *this* settlement into equity continuously
            if i > 0:
                db = basis[i] - basis[i - 1]
                bstep = -notional * db
                equity += bstep
                tot_basis += bstep

        equity_curve.append(equity)
        # rolling-annualized funding (trailing 90d ~= 270 settlements) for display
        lo = max(0, i - 270)
        funding_curve.append(float(rates[lo:i + 1].mean()) * SETTLEMENTS_PER_YEAR)
        on_flags.append(active)

    # if still active at the end, unwind: the per-step marks already captured
    # the full basis move entry..end, so just charge the 2-fill close cost.
    if active:
        notional = cfg.notional_fraction * equity
        cost = notional * half_open_fee
        equity -= cost
        tot_fees += cost
        n_legs += 1
        equity_curve[-1] = equity

    years = (ts[-1] - ts[0]).total_seconds() / (365.25 * 24 * 3600)
    return CarryResult(equity_curve=equity_curve, timestamps=[ts[0]] + ts,
                       funding_curve=funding_curve, on_flags=on_flags, cfg=cfg,
                       n_legs=n_legs, total_funding=tot_funding,
                       total_fees=tot_fees, total_basis_pnl=tot_basis,
                       years=years)


# ---------------------------------------------------------------------------
# CLI / report
# ---------------------------------------------------------------------------

def _fmt_pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def _summary(name: str, r: CarryResult) -> str:
    return (f"{name:<26} tot {_fmt_pct(r.total_return):>9}  ann {_fmt_pct(r.annualized_return):>8}  "
            f"maxDD {r.max_drawdown * 100:5.2f}%  Sharpe {r.sharpe:5.2f}  Calmar "
            f"{r.calmar if np.isfinite(r.calmar) else float('inf'):>6.2f}  "
            f"fund ${r.total_funding:8.2f}  fees ${r.total_fees:7.2f}  legs {r.n_legs}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--funding", default=DEFAULT_FUNDING_PATH)
    ap.add_argument("--balance", type=float, default=5000.0)
    ap.add_argument("--maker-frac", type=float, default=0.50)
    ap.add_argument("--gate", nargs=2, type=float, default=None,
                    metavar=("WINDOW", "THRESH"),
                    help="on/off rule: trailing WINDOW settlements, re-enter when mean > THRESH")
    ap.add_argument("--plot", action="store_true")
    args = ap.parse_args(argv)

    fd = load_funding_series(args.funding)
    if fd.empty:
        print(f"no funding data at {args.funding}")
        return 1
    print(f"funding span: {fd['timestamp'].min()} -> {fd['timestamp'].max()}  "
          f"({len(fd)} settlements)")

    base = CarryConfig(initial_balance=args.balance, maker_fraction=args.maker_frac)
    maker_best = CarryConfig(initial_balance=args.balance, maker_fraction=1.0)
    taker_worst = CarryConfig(initial_balance=args.balance, maker_fraction=0.0)
    gate_cfg = CarryConfig(initial_balance=args.balance, maker_fraction=args.maker_frac,
                           funding_gate_window=21, funding_gate_thresh=0.0)
    if args.gate:
        gate_cfg.funding_gate_window = int(args.gate[0])
        gate_cfg.funding_gate_thresh = args.gate[1]

    r_base = run_carry_backtest(fd, base)
    r_maker = run_carry_backtest(fd, maker_best)
    r_taker = run_carry_backtest(fd, taker_worst)
    r_gate = run_carry_backtest(fd, gate_cfg)

    # benchmarks: cash (0%) and BTC buy-and-hold over the same span (proxy via funding ts is wrong;
    # report from the daily CSV if present)
    btc_path = os.path.join(ROOT, "backtest", "data", "BTC-USDT_1d.csv")
    btc_bh = None
    if os.path.exists(btc_path):
        b = pd.read_csv(btc_path)
        b["timestamp"] = pd.to_datetime(b["timestamp"], utc=True)
        b = b[(b["timestamp"] >= fd["timestamp"].min()) & (b["timestamp"] <= fd["timestamp"].max())]
        if len(b) > 1:
            btc_bh = b["close"].iloc[-1] / b["close"].iloc[0] - 1.0

    print()
    print(_summary("carry (blended fees)", r_base))
    print(_summary("carry (maker-best)", r_maker))
    print(_summary("carry (taker-worst)", r_taker))
    print(_summary(f"carry on/off gate w={gate_cfg.funding_gate_window}", r_gate))
    print(f"{'cash (0%)':<26} tot {_fmt_pct(0.0):>9}  ann {_fmt_pct(0.0):>8}  maxDD  0.00%")
    if btc_bh is not None:
        bh_ann = (1 + btc_bh) ** (1 / r_base.years) - 1
        print(f"{'BTC buy-and-hold':<26} tot {_fmt_pct(btc_bh):>9}  ann {_fmt_pct(bh_ann):>8}  "
              f"(over the same {r_base.years:.2f}y span)")

    print(f"\ngross carry (sum funding / start equity, annualized): "
          f"{(fd['funding_rate'].sum() / 1.0) * 0 + fd['funding_rate'].mean() * SETTLEMENTS_PER_YEAR * 100:.2f}%/yr")
    print(f"round-trip fee (open+close, blended {args.maker_frac:.0%} maker): "
          f"{base.fee_round_trip * 100:.3f}%  | maker-best {maker_best.fee_round_trip*100:.3f}%  "
          f"| taker-worst {taker_worst.fee_round_trip*100:.3f}%")

    if args.plot:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            os.makedirs(RESULTS_DIR, exist_ok=True)
            fig, axes = plt.subplots(2, 1, figsize=(11, 8), sharex=True)
            t = pd.to_datetime(r_base.timestamps)
            axes[0].plot(t, r_base.equity_curve, label="carry (blended fees)")
            axes[0].plot(t, r_maker.equity_curve, label="carry (maker-best)", alpha=.6)
            axes[0].plot(t, r_taker.equity_curve, label="carry (taker-worst)", alpha=.6)
            axes[0].plot(t, r_gate.equity_curve,
                         label=f"carry on/off (w={gate_cfg.funding_gate_window})", alpha=.7)
            axes[0].axhline(args.balance, color="grey", ls="--", lw=.8, label="cash (flat)")
            axes[0].set_ylabel("equity ($)")
            axes[0].set_title("Delta-neutral BTC cash-and-carry — equity curve")
            axes[0].legend(fontsize=8)
            axes[0].grid(alpha=.3)
            axes[1].plot(t, np.asarray(r_base.funding_curve) * 100, color="tab:green",
                         label="trailing-~90d funding, annualized %")
            axes[1].axhline(0, color="red", ls="--", lw=.8)
            axes[1].set_ylabel("funding (ann. %)")
            axes[1].legend(fontsize=8)
            axes[1].grid(alpha=.3)
            out = os.path.join(RESULTS_DIR, "carry_equity.png")
            fig.tight_layout()
            fig.savefig(out, dpi=110)
            print(f"\nplot -> {out}")
        except Exception as e:  # pragma: no cover
            print(f"plot failed: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
