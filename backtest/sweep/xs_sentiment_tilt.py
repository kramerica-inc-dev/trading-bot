"""Asymmetric SENTIMENT TILT on the cross-sectional momentum basket.

The live lane is a DOLLAR-NEUTRAL long-short basket (long top-m / short bottom-m
of 10 OKX perps by trailing return, lb=120, rebal=5). This module asks the user's
question: can a CAUSAL market-regime signal asymmetrically tilt the book net-long
in a bull / net-short in a bear, and does a stop-loss tame sudden reversals —
WITHOUT degrading out-of-sample or worsening drawdown?

Strong priors (docs/XS-BETA-STUDY, XS-TRIGGER-STUDY, SWEEP-RESULTS): the neutral
book already carries a pro-cyclical hidden net-beta that HELPED in the bull sample
but degraded every OOS metric when removed; the momentum edge is concentrated in
the 2023 bull and the 2024-25 holdout is ~flat/negative; ALL price-directional
families are dead inside the random-entry null band. So amplifying the tilt is
expected to juice the in-sample (bull) return and likely fail OOS / worsen DD.
This module MEASURES it — adversarially honest about lookahead and OOS.

Design (reuses backtest/sweep/xsectional primitives — load_panel, the trailing-
return ranking, the random-basket null + shuffle sham, _total_return_pct/_sharpe):

  * CAUSAL regime s_t in {+1 bull, -1 bear}: sign of BTC trailing return over W,
    computed from closes STRICTLY BEFORE t (closes[t-1] / closes[t-1-W]). NO
    lookahead — asserted. (closes[t] itself is the decision-day close, which the
    book then earns over day t->t+1; using closes[t] in the trend would peek at
    the very return we are about to trade, so we deliberately lag one day.)

  * ASYMMETRIC TILT tau: long-leg weight = base*(1 + tau*s_t), short-leg weight =
    base*(1 - tau*s_t). tau=0 == dollar-neutral baseline; tau=1 == long-only in a
    bull / short-only in a bear. Gross exposure is RE-NORMALIZED to a constant
    TARGET_GROSS across tau for a fair comparison; realized NET exposure reported.

  * Continuous-OOS sim (matches XS-TRIGGER-STUDY): the FULL series is simulated
    once with the book carried (weights drift with returns between rebalances) and
    daily returns are sliced to the holdout — no free clean re-entry at the
    boundary. Cost = same cost_rate as xsectional (0.0015) on |dweight| traded,
    plus an optional flat funding drag on gross (HL-calibrated ~6%/yr).

  * STOP / de-risk overlays (tested on the best tilt only):
      (a) FLIP-TO-NEUTRAL: when s_t reverses against the current tilt, drop to
          dollar-neutral (tau->0) until the regime re-confirms.
      (b) TRAILING EQUITY STOP: arm after equity is +arm_pct above the entry
          high; then if equity falls trail_pct below its peak, flatten the
          DIRECTIONAL part (revert to neutral) until the next rebalance.

Risk-adjusted metrics: max drawdown, Calmar, CVaR-5%. The null is the random
dollar-neutral basket (matched fire dates) carried through the SAME tilt machinery
on a random sign-stream control; the sham shuffles the asset ranking and MUST FAIL
the gate (else the result is VOID).
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from xsectional import (  # noqa: E402
    load_panel, DEFAULT_ASSETS, _total_return_pct, _sharpe,
)

# Cost: the SAME cost_rate as xsectional (the conservative 15bps / |dweight|).
COST_RATE = 0.0015
# Flat funding drag on gross (HL-calibrated ~6%/yr, 1.65bps/day) — the same stress
# the trigger/beta studies applied; a tilt that adds a directional leg should pay
# the same funding the neutral book pays. Set to 0.0 to disable.
FUNDING = 0.000165
TARGET_GROSS = 2.0            # sum|w| of the neutral book at a fresh +-1/m rebalance
MKT = 0                       # BTC-USDT is index 0 in DEFAULT_ASSETS (the regime asset)


@dataclass
class TiltConfig:
    lookback: int = 120          # momentum ranking window (days)
    rebal: int = 5               # rebalance cadence (days)
    m: int = 3                   # longs = top m, shorts = bottom m
    regime_w: int = 100          # BTC trailing-return window for the regime signal
    tau: float = 0.0             # asymmetric tilt strength (0 == neutral baseline)
    cost_rate: float = COST_RATE
    funding: float = FUNDING
    # overlays (tested on the best tilt only)
    flip_neutral: bool = False   # drop to neutral when regime reverses the tilt
    trail_stop: bool = False     # trailing equity stop on the directional part
    arm_pct: float = 0.05        # arm the trailing stop after +arm_pct equity gain
    trail_pct: float = 0.10      # flatten directional part on a trail_pct drawdown


# ---------------------------------------------------------------------------
# Causal regime signal (NO LOOKAHEAD)
# ---------------------------------------------------------------------------

def regime_signal(closes: np.ndarray, w: int, *, mkt: int = MKT) -> np.ndarray:
    """s_t in {+1, -1, 0} from the sign of BTC's trailing return over window `w`,
    using closes STRICTLY BEFORE day t: sign(close[t-1] / close[t-1-w] - 1).

    Day t's book is set at the close of day t-1 (it earns ret over t-1->t in the
    sim's bar t-1 row). To be tradable, s_t may only use info available at the
    close of t-1, i.e. closes up to index t-1. Returns length n (s[t] for the
    decision at the start of day t); s[t]=0 (neutral) until enough history.
    """
    n = closes.shape[0]
    mc = closes[:, mkt]
    s = np.zeros(n, dtype=float)
    for t in range(n):
        j = t - 1                      # last close available when setting day-t book
        if j - w >= 0 and np.isfinite(mc[j]) and np.isfinite(mc[j - w]) and mc[j - w] > 0:
            r = mc[j] / mc[j - w] - 1.0
            s[t] = 1.0 if r > 0 else (-1.0 if r < 0 else 0.0)
    return s


def _assert_no_lookahead(closes: np.ndarray, w: int, *, mkt: int = MKT) -> None:
    """Independently recompute s_t two ways and assert the signal at t never uses
    close[t] (or later). We perturb close[t..] to +inf and confirm s[:t] is
    unchanged — a hard guard against the #1 trap."""
    s_ref = regime_signal(closes, w, mkt=mkt)
    n = closes.shape[0]
    for t in (w + 2, n // 2, n - 1):
        if not (0 <= t < n):
            continue
        cc = closes.copy()
        cc[t:, mkt] = np.inf            # poison the present + future
        s_pert = regime_signal(cc, w, mkt=mkt)
        if not np.array_equal(s_ref[:t + 1], s_pert[:t + 1]):
            raise AssertionError(
                f"LOOKAHEAD: regime signal at/<= t={t} changed when closes[t:] "
                "were poisoned — s_t depends on close[t] or later.")


# ---------------------------------------------------------------------------
# Tilted dollar-neutral simulation (continuous book carry, like XS-TRIGGER)
# ---------------------------------------------------------------------------

def _target_weights(trail: np.ndarray, m: int, tau: float, s: float) -> np.ndarray:
    """Asymmetric tilt with gross re-normalized to TARGET_GROSS.

    long-leg base*(1 + tau*s), short-leg base*(1 - tau*s); base = 1/m so the
    untilted book is +-1/m (gross = 2). Then rescale so sum|w| == TARGET_GROSS
    for EVERY tau (fair gross-matched comparison). Returns (k,) weights.
    """
    k = len(trail)
    order = np.argsort(trail)
    longs = order[-m:]
    shorts = order[:m]
    base = 1.0 / m
    wl = base * (1.0 + tau * s)        # per-name long weight
    ws = base * (1.0 - tau * s)        # per-name short weight (sign applied below)
    w = np.zeros(k)
    w[longs] = wl
    w[shorts] = -ws
    gross = float(np.abs(w).sum())
    if gross > 1e-12:
        w *= TARGET_GROSS / gross
    return w


def _random_tilted(order: np.ndarray, m: int, tau: float, s: float) -> np.ndarray:
    """Random dollar-neutral basket (top-m/bottom-m of a random permutation) with
    the SAME asymmetric tilt + gross re-normalization as the momentum book."""
    k = len(order)
    base = 1.0 / m
    w = np.zeros(k)
    w[order[-m:]] = base * (1.0 + tau * s)
    w[order[:m]] = -base * (1.0 - tau * s)
    gross = float(np.abs(w).sum())
    if gross > 1e-12:
        w *= TARGET_GROSS / gross
    return w


def simulate(closes: np.ndarray, cfg: TiltConfig, *, selection: str = "momentum",
             rng: Optional[np.random.Generator] = None,
             s_override: Optional[np.ndarray] = None,
             ) -> Tuple[np.ndarray, np.ndarray, List[int]]:
    """Daily tilted dollar-neutral-ish portfolio returns with the book carried
    across rebalances (weights drift with realized returns between fires), the
    same convention as the validated trigger study.

    selection: 'momentum' (rank by trailing return), 'random' (random baskets,
    matched fire dates), 'shuffle' (rank a shuffled trailing vector).

    Returns (daily_port[n-1], net_exposure[n-1], fire_dates).
    net_exposure[t] = sum(weights) / TARGET_GROSS  (signed gross-normalized tilt).
    """
    n, k = closes.shape
    rets = closes[1:] / closes[:-1] - 1.0          # (n-1, k); row t == day t->t+1
    s = s_override if s_override is not None else regime_signal(closes, cfg.regime_w)

    w = np.zeros(k)
    out = np.empty(n - 1)
    net_exp = np.empty(n - 1)
    fires: List[int] = []

    # trailing-stop state (entry-relative, re-armed each rebalance)
    eq = 1.0                # global running equity (compounding port)
    entry_eq = 1.0          # equity at the last rebalance (the within-cycle base)
    peak_eq = 1.0           # peak equity since the last rebalance
    armed = False           # trailing stop arms after +arm_pct within the cycle
    stopped = False         # directional book flattened until the next rebalance

    last_signed_tau = 0.0   # the tilt actually applied at the last rebalance

    for t in range(n - 1):
        cost = 0.0
        if t % cfg.rebal == 0 and t >= cfg.lookback:
            st = float(s[t])
            tau = cfg.tau
            # overlay (a): drop the tilt to neutral when the regime has REVERSED
            # against the tilt we currently hold (de-risk into a flip, don't double
            # down on a fresh ranking until the new regime confirms next cycle).
            if cfg.flip_neutral and last_signed_tau != 0.0 and st * np.sign(last_signed_tau) < 0:
                tau = 0.0
            # overlay (b): if the trailing stop tripped during the prior cycle,
            # re-enter neutral this cycle (re-arm fresh thereafter).
            if cfg.trail_stop and stopped:
                tau = 0.0
            trail = closes[t] / closes[t - cfg.lookback] - 1.0
            if selection == "momentum":
                nw = _target_weights(trail, cfg.m, tau, st)
            elif selection == "random":
                # uniform random dollar-neutral basket, then APPLY THE SAME TILT —
                # so the null carries the tilt's directional bet and we isolate the
                # MOMENTUM-RANKING contribution (xsectional.py's random selection).
                nw = _random_tilted(rng.permutation(k), cfg.m, tau, st)
            elif selection == "shuffle":
                nw = _target_weights(rng.permutation(trail), cfg.m, tau, st)
            else:
                raise ValueError(selection)
            cost = float(np.abs(nw - w).sum()) * cfg.cost_rate
            w = nw
            fires.append(t)
            last_signed_tau = tau * st
            armed = False            # re-arm fresh at each rebalance
            stopped = False
            entry_eq = eq            # within-cycle base for the trailing stop
            peak_eq = eq

        drag = cfg.funding * float(np.abs(w).sum())
        port = float((w * rets[t]).sum() - cost - drag)
        out[t] = port
        net_exp[t] = float(w.sum()) / TARGET_GROSS

        # compound equity + trailing-stop bookkeeping (entry-relative, intra-cycle)
        eq *= (1.0 + port)
        if cfg.trail_stop and not stopped:
            peak_eq = max(peak_eq, eq)
            if not armed and eq >= entry_eq * (1.0 + cfg.arm_pct):
                armed = True         # the cycle gained +arm_pct -> protect it
            if armed and eq <= peak_eq * (1.0 - cfg.trail_pct):
                stopped = True
                w = np.zeros(k)      # flatten the book on a trailing-stop trip

        # carry the book: weights drift with realized returns between rebalances
        denom = 1.0 + port
        w = w * (1.0 + rets[t]) / (denom if abs(denom) > 1e-9 else 1e-9)

    return out, net_exp, fires


# ---------------------------------------------------------------------------
# Risk metrics
# ---------------------------------------------------------------------------

def equity_curve(port: np.ndarray) -> np.ndarray:
    return np.cumprod(1.0 + port)


def max_drawdown_pct(port: np.ndarray) -> float:
    if len(port) == 0:
        return 0.0
    eq = equity_curve(port)
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / peak
    return float(dd.min() * 100.0)


def calmar(port: np.ndarray, *, periods_per_year: int = 365) -> float:
    if len(port) == 0:
        return 0.0
    eq = equity_curve(port)
    years = len(port) / periods_per_year
    if years <= 0 or eq[-1] <= 0:
        return 0.0
    cagr = eq[-1] ** (1.0 / years) - 1.0
    mdd = abs(max_drawdown_pct(port) / 100.0)
    return float(cagr / mdd) if mdd > 1e-9 else 0.0


def cvar_pct(port: np.ndarray, *, alpha: float = 0.05) -> float:
    """CVaR-5% of DAILY returns (mean of the worst alpha tail), in percent."""
    if len(port) == 0:
        return 0.0
    q = np.quantile(port, alpha)
    tail = port[port <= q]
    return float(np.mean(tail) * 100.0) if len(tail) else float(q * 100.0)


# ---------------------------------------------------------------------------
# Evaluation: full-sample + continuous OOS holdout + null + sham
# ---------------------------------------------------------------------------

def evaluate(closes: np.ndarray, cfg: TiltConfig, *, cut: int, reps: int = 400,
             n_sham: int = 3, seed: int = 20260606) -> Dict:
    """Full-sample + continuous-holdout obs vs the random-basket null (matched
    fire dates, SAME tilt machinery driven by the SAME causal regime stream), plus
    the shuffle sham. Returns a metrics dict (JSON-ready)."""
    s = regime_signal(closes, cfg.regime_w)
    port, net_exp, fires = simulate(closes, cfg, selection="momentum", s_override=s)

    hold = port[cut:]
    full = {
        "net_pct": round(_total_return_pct(port), 2),
        "sharpe": round(_sharpe(port), 3),
        "max_dd_pct": round(max_drawdown_pct(port), 2),
        "calmar": round(calmar(port), 3),
        "cvar5_pct": round(cvar_pct(port), 3),
        "mean_net_exposure": round(float(np.mean(net_exp)), 3),
        "net_long_day_frac": round(float(np.mean(net_exp > 1e-9)), 3),
    }
    hold_m = {
        "net_pct": round(_total_return_pct(hold), 2),
        "sharpe": round(_sharpe(hold), 3),
        "max_dd_pct": round(max_drawdown_pct(hold), 2),
        "calmar": round(calmar(hold), 3),
        "cvar5_pct": round(cvar_pct(hold), 3),
        "mean_net_exposure": round(float(np.mean(net_exp[cut:])), 3),
    }

    # null: random dollar-neutral baskets on the same fire dates, same regime &
    # tilt machinery (so the null also carries whatever directional bet the tilt
    # imposes — isolating the MOMENTUM RANKING contribution, not the tilt's beta).
    rng = np.random.default_rng(seed)
    full_null, hold_null = [], []
    for _ in range(reps):
        pr, _, _ = simulate(closes, cfg, selection="random", rng=rng, s_override=s)
        full_null.append(_total_return_pct(pr))
        hold_null.append(_total_return_pct(pr[cut:]))
    full_null = np.array(full_null)
    hold_null = np.array(hold_null)
    full["null_pct"] = round(float(np.mean(full_null < full["net_pct"]) * 100), 1)
    hold_m["null_pct"] = round(float(np.mean(hold_null < hold_m["net_pct"]) * 100), 1)

    # sham: shuffled ranking -> must collapse into the null (NOT clear the gate)
    sham_pcts: List[float] = []
    for k in range(n_sham):
        sh, _, _ = simulate(closes, cfg, selection="shuffle",
                            rng=np.random.default_rng(seed + 101 + k), s_override=s)
        sham_pcts.append(round(float(np.mean(full_null < _total_return_pct(sh)) * 100), 1))
    sham_passes = sum(1 for sp in sham_pcts if sp > 95.0)
    sham_failing = sham_passes < (n_sham // 2 + 1)

    return {
        "tau": cfg.tau, "regime_w": cfg.regime_w,
        "flip_neutral": cfg.flip_neutral, "trail_stop": cfg.trail_stop,
        "arm_pct": cfg.arm_pct, "trail_pct": cfg.trail_pct,
        "n_rebal": len(fires),
        "full": full, "holdout": hold_m,
        "sham_percentiles": sham_pcts, "sham_fails": bool(sham_failing),
    }
