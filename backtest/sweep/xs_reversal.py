"""Cross-sectional REVERSAL basket with k_exit hysteresis — Plan E's edge gate.

Plan E is the live market-neutral lane's REVERSAL cousin: each daily rebalance it
ranks the 10 OKX perps by their trailing 72h LOG-return, flips the sign (sign=-1),
and goes LONG the laggers (lowest past return) / SHORT the leaders (highest), top-3 /
bottom-3, dollar-neutral, with STATEFUL k_exit=6 hysteresis (a held name is kept as
long as it stays inside the top/bottom-k band, only swapped out when it drifts past
k). It was deployed (8 SL variants + a trailing winner) but **was never null-gated** —
this module asks whether the reversal ranking itself clears a proper cross-sectional
random-basket null OUT-OF-SAMPLE, and whether its daily return stream is correlated
with the live cross-sectional MOMENTUM lane (lb=120/rebal=5/m=3, sign=+1, no exit).

This is a faithful CLONE of `backtest/sweep/xs_sentiment_tilt.py`'s continuous-book-
carry engine (gross-renorm, cost on |dweight|, funding drag, momentum/random/shuffle
selection, `_assert_no_lookahead` poison-test, max_drawdown_pct/calmar/cvar_pct,
`evaluate()` with full + continuous-OOS + null + sham) MINUS the asymmetric tilt,
PLUS:
  * a REVERSAL sign flip (sign=-1: rank by -trailing-return so laggers sort to the
    top of `order` and get the longs);
  * a stateful k_exit HYSTERESIS selector ported from the LIVE runner's
    `select_positions` band-keep logic into index space (held names are retained
    while inside the keep band, reducing turnover).

`sign=+1` + `k_exit=None` recovers the plain momentum book — the MOMENTUM-ANCHOR
configuration used as the trust gate (must reproduce the known momentum null
~98-100th, cf. XS-BREADTH 100 / XS-TRIGGER 98.5). If it does NOT, the clone is wrong.

Reused from `xsectional`: load_panel, DEFAULT_ASSETS, _total_return_pct, _sharpe,
_cross_sectional_ic. Verdict via sweep_feasibility.decide_verdict.
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
    load_panel, DEFAULT_ASSETS, _total_return_pct, _sharpe, _cross_sectional_ic,
    XSConfig,
)

# Cost: the SAME cost_rate as xsectional (conservative 15bps / |dweight|).
COST_RATE = 0.0015
# Flat funding drag on gross (HL-calibrated ~6%/yr, 1.65bps/day) — the same stress
# the trigger/beta/tilt studies applied. Set to 0.0 for the zero-cost control.
FUNDING = 0.000165
TARGET_GROSS = 2.0            # sum|w| of a fresh +-1/m book (m longs + m shorts)


@dataclass
class RevConfig:
    lookback: int = 3            # trailing-return ranking window (DAYS; 72h ~ 3d)
    rebal: int = 1               # rebalance cadence (days); Plan E rebals daily
    m: int = 3                   # longs = top m of `order`, shorts = bottom m
    reversal: bool = True        # True -> sign=-1 (REV); False -> sign=+1 (MOM anchor)
    k_exit: Optional[int] = 6    # hysteresis band width; None -> no hysteresis (pure top/bottom-m)
    cost_rate: float = COST_RATE
    funding: float = FUNDING

    @property
    def sign(self) -> int:
        return -1 if self.reversal else 1


# ---------------------------------------------------------------------------
# Signed trailing-return signal (NO LOOKAHEAD by construction)
# ---------------------------------------------------------------------------

def signal_at(closes: np.ndarray, t: int, lookback: int, sign: int) -> np.ndarray:
    """Signed trailing LOG-return over `lookback` days ending at the close of day t.

    sign=-1 -> reversal (laggers score high), sign=+1 -> momentum (leaders score
    high). Mirrors the LIVE runner's `compute_signal` (sign * log(latest/past))
    but on the daily panel where closes[t] is the decision-day close (the book is
    then set at t and earns over t->t+1 in row t of `rets`). Uses ONLY closes[:t+1].
    """
    cur = closes[t]
    past = closes[t - lookback]
    out = np.full(cur.shape, np.nan)
    ok = np.isfinite(cur) & np.isfinite(past) & (cur > 0) & (past > 0)
    out[ok] = sign * np.log(cur[ok] / past[ok])
    return out


# ---------------------------------------------------------------------------
# k_exit hysteresis (port of the LIVE runner's select_positions to INDEX space)
# ---------------------------------------------------------------------------

def select_indices(
    ranked: np.ndarray, cur_longs: set, cur_shorts: set,
    m: int, k_exit: int,
) -> Tuple[List[int], List[int]]:
    """Stateful band-keep selection, ported from scripts/plan_e_runner.select_positions.

    `ranked` is the asset indices sorted by signal DESCENDING (best == ranked[0]).
    Longs come from the TOP of the ranking, shorts from the BOTTOM — the same
    orientation as the live runner (which sorts signals descending, then longs the
    head and shorts the tail). A currently-held long is retained if it is still in
    the top-`k_exit` band; a held short if still in the bottom-`k_exit` band. The
    remaining slots are filled greedily from the head (longs) / tail (shorts), and
    a name cannot be both long and short.
    """
    keep_long_band = set(ranked[:k_exit].tolist())
    keep_short_band = set(ranked[-k_exit:].tolist())
    ranked_list = ranked.tolist()

    new_longs: List[int] = [s for s in ranked_list if s in cur_longs and s in keep_long_band]
    for s in ranked_list:
        if len(new_longs) >= m:
            break
        if s not in new_longs:
            new_longs.append(s)
    new_longs = new_longs[:m]

    new_shorts: List[int] = [s for s in reversed(ranked_list)
                             if s in cur_shorts and s in keep_short_band]
    for s in reversed(ranked_list):
        if len(new_shorts) >= m:
            break
        if s not in new_shorts:
            new_shorts.append(s)
    new_shorts = new_shorts[:m]

    long_set = set(new_longs)
    new_shorts = [s for s in new_shorts if s not in long_set]
    return new_longs, new_shorts


def _weights_from_legs(longs: List[int], shorts: List[int], k: int) -> np.ndarray:
    """Equal-weight dollar-neutral book gross-renormalized to TARGET_GROSS."""
    w = np.zeros(k)
    if longs:
        w[longs] = 1.0 / len(longs)
    if shorts:
        w[shorts] = -1.0 / len(shorts)
    gross = float(np.abs(w).sum())
    if gross > 1e-12:
        w *= TARGET_GROSS / gross
    return w


# ---------------------------------------------------------------------------
# Lookahead poison-test (ranking + hysteresis state)
# ---------------------------------------------------------------------------

def _assert_no_lookahead(closes: np.ndarray, cfg: RevConfig) -> None:
    """Recompute the daily portfolio two ways and assert that poisoning closes[t:]
    to +inf never changes any portfolio return on day < t. This guards BOTH the
    ranking AND the stateful hysteresis path (a subtle lookahead in carried state
    would change earlier returns when the future is poisoned)."""
    port_ref, _, _ = simulate(closes, cfg, selection="momentum")
    n = closes.shape[0]
    for t in (cfg.lookback + cfg.rebal + 2, n // 2, n - 1):
        if not (1 <= t < n):
            continue
        cc = closes.copy()
        cc[t:] = np.inf                 # poison the present + future closes
        port_p, _, _ = simulate(cc, cfg, selection="momentum")
        # rets row j == day j->j+1 uses closes[j] and closes[j+1]; row t-1 is the
        # last row using only un-poisoned closes[:t], so check returns < t-1.
        horizon = max(0, t - 1)
        if not np.allclose(port_ref[:horizon], port_p[:horizon], atol=1e-12,
                           equal_nan=True):
            raise AssertionError(
                f"LOOKAHEAD: portfolio return < t={t} changed when closes[t:] were "
                "poisoned — ranking or hysteresis state depends on close[t] or later.")


# ---------------------------------------------------------------------------
# Continuous-book-carry simulation (clone of xs_sentiment_tilt.simulate, no tilt)
# ---------------------------------------------------------------------------

def simulate(closes: np.ndarray, cfg: RevConfig, *, selection: str = "momentum",
             rng: Optional[np.random.Generator] = None,
             ) -> Tuple[np.ndarray, np.ndarray, List[int]]:
    """Daily dollar-neutral portfolio returns with the book carried across
    rebalances (weights drift with realized returns between fires), same convention
    as the validated trigger/tilt studies.

    selection: 'momentum' (rank by the signed trailing return == the strategy),
    'random' (random dollar-neutral baskets, matched fire dates, hysteresis applied
    in index space the same way), 'shuffle' (rank a shuffled signal == random
    ranking). Reversal vs momentum is set by cfg.reversal (the SIGN); selection
    only governs WHICH ranking feeds the (shared) hysteresis+weight machinery.

    Returns (daily_port[n-1], gross[n-1], fire_dates).
    """
    n, k = closes.shape
    rets = closes[1:] / closes[:-1] - 1.0          # (n-1, k); row t == day t->t+1

    w = np.zeros(k)
    out = np.empty(n - 1)
    gross_series = np.empty(n - 1)
    fires: List[int] = []
    cur_longs: set = set()
    cur_shorts: set = set()

    for t in range(n - 1):
        cost = 0.0
        if t % cfg.rebal == 0 and t >= cfg.lookback:
            sig = signal_at(closes, t, cfg.lookback, cfg.sign)
            if not np.all(np.isfinite(sig)):
                # carry on a non-finite signal day (mirrors live runner skipping)
                pass
            else:
                if selection == "momentum":
                    order = np.argsort(-sig)         # descending: best signal first
                elif selection == "random":
                    order = rng.permutation(k)
                elif selection == "shuffle":
                    order = np.argsort(-rng.permutation(sig))
                else:
                    raise ValueError(selection)
                if cfg.k_exit is not None:
                    longs, shorts = select_indices(order, cur_longs, cur_shorts,
                                                   cfg.m, cfg.k_exit)
                else:
                    longs = order[:cfg.m].tolist()
                    shorts = order[-cfg.m:].tolist()
                    shorts = [s for s in shorts if s not in set(longs)]
                nw = _weights_from_legs(longs, shorts, k)
                cost = float(np.abs(nw - w).sum()) * cfg.cost_rate
                w = nw
                cur_longs = set(longs)
                cur_shorts = set(shorts)
                fires.append(t)

        drag = cfg.funding * float(np.abs(w).sum())
        port = float((w * rets[t]).sum() - cost - drag)
        out[t] = port
        gross_series[t] = float(np.abs(w).sum())

        # carry the book: weights drift with realized returns between rebalances
        denom = 1.0 + port
        w = w * (1.0 + rets[t]) / (denom if abs(denom) > 1e-9 else 1e-9)

    return out, gross_series, fires


# ---------------------------------------------------------------------------
# Risk metrics (identical to xs_sentiment_tilt)
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
# Reversal IC (signed trailing return vs forward rebal-day return)
# ---------------------------------------------------------------------------

def reversal_ic(closes: np.ndarray, cfg: RevConfig) -> Tuple[float, float]:
    """Mean per-rebalance Spearman IC between the SIGNED trailing return (the
    strategy signal) and the forward `rebal`-day return across assets, t-tested vs 0.

    For a reversal edge we EXPECT a POSITIVE IC of the signed signal (laggers ->
    high score -> high forward return), equivalently a NEGATIVE raw momentum IC.
    Reuses xsectional._cross_sectional_ic on the RAW trailing return then flips the
    sign for the signed reporting, so the magnitude/p-value match the canonical IC.
    """
    xs_cfg = XSConfig(lookback=cfg.lookback, rebal=cfg.rebal, m=cfg.m,
                      cost_rate=cfg.cost_rate)
    raw_ic, p = _cross_sectional_ic(closes, xs_cfg)   # IC of RAW (momentum) trailing
    signed_ic = cfg.sign * raw_ic                      # signal = sign * trailing
    return signed_ic, p


# ---------------------------------------------------------------------------
# Evaluation: full-sample + continuous OOS holdout + null + sham + zero-cost
# ---------------------------------------------------------------------------

def _metrics(port: np.ndarray) -> Dict:
    return {
        "net_pct": round(_total_return_pct(port), 2),
        "sharpe": round(_sharpe(port), 3),
        "max_dd_pct": round(max_drawdown_pct(port), 2),
        "calmar": round(calmar(port), 3),
        "cvar5_pct": round(cvar_pct(port), 3),
    }


def evaluate(closes: np.ndarray, cfg: RevConfig, *, cut: int, reps: int = 400,
             n_sham: int = 3, seed: int = 20260606,
             regime_cut: Optional[int] = None) -> Dict:
    """Full-sample + continuous-holdout obs vs the cross-sectional random-basket
    null (matched fire dates + SAME hysteresis machinery), plus the shuffle sham,
    a zero-cost control, the reversal IC, and a 2-way regime split."""
    port, gross, fires = simulate(closes, cfg, selection="momentum")

    hold = port[cut:]
    full = _metrics(port)
    hold_m = _metrics(hold)

    # zero-cost control (cost_rate=0, funding=0) — isolates the gross edge
    zcfg = RevConfig(lookback=cfg.lookback, rebal=cfg.rebal, m=cfg.m,
                     reversal=cfg.reversal, k_exit=cfg.k_exit,
                     cost_rate=0.0, funding=0.0)
    zport, _, _ = simulate(closes, zcfg, selection="momentum")
    zero_cost = {"full_net_pct": round(_total_return_pct(zport), 2),
                 "oos_net_pct": round(_total_return_pct(zport[cut:]), 2)}

    # null: random dollar-neutral baskets on the same fire dates, SAME hysteresis.
    rng = np.random.default_rng(seed)
    full_null, hold_null = [], []
    for _ in range(reps):
        pr, _, _ = simulate(closes, cfg, selection="random", rng=rng)
        full_null.append(_total_return_pct(pr))
        hold_null.append(_total_return_pct(pr[cut:]))
    full_null = np.array(full_null)
    hold_null = np.array(hold_null)
    full["null_pct"] = round(float(np.mean(full_null < full["net_pct"]) * 100), 1)
    hold_m["null_pct"] = round(float(np.mean(hold_null < hold_m["net_pct"]) * 100), 1)
    full["null_p95_pct"] = round(float(np.percentile(full_null, 95)), 2)
    hold_m["null_p95_pct"] = round(float(np.percentile(hold_null, 95)), 2)

    # sham: shuffled ranking -> must collapse into the null (NOT clear the gate)
    sham_pcts: List[float] = []
    for j in range(n_sham):
        sh, _, _ = simulate(closes, cfg, selection="shuffle",
                            rng=np.random.default_rng(seed + 101 + j))
        sham_pcts.append(round(float(np.mean(full_null < _total_return_pct(sh)) * 100), 1))
    sham_passes = sum(1 for sp in sham_pcts if sp > 95.0)
    sham_failing = sham_passes < (n_sham // 2 + 1)

    # reversal IC (signed signal vs forward return)
    ic, ic_p = reversal_ic(closes, cfg)

    # regime split: first vs second half of the TRADED region (lookback:)
    rc = regime_cut if regime_cut is not None else (cfg.lookback + (len(port) - cfg.lookback) // 2)
    regime = {
        "split_idx": int(rc),
        "first": _metrics(port[cfg.lookback:rc]),
        "second": _metrics(port[rc:]),
    }

    return {
        "spec": {"lookback": cfg.lookback, "rebal": cfg.rebal, "m": cfg.m,
                 "reversal": cfg.reversal, "sign": cfg.sign, "k_exit": cfg.k_exit,
                 "cost_rate": cfg.cost_rate, "funding": cfg.funding},
        "n_rebal": len(fires),
        "mean_gross": round(float(np.mean(gross)), 3),
        "full": full, "holdout": hold_m,
        "zero_cost": zero_cost,
        "reversal_ic": (round(float(ic), 4) if np.isfinite(ic) else None),
        "reversal_ic_p": (round(float(ic_p), 4) if np.isfinite(ic_p) else None),
        "sham_percentiles": sham_pcts, "sham_fails": bool(sham_failing),
        "regime_split": regime,
    }
