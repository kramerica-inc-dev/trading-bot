"""Faithful VRP replication (B7 deepening) — short-straddle, delta-hedged.

The first-pass VRP probe (`backtest/sweep/vrp.py`) used a *variance-swap proxy*:
monthly P&L = (IV - RV - cost) in vol points. That is the idealised payoff of a
perfectly-hedged variance swap and IGNORES the two things that actually decide
whether short-vol is tradable:

  1. **Dollar-gamma weighting.** A delta-hedged short STRADDLE is not a clean
     variance swap. Its exposure is concentrated near the strike (dollar-gamma),
     and its vega decays as spot drifts away from the strike intra-month. The
     realised P&L is the dollar-gamma-weighted (sigma_imp^2 - RV^2), not the
     clean (IV - RV). Discrete daily hedging adds path-dependent gamma error.
  2. **The real cost stack.** Not one "cost_volpts" number but: the option
     entry bid/ask, and a delta-hedge transaction cost on EVERY daily rebalance.

This module replaces the proxy with an actual Black-Scholes (r=0) short ATM
straddle, **delta-hedged daily at frozen entry IV**, held to a 30d expiry where
everything settles at intrinsic. The daily MTM telescopes exactly to
(premium - intrinsic) + hedge gains - costs, so the per-cycle P&L is the true
delta-hedged short-straddle return. P&L is reported both in dollars (on one
straddle) and normalised to vol points (dollar P&L / entry straddle vega-per-
vol-point) so it is directly comparable to the proxy's +5.4 volpt/mo headline.

On top of the short straddle we add an optional **tail-hedge**: long an OTM
strangle (wings) at a target delta, financed out of the straddle premium. The
wings cost premium every month (drag) but cap the left tail when spot gaps. The
decisive question this module answers: *does the premium survive faithful
replication, and can the -48 volpt tail be hedged cheaply enough to keep the
edge significant?*

Modelling honesty (flagged, not hidden):
  * We only have the 30d ATM IV index (Deribit DVOL), not the full surface, so
    the wings are priced at ATM IV + a parametric `skew_volpts` add-on. Real
    Deribit skew makes OTM puts richer; `skew_volpts` is swept to bound the
    tail-hedge cost. Real surface data would pin this — see docs.
  * Hedging at frozen entry IV is the standard "hedge at implied" convention;
    the terminal P&L is path/IV-path independent because we hold to expiry and
    settle at intrinsic (the intra-month IV path only affects MTM drawdown).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy.stats import norm, ttest_1samp

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
DVOL_CSV = os.path.join(DATA, "deribit_dvol_BTC.csv")
BTC_DAILY = os.path.join(DATA, "okx", "BTC-USDT_1Dutc.csv")

YEAR_DAYS = 365.0


# ---------------------------------------------------------------------------
# Black-Scholes (r=0, no carry) — the minimum needed for option replication
# ---------------------------------------------------------------------------

def _d1(S: float, K: float, T: float, sig: float) -> float:
    return (np.log(S / K) + 0.5 * sig * sig * T) / (sig * np.sqrt(T))


def bs_price(S: float, K: float, T: float, sig: float, kind: str) -> float:
    """European option price, r=0. kind in {'c','p'}."""
    if T <= 0 or sig <= 0:
        return max(0.0, (S - K) if kind == "c" else (K - S))
    d1 = _d1(S, K, T, sig)
    d2 = d1 - sig * np.sqrt(T)
    if kind == "c":
        return float(S * norm.cdf(d1) - K * norm.cdf(d2))
    return float(K * norm.cdf(-d2) - S * norm.cdf(-d1))


def bs_delta(S: float, K: float, T: float, sig: float, kind: str) -> float:
    if T <= 0 or sig <= 0:
        if kind == "c":
            return 1.0 if S > K else 0.0
        return -1.0 if S < K else 0.0
    d1 = _d1(S, K, T, sig)
    return float(norm.cdf(d1)) if kind == "c" else float(norm.cdf(d1) - 1.0)


def bs_vega(S: float, K: float, T: float, sig: float) -> float:
    """Vega per 1.00 of vol (i.e. per 100 vol points). Same for call and put."""
    if T <= 0 or sig <= 0:
        return 0.0
    d1 = _d1(S, K, T, sig)
    return float(S * norm.pdf(d1) * np.sqrt(T))


def strike_for_delta(S: float, T: float, sig: float, kind: str, target_delta: float) -> float:
    """Strike whose BS delta equals target_delta (call: +, put: -), r=0."""
    if kind == "c":
        d1 = norm.ppf(target_delta)            # N(d1)=target
    else:
        d1 = norm.ppf(target_delta + 1.0)      # N(d1)-1=target -> N(d1)=target+1
    return float(S * np.exp(0.5 * sig * sig * T - d1 * sig * np.sqrt(T)))


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class ReplConfig:
    horizon: int = 30                 # days to expiry (DVOL is the 30d index)
    hedge_cost_bps: float = 6.0       # per delta-hedge trade, bps of notional traded
    opt_entry_spread_volpts: float = 1.0   # half-spread paid on each option leg at entry
    # tail-hedge (long OTM strangle). ratio=0 -> no tail-hedge (naked short straddle)
    wing_delta: float = 0.15          # target |delta| of each long wing
    wing_ratio: float = 0.0           # long wings per 1.0 short straddle (0 = none)
    wing_skew_volpts: float = 5.0     # OTM IV = ATM IV + this (crypto skew proxy)


@dataclass
class CycleResult:
    pnl_dollars: float
    pnl_volpts: float
    realized_vol: float
    implied_vol: float
    straddle_premium: float
    wing_premium: float
    hedge_cost: float
    opt_cost: float


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def load_aligned(dvol_csv: str = DVOL_CSV, btc_csv: str = BTC_DAILY) -> pd.DataFrame:
    dv = pd.read_csv(dvol_csv)
    dv["date"] = pd.to_datetime(dv["timestamp"], utc=True).dt.floor("D")
    bt = pd.read_csv(btc_csv)
    bt["date"] = pd.to_datetime(bt["timestamp"], utc=True).dt.floor("D")
    m = dv[["date", "dvol"]].merge(bt[["date", "close"]], on="date").sort_values("date")
    return m.reset_index(drop=True)


def _realized_vol(close: np.ndarray) -> float:
    """Annualised realised vol of daily log returns over the path."""
    if len(close) < 3:
        return float("nan")
    lr = np.diff(np.log(close))
    return float(np.std(lr, ddof=1) * np.sqrt(YEAR_DAYS))


# ---------------------------------------------------------------------------
# One 30d delta-hedged short-straddle (+ optional long wings) cycle
# ---------------------------------------------------------------------------

def _build_book(S0: float, T0: float, iv: float, cfg: ReplConfig
                ) -> List[Tuple[str, float, float, float]]:
    """Book of legs: (kind, strike, qty, entry_iv). Short straddle (-1 call,
    -1 put ATM) plus optional long wings (+ratio call, +ratio put OTM)."""
    book: List[Tuple[str, float, float, float]] = [
        ("c", S0, -1.0, iv),
        ("p", S0, -1.0, iv),
    ]
    if cfg.wing_ratio > 0.0:
        wing_iv = iv + cfg.wing_skew_volpts / 100.0
        kc = strike_for_delta(S0, T0, wing_iv, "c", cfg.wing_delta)
        kp = strike_for_delta(S0, T0, wing_iv, "p", -cfg.wing_delta)
        book.append(("c", kc, cfg.wing_ratio, wing_iv))
        book.append(("p", kp, cfg.wing_ratio, wing_iv))
    return book


def _book_value(book, S: float, T: float) -> float:
    return float(sum(q * bs_price(S, K, T, iv, kind) for kind, K, q, iv in book))


def _book_delta(book, S: float, T: float) -> float:
    return float(sum(q * bs_delta(S, K, T, iv, kind) for kind, K, q, iv in book))


def replicate_cycle(closes: np.ndarray, iv: float, cfg: ReplConfig) -> CycleResult:
    """Delta-hedge one short-straddle(+wings) book over `horizon` days.

    closes: array of length horizon+1 (entry close .. expiry close).
    iv: entry implied vol (decimal, e.g. 0.65). Returns dollars + vol points.
    """
    H = cfg.horizon
    S = np.asarray(closes, dtype=float)
    S0 = S[0]
    T0 = H / YEAR_DAYS
    book = _build_book(S0, T0, iv, cfg)

    # entry option cost: pay opt_entry_spread on each leg's vega (you sell the
    # straddle that many vol points cheaper / buy the wings that much richer).
    opt_cost = 0.0
    for kind, K, q, leg_iv in book:
        vega_pvp = bs_vega(S0, K, T0, leg_iv) / 100.0
        opt_cost += abs(q) * vega_pvp * cfg.opt_entry_spread_volpts

    straddle_premium = bs_price(S0, S0, T0, iv, "c") + bs_price(S0, S0, T0, iv, "p")
    wing_premium = 0.0
    for kind, K, q, leg_iv in book:
        if q > 0:
            wing_premium += q * bs_price(S0, K, T0, leg_iv, kind)

    # daily delta-hedge at frozen entry IV; hold to expiry (settle at intrinsic).
    h_prev = -_book_delta(book, S0, T0)               # underlying held over day 0->1
    hedge_cost = abs(h_prev) * S0 * cfg.hedge_cost_bps / 1e4   # set up initial hedge
    v_prev = _book_value(book, S0, T0)
    pnl = -opt_cost - hedge_cost                       # option spread + initial hedge

    for j in range(1, H + 1):
        Tj = (H - j) / YEAR_DAYS
        Sj = S[j]
        vj = _book_value(book, Sj, Tj)
        pnl += (vj - v_prev)                           # option MTM change (to us)
        pnl += h_prev * (Sj - S[j - 1])                # hedge P&L over the day
        if j < H:                                      # no re-hedge at the instant of expiry
            h_new = -_book_delta(book, Sj, Tj)
            trade = abs(h_new - h_prev)
            hc = trade * Sj * cfg.hedge_cost_bps / 1e4
            pnl -= hc
            hedge_cost += hc
            h_prev = h_new
        v_prev = vj

    rv = _realized_vol(S)
    # normalise to vol points via entry straddle vega-per-vol-point
    straddle_vega_pvp = 2.0 * bs_vega(S0, S0, T0, iv) / 100.0
    pnl_volpts = pnl / straddle_vega_pvp if straddle_vega_pvp > 0 else float("nan")
    return CycleResult(
        pnl_dollars=float(pnl), pnl_volpts=float(pnl_volpts),
        realized_vol=rv, implied_vol=iv,
        straddle_premium=float(straddle_premium), wing_premium=float(wing_premium),
        hedge_cost=float(hedge_cost), opt_cost=float(opt_cost),
    )


def run_cycles(m: pd.DataFrame, cfg: ReplConfig) -> List[CycleResult]:
    """Non-overlapping 30d cycles over the aligned (date,dvol,close) frame."""
    iv_all = m["dvol"].to_numpy() / 100.0
    close = m["close"].to_numpy()
    n = len(m)
    out: List[CycleResult] = []
    t = 0
    while t + cfg.horizon < n:
        seg = close[t: t + cfg.horizon + 1]
        if len(seg) == cfg.horizon + 1 and np.all(np.isfinite(seg)) and np.all(seg > 0):
            out.append(replicate_cycle(seg, float(iv_all[t]), cfg))
        t += cfg.horizon
    return out


# ---------------------------------------------------------------------------
# Stats / summary (mirrors sweep/vrp.py so results are directly comparable)
# ---------------------------------------------------------------------------

def _max_dd(equity: np.ndarray) -> float:
    if not len(equity):
        return 0.0
    peak = np.maximum.accumulate(equity)
    return float(np.max(peak - equity))


def summarize(cycles: List[CycleResult], *, reps: int = 1000, seed: int = 20260604) -> dict:
    pv = np.array([c.pnl_volpts for c in cycles], dtype=float)
    pd_ = np.array([c.pnl_dollars for c in cycles], dtype=float)
    notional = np.array([c.straddle_premium for c in cycles], dtype=float)
    if len(pv) < 6:
        return {"n_months": int(len(pv)), "error": "too few cycles"}

    mean_v = float(np.mean(pv))
    t_stat, p = ttest_1samp(pv, 0.0)
    sd = np.std(pv, ddof=1)
    sharpe = float(mean_v / sd * np.sqrt(12)) if sd > 0 else 0.0
    equity = np.cumsum(pv)
    maxdd = _max_dd(equity)
    worst = float(np.min(pv))
    k = max(1, len(pv) // 20)
    cvar5 = float(np.mean(np.sort(pv)[:k]))
    tail_ratio = abs(worst) / mean_v if mean_v > 0 else float("inf")

    thirds = np.array_split(pv, 3)
    pos_thirds = int(sum(1 for tp in thirds if len(tp) and np.mean(tp) > 0))

    # random-sign null on vol-point P&L (same control as the proxy)
    rng = np.random.default_rng(seed)
    null_sharpes = []
    for _ in range(reps):
        signs = rng.choice([-1.0, 1.0], size=len(pv))
        npnl = signs * pv
        s = np.std(npnl, ddof=1)
        null_sharpes.append(float(np.mean(npnl) / s * np.sqrt(12)) if s > 0 else 0.0)
    pct = float(np.mean(np.asarray(null_sharpes) < sharpe) * 100.0)

    return {
        "n_months": int(len(pv)),
        "mean_pnl_volpts_per_month": round(mean_v, 2),
        "t_stat": round(float(t_stat), 2),
        "p_value": round(float(p), 4),
        "sharpe_annual": round(sharpe, 2),
        "null_percentile": round(pct, 1),
        "max_dd_volpts": round(maxdd, 1),
        "worst_month_volpts": round(worst, 1),
        "cvar5_volpts": round(cvar5, 1),
        "tail_ratio_worst_over_mean": round(tail_ratio, 1) if np.isfinite(tail_ratio) else None,
        "positive_sub_periods": pos_thirds,
        "mean_pnl_dollars_per_straddle": round(float(np.mean(pd_)), 2),
        "worst_month_dollars_per_straddle": round(float(np.min(pd_)), 2),
        "mean_straddle_premium": round(float(np.mean(notional)), 2),
        "mean_hedge_cost_dollars": round(float(np.mean([c.hedge_cost for c in cycles])), 2),
        "mean_opt_cost_dollars": round(float(np.mean([c.opt_cost for c in cycles])), 2),
    }


def capital_translation(cycles: List[CycleResult], *, cvar_budget_pct: float = 10.0,
                        capital: float = 100_000.0) -> dict:
    """Size the book so the worst-5% monthly dollar loss = cvar_budget_pct of
    capital, then report the resulting %/yr. Converts 'volpts/mo' into a real
    return-on-capital at a stated tail budget."""
    pd_ = np.array([c.pnl_dollars for c in cycles], dtype=float)
    if len(pd_) < 6:
        return {"error": "too few cycles"}
    k = max(1, len(pd_) // 20)
    cvar5_dollars = float(np.mean(np.sort(pd_)[:k]))     # negative
    if cvar5_dollars >= 0:
        return {"note": "no left tail in sample — sizing unbounded", "cvar5_dollars": cvar5_dollars}
    budget = capital * cvar_budget_pct / 100.0
    n_straddles = budget / abs(cvar5_dollars)            # so CVaR5 loss = budget
    mean_monthly = float(np.mean(pd_)) * n_straddles
    ann_return_pct = mean_monthly * 12.0 / capital * 100.0
    worst_month_dollars = float(np.min(pd_)) * n_straddles
    return {
        "cvar_budget_pct": cvar_budget_pct,
        "n_straddles": round(n_straddles, 2),
        "ann_return_on_capital_pct": round(ann_return_pct, 2),
        "worst_month_loss_pct_of_capital": round(worst_month_dollars / capital * 100.0, 2),
        "mean_monthly_dollars": round(mean_monthly, 2),
    }


# ---------------------------------------------------------------------------
# CLI: proxy vs faithful (naked) vs faithful (tail-hedged), across costs
# ---------------------------------------------------------------------------

def _print_summary(label: str, s: dict) -> None:
    if "error" in s:
        print(f"{label:34s} {s}")
        return
    print(f"{label:34s} mean={s['mean_pnl_volpts_per_month']:+6.2f}vp/mo "
          f"t={s['t_stat']:+5.2f} (p={s['p_value']:.3f}) Sharpe={s['sharpe_annual']:+5.2f} "
          f"null={s['null_percentile']:5.1f} worst={s['worst_month_volpts']:+6.1f} "
          f"CVaR5={s['cvar5_volpts']:+6.1f} sub+={s['positive_sub_periods']}/3")


if __name__ == "__main__":
    m = load_aligned()
    print(f"VRP faithful replication — {len(m)} aligned days "
          f"({m['date'].min().date()}..{m['date'].max().date()})\n")

    print("== Naked short straddle (no tail-hedge), hedge_cost/opt_spread sweep ==")
    for hc in (0.0, 6.0, 12.0):
        for sp in (0.0, 1.0, 2.0):
            cfg = ReplConfig(hedge_cost_bps=hc, opt_entry_spread_volpts=sp, wing_ratio=0.0)
            s = summarize(run_cycles(m, cfg))
            _print_summary(f"hedge={hc:>4}bps opt_spread={sp}vp", s)

    print("\n== Tail-hedged (long wings) — realistic costs (6bps / 1vp) ==")
    base = dict(hedge_cost_bps=6.0, opt_entry_spread_volpts=1.0)
    for wd in (0.10, 0.15, 0.25):
        for wr in (0.5, 1.0):
            for sk in (0.0, 5.0, 10.0):
                cfg = ReplConfig(wing_delta=wd, wing_ratio=wr, wing_skew_volpts=sk, **base)
                s = summarize(run_cycles(m, cfg))
                _print_summary(f"wing d={wd} ratio={wr} skew={sk}vp", s)

    print("\n== Capital translation (CVaR-5% sized to 10% of capital) ==")
    for label, cfg in (
        ("naked (6bps/1vp)", ReplConfig(wing_ratio=0.0, **base)),
        ("tail d=0.15 r=1.0 sk=5", ReplConfig(wing_delta=0.15, wing_ratio=1.0, wing_skew_volpts=5.0, **base)),
    ):
        cyc = run_cycles(m, cfg)
        print(f"{label:28s} {capital_translation(cyc)}")
