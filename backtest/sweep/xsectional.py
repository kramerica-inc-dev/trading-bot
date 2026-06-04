"""Cross-sectional momentum candidate (market-neutral) — sweep wave 1's real bet.

Unlike the single-asset directional family (all dead), this is dollar-neutral:
each rebalance it ranks the OKX-perp universe by trailing return, longs the
top-m and shorts the bottom-m in equal weight. Market-neutral by construction,
so it's a genuinely different bet than the directional lanes that lost to BTC
beta and friction.

Lane-specific null (per the V2.1 discipline): the time-series random-entry null
doesn't apply to a market-neutral basket. Instead the null is **random asset
selection with matched turnover** — every rebalance pick m random longs + m
random shorts. That isolates whether the *momentum ranking* adds value beyond a
random dollar-neutral basket. PASS = observed total return above the 95th
percentile of the random-selection null.

Sham: shuffle the signal across assets (random ranking) — which collapses to the
random-selection null, so it must NOT clear the gate.

Runs on the OKX daily panel (backtest/data/okx/<ASSET>_1Dutc.csv).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from scipy.stats import ttest_1samp

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from sweep_feasibility import FeasibilityVerdict, decide_verdict  # noqa: E402

DEFAULT_ASSETS = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT",
                  "DOGE-USDT", "ADA-USDT", "AVAX-USDT", "DOT-USDT", "LINK-USDT"]
OKX_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "okx")


@dataclass
class XSConfig:
    lookback: int = 90        # trailing-return ranking window (days)
    rebal: int = 5            # rebalance every N days
    m: int = 3                # longs = top m, shorts = bottom m
    cost_rate: float = 0.0015  # per unit of |Δweight| traded (taker + slippage)
    bar: str = "1Dutc"


def load_panel(assets: List[str], *, data_dir: str = OKX_DATA, bar: str = "1Dutc") -> pd.DataFrame:
    """Aligned close panel (inner-join on common dates)."""
    series: Dict[str, pd.Series] = {}
    for a in assets:
        p = os.path.join(data_dir, f"{a}_{bar}.csv")
        if not os.path.exists(p):
            continue
        d = pd.read_csv(p)
        d["timestamp"] = pd.to_datetime(d["timestamp"], utc=True, format="ISO8601")
        series[a] = d.set_index("timestamp")["close"].astype(float)
    if not series:
        return pd.DataFrame()
    return pd.DataFrame(series).dropna()


def load_funding_panel(dates: pd.DatetimeIndex, assets: List[str], *,
                       data_dir: str = OKX_DATA) -> np.ndarray:
    """Per-asset daily funding aligned to `dates` (sum of 8h settlements per UTC
    day; 0 where unavailable). Column order matches `assets`. Row t = funding
    over day t. OKX public funding is ~3 months, so most of a multi-year panel
    is 0 — use only for the recent-window realistic estimate, not full history.
    """
    cols = []
    for a in assets:
        p = os.path.join(data_dir, f"funding_{a}.csv")
        if not os.path.exists(p):
            cols.append(pd.Series(0.0, index=dates))
            continue
        d = pd.read_csv(p)
        d["timestamp"] = pd.to_datetime(d["timestamp"], utc=True, format="ISO8601")
        daily = d.groupby(d["timestamp"].dt.floor("D"))["funding_rate"].sum()
        cols.append(daily.reindex(dates).fillna(0.0))
    return np.column_stack([c.to_numpy() for c in cols])


def _portfolio_returns(closes: np.ndarray, cfg: XSConfig, *, selection: str,
                       rng: Optional[np.random.Generator] = None,
                       funding_panel: Optional[np.ndarray] = None,
                       flat_drag_daily: float = 0.0) -> np.ndarray:
    """Daily dollar-neutral portfolio returns for a selection rule.

    selection: 'momentum' (rank by trailing return), 'random' (random baskets),
    'shuffle' (rank a shuffled trailing-return vector == random ranking).

    funding_panel: optional (n, k) array of per-asset daily funding (row t =
        funding over day t->t+1, positive == longs pay). A long position pays
        and a short receives, so funding PnL_t = -sum(weights * funding_panel[t]).
    flat_drag_daily: optional flat funding-headwind stress applied to the gross
        book each day (cost = drag * sum|weights|).
    """
    n, k = closes.shape
    rets = closes[1:] / closes[:-1] - 1.0       # (n-1, k)
    weights = np.zeros(k)
    out = np.empty(n - 1)
    m = cfg.m
    for t in range(n - 1):
        cost = 0.0
        if t % cfg.rebal == 0 and t >= cfg.lookback:
            trail = closes[t] / closes[t - cfg.lookback] - 1.0
            if selection == "momentum":
                order = np.argsort(trail)
            elif selection == "random":
                order = rng.permutation(k)
            elif selection == "shuffle":
                order = np.argsort(rng.permutation(trail))
            else:
                raise ValueError(selection)
            new_w = np.zeros(k)
            new_w[order[-m:]] = 1.0 / m          # longs (top)
            new_w[order[:m]] = -1.0 / m          # shorts (bottom)
            cost = np.abs(new_w - weights).sum() * cfg.cost_rate
            weights = new_w
        fpnl = -float(np.dot(weights, funding_panel[t])) if funding_panel is not None else 0.0
        drag = flat_drag_daily * float(np.abs(weights).sum())
        out[t] = float((weights * rets[t]).sum() - cost + fpnl - drag)
    return out


def _total_return_pct(port: np.ndarray) -> float:
    eq = np.cumprod(1.0 + port)
    return float((eq[-1] - 1.0) * 100.0) if len(eq) else 0.0


def _sharpe(port: np.ndarray) -> float:
    sd = float(np.std(port, ddof=1))
    return float(np.mean(port) / sd * np.sqrt(365.0)) if sd > 0 else 0.0


def _cross_sectional_ic(closes: np.ndarray, cfg: XSConfig) -> tuple[float, float]:
    """Mean per-rebalance Spearman IC between trailing return and the forward
    `rebal`-day return across assets, with a t-test of the per-date ICs vs 0."""
    from scipy.stats import spearmanr
    n, k = closes.shape
    ics: List[float] = []
    for t in range(cfg.lookback, n - cfg.rebal, cfg.rebal):
        trail = closes[t] / closes[t - cfg.lookback] - 1.0
        fwd = closes[t + cfg.rebal] / closes[t] - 1.0
        if np.all(np.isfinite(trail)) and np.all(np.isfinite(fwd)):
            rho, _ = spearmanr(trail, fwd)
            if np.isfinite(rho):
                ics.append(float(rho))
    if len(ics) < 5:
        return float("nan"), float("nan")
    t_stat, p = ttest_1samp(ics, 0.0)
    return float(np.mean(ics)), float(p)


def run(cfg: Optional[XSConfig] = None, *, assets: Optional[List[str]] = None,
        reps: int = 500, n_sham: int = 3, seed: int = 20260604,
        data_dir: str = OKX_DATA) -> FeasibilityVerdict:
    cfg = cfg or XSConfig()
    assets = assets or DEFAULT_ASSETS
    panel = load_panel(assets, data_dir=data_dir, bar=cfg.bar)
    name = f"xsec_momentum_{cfg.lookback}d_top{cfg.m}"
    thesis = ("cross-sectional momentum: long top-%d / short bottom-%d of %d OKX "
              "perps by trailing %dd return, dollar-neutral, rebalanced %dd"
              % (cfg.m, cfg.m, len(assets), cfg.lookback, cfg.rebal))

    if panel.empty or panel.shape[1] < 2 * cfg.m or len(panel) < cfg.lookback + 2 * cfg.rebal:
        return FeasibilityVerdict(name=name, verdict="KILL",
                                  reasons=["insufficient OKX panel data — run backtest.okx_backfill"],
                                  thesis=thesis)

    closes = panel.to_numpy()
    obs = _portfolio_returns(closes, cfg, selection="momentum")
    obs_ret = _total_return_pct(obs)
    obs_sharpe = _sharpe(obs)

    # cost-floor: gross (no turnover cost) vs net
    gross_cfg = XSConfig(**{**cfg.__dict__, "cost_rate": 0.0})
    gross = _total_return_pct(_portfolio_returns(closes, gross_cfg, selection="momentum"))
    cost_share = (gross - obs_ret) / max(abs(gross), 1e-9)
    c1 = (obs_ret > 0.0) and (cost_share < 0.60)

    # null: random asset selection, matched turnover
    rng = np.random.default_rng(seed)
    null_rets = np.array([_total_return_pct(_portfolio_returns(closes, cfg, selection="random", rng=rng))
                          for _ in range(reps)])
    pct = float(np.mean(null_rets < obs_ret) * 100.0)
    c2 = pct > 95.0

    # cross-sectional IC
    ic, ic_p = _cross_sectional_ic(closes, cfg)
    c3 = bool(np.isfinite(ic) and abs(ic) > 0.03 and ic_p < 0.05 and ic > 0)

    # sham: shuffled ranking should collapse to the random null
    sham_pcts: List[float] = []
    for k in range(n_sham):
        sham = _total_return_pct(_portfolio_returns(closes, cfg, selection="shuffle",
                                                    rng=np.random.default_rng(seed + 100 + k)))
        sham_pcts.append(float(np.mean(null_rets < sham) * 100.0))
    sham_passes = sum(1 for sp in sham_pcts if sp > 95.0)
    sham_failing = sham_passes < (n_sham // 2 + 1)

    verdict = decide_verdict(c1, c2, c3, sham_failing)
    reasons: List[str] = []
    if verdict == "VOID":
        reasons.append(f"sham passed ({sham_passes}/{n_sham}) — random ranking also "
                       "clears the null; gate broken")
    elif verdict == "KILL":
        if not c1:
            reasons.append(f"cost: net={obs_ret:.1f}% cost_share={cost_share:.2f}")
        if not c2:
            reasons.append(f"null: {pct:.0f}th pct of random-basket null (needs >95)")
        if not c3:
            reasons.append(f"IC: mean_xs_ic={ic:.3f} p={ic_p:.3f} (needs >0.03, p<0.05)")

    return FeasibilityVerdict(
        name=name, verdict=verdict,
        checks={"cost_floor": bool(c1), "null_gate": bool(c2),
                "signal_ic": bool(c3), "sham_fails": bool(sham_failing)},
        metrics={
            "net_return_pct": round(obs_ret, 2),
            "gross_return_pct": round(gross, 2),
            "cost_share": round(float(cost_share), 3),
            "sharpe": round(obs_sharpe, 3),
            "null_percentile": round(pct, 1),
            "null_p95_return_pct": round(float(np.percentile(null_rets, 95)), 2),
            "xs_ic_mean": (round(ic, 4) if np.isfinite(ic) else None),
            "xs_ic_p": (round(ic_p, 4) if np.isfinite(ic_p) else None),
            "n_assets": int(panel.shape[1]),
            "n_days": int(len(panel)),
            "sham_percentiles": [round(s, 1) for s in sham_pcts],
        },
        reasons=reasons, thesis=thesis,
    )


if __name__ == "__main__":
    v = run(reps=300)
    print(v.verdict, v.name, v.metrics, v.reasons)
