#!/usr/bin/env python3
"""Beta-decomposition of the cross-sectional momentum basket.

Follows up the dollar-neutral != beta-neutral critique: a dollar-neutral long/short
basket still carries residual BTC-beta exposure if the legs have unequal aggregate
beta. For cross-sectional momentum this is NOT random — momentum longs recent
winners (in a bull, the high-beta leaders -> net LONG beta) and shorts recent
losers (in a bull, low-beta laggards), so the residual beta is PRO-CYCLICAL
(net-long-beta in up-markets, net-short in down-markets). That is plausibly a big
part of why the edge concentrated in the 2023 bull (docs/XS-TRIGGER-STUDY.md).

This measures it (don't guess — regress), three ways:
  1. HIDDEN BETA. Rolling 90d beta of each asset vs BTC; the strategy's realized
     net beta over time (sign, magnitude, correlation with the BTC trend).
  2. P&L DECOMPOSITION. Split the dollar-neutral return into a beta-driven part
     (net_beta_t * BTC_return_t) and an idiosyncratic residual. How much of the
     edge was disguised market timing?
  3. BETA-NEUTRAL VARIANT. Reweight each rebalance so Sigma(long w*beta) =
     Sigma(short w*beta) (equal-within-leg, leg-scaled, gross held at 2). Run it
     through the SAME null / continuous-OOS / walk-forward harness as the trigger
     study. Does the pure idiosyncratic momentum edge still clear the null,
     especially OUTSIDE the 2023 bull?

OKX 3.5y daily panel; funding = HL-calibrated ~6%/yr flat proxy. Writes
backtest/results/sweep/xs_beta.json.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "backtest"))
from sweep.xsectional import load_panel, DEFAULT_ASSETS  # noqa: E402

RESULTS = PROJECT_ROOT / "backtest" / "results" / "sweep"
HL_COST = 0.00045
FUNDING = 0.000165
BETA_WIN = 90
MKT = 0                # BTC-USDT is index 0 in DEFAULT_ASSETS (the market factor)
TARGET_GROSS = 2.0


def rolling_beta(rets, *, win=BETA_WIN, mkt=MKT):
    """Causal rolling beta of each asset vs the market column. beta_t uses returns
    [t-win, t) (strictly past), so it's tradable at t. Row 0..win-1 default to 1.0."""
    nm1, k = rets.shape
    betas = np.ones((nm1, k))
    mr = rets[:, mkt]
    for t in range(win, nm1):
        m = mr[t - win:t]
        mv = float(np.var(m, ddof=1))           # match cov ddof=1 (else betas x win/(win-1))
        if mv <= 1e-12:
            betas[t] = betas[t - 1]; continue
        for j in range(k):
            betas[t, j] = float(np.cov(rets[t - win:t, j], m, ddof=1)[0, 1] / mv)
    return betas


def _weights(longs, shorts, m, beta_now, mode):
    k = len(beta_now); w = np.zeros(k)
    if mode == "dollar":
        w[longs] = 1.0 / m; w[shorts] = -1.0 / m
    else:  # beta-neutral: scale legs on the RAW leg betas so realized net beta is
        # structurally ~0 (not data-contingent on a clip never firing)
        bl = float(beta_now[longs].mean()); bs = float(beta_now[shorts].mean())
        if bl > 1e-6 and bs > 1e-6:
            a = 2.0 * bs / (bl + bs); b = 2.0 * bl / (bl + bs)
            w[longs] = a / m; w[shorts] = -b / m
        else:                                    # non-positive leg beta -> fall back to dollar-neutral
            w[longs] = 1.0 / m; w[shorts] = -1.0 / m
    return w


def simulate(closes, betas, *, lookback, m, cost_rate, cadence, mode,
             selection="momentum", rng=None, forced_dates=None, flat_drag=0.0):
    """Drift-aware sim that also tracks realized net beta + beta-driven PnL."""
    n, k = closes.shape
    rets = closes[1:] / closes[:-1] - 1.0
    w = np.zeros(k); last = -10**9
    out = np.empty(n - 1); net_beta = np.zeros(n - 1); beta_pnl = np.zeros(n - 1)
    turnover = 0.0; fires = []
    forced = set(forced_dates) if forced_dates is not None else None
    for t in range(n - 1):
        cost = 0.0
        fire = (t in forced) if forced is not None else (t >= lookback and (t - last) >= cadence)
        if fire and t >= lookback:
            trail = closes[t] / closes[t - lookback] - 1.0
            order = (np.argsort(trail) if selection == "momentum" else rng.permutation(k))
            longs, shorts = order[-m:], order[:m]
            nw = _weights(longs, shorts, m, betas[t], mode)
            d = float(np.abs(nw - w).sum()); cost = d * cost_rate; turnover += d
            w = nw; last = t; fires.append(t)
        nb = float(np.dot(w, betas[t]))                 # realized net beta today
        net_beta[t] = nb
        beta_pnl[t] = nb * rets[t, MKT]                 # market-explained gross PnL
        drag = flat_drag * float(np.abs(w).sum())
        port = float((w * rets[t]).sum() - cost - drag)
        out[t] = port
        denom = 1.0 + port
        w = w * (1.0 + rets[t]) / (denom if abs(denom) > 1e-9 else 1e-9)
    return out, fires, turnover, net_beta, beta_pnl


def _ret(p):
    return float((np.cumprod(1.0 + p)[-1] - 1.0) * 100.0) if len(p) else 0.0


def evaluate(closes, betas, *, mode, cost, reps, seed, cut, cadence=5, lookback=120, m=3):
    kw = dict(lookback=lookback, m=m, cost_rate=cost, cadence=cadence, mode=mode, flat_drag=FUNDING)
    port, fires, turn, nb, bp = simulate(closes, betas, selection="momentum", **kw)
    full, hold = _ret(port), _ret(port[cut:])
    rng = np.random.default_rng(seed); fn, hn = [], []
    for _ in range(reps):
        pr = simulate(closes, betas, selection="random", rng=rng, forced_dates=fires, **kw)[0]
        fn.append(_ret(pr)); hn.append(_ret(pr[cut:]))
    return {"net_pct": round(full, 1), "null_pct": round(float(np.mean(np.array(fn) < full) * 100), 1),
            "hold_net_pct": round(hold, 1), "hold_null_pct": round(float(np.mean(np.array(hn) < hold) * 100), 1),
            "turnover_per_yr": round(turn / (len(closes) / 365.0), 1),
            "_port": port, "_nb": nb, "_bp": bp}


def walk_forward(closes, betas, *, mode, cost, reps, cadence=5, lookback=120, m=3):
    n = len(closes); b = np.linspace(0, n, 5).astype(int); rows = []
    for i in range(4):
        cs, bs = closes[b[i]:b[i + 1]], betas[b[i]:b[i + 1]]
        if len(cs) < lookback + 20:
            continue
        kw = dict(lookback=lookback, m=m, cost_rate=cost, cadence=cadence, mode=mode, flat_drag=FUNDING)
        port, fires, _, _, _ = simulate(cs, bs, selection="momentum", **kw)
        obs = _ret(port); rng = np.random.default_rng(60 + i)
        null = np.array([_ret(simulate(cs, bs, selection="random", rng=rng, forced_dates=fires, **kw)[0]) for _ in range(reps)])
        rows.append({"win": i + 1, "net_pct": round(obs, 1), "null_pct": round(float(np.mean(null < obs) * 100), 1)})
    return rows


def main() -> int:
    panel = load_panel(DEFAULT_ASSETS)
    if panel.empty:
        print("No OKX panel — run: python -m backtest.okx_backfill --bar 1Dutc"); return 1
    closes = panel.to_numpy(); n = len(panel); cut = int(n * 0.70)
    rets = closes[1:] / closes[:-1] - 1.0
    betas = rolling_beta(rets)
    out = {"n_days": n, "n_assets": int(panel.shape[1]), "beta_win": BETA_WIN,
           "market": DEFAULT_ASSETS[MKT], "assets": list(panel.columns)}

    # 1. HIDDEN BETA of the dollar-neutral momentum strategy
    dn = evaluate(closes, betas, mode="dollar", cost=HL_COST, reps=200, seed=20260604, cut=cut)
    nb = dn["_nb"]
    btc_trail = np.array([closes[t, MKT] / closes[max(t - 90, 0), MKT] - 1.0 for t in range(len(nb))])
    valid = np.arange(len(nb)) >= 120
    corr_nb_trend = float(np.corrcoef(nb[valid], btc_trail[valid])[0, 1])
    out["hidden_beta"] = {
        "mean_net_beta": round(float(np.mean(nb[valid])), 3),
        "std_net_beta": round(float(np.std(nb[valid])), 3),
        "min_net_beta": round(float(np.min(nb[valid])), 3),
        "max_net_beta": round(float(np.max(nb[valid])), 3),
        "frac_days_net_long_beta": round(float(np.mean(nb[valid] > 0)), 3),
        "corr_netbeta_vs_btc_90d_trend": round(corr_nb_trend, 3),
        "note": "corr>0 => pro-cyclical: net-LONG beta when BTC has been rising, net-SHORT when falling",
    }

    # 2. P&L DECOMPOSITION — GROSS/GROSS (no cost) so numerator and denominator share a base,
    #    split by regime (the beta exposure is heavily time-concentrated, not a flat 20%).
    gport, _, _, _, gbp = simulate(closes, betas, lookback=120, m=3, cost_rate=0.0,
                                   cadence=5, mode="dollar", flat_drag=0.0)

    def _share(num, den):
        s = float(np.sum(den))
        return round(float(np.sum(num)) / s, 3) if abs(s) > 1e-6 else None
    out["pnl_decomposition"] = {
        "basis": "arithmetic daily sums, GROSS (cost/funding excluded); beta_ret_t = net_beta_t * BTC_ret_t",
        "sum_total_gross": round(float(np.sum(gport)), 4),
        "sum_beta_gross": round(float(np.sum(gbp)), 4),
        "beta_share_full": _share(gbp, gport),
        "beta_share_train_2023bull": _share(gbp[:cut], gport[:cut]),
        "beta_share_holdout_2024_25": _share(gbp[cut:], gport[cut:]),
        "note": "share is a whole-sample average of a TIME-CONCENTRATED exposure — see the regime split",
    }

    # 3. BETA-NEUTRAL variant vs DOLLAR-NEUTRAL through the same harness
    bn = evaluate(closes, betas, mode="beta", cost=HL_COST, reps=200, seed=20260604, cut=cut)
    out["compare"] = {
        "dollar_neutral": {k: dn[k] for k in ("net_pct", "null_pct", "hold_net_pct", "hold_null_pct", "turnover_per_yr")},
        "beta_neutral":   {k: bn[k] for k in ("net_pct", "null_pct", "hold_net_pct", "hold_null_pct", "turnover_per_yr")},
    }
    out["walk_forward"] = {
        "dollar_neutral": walk_forward(closes, betas, mode="dollar", cost=HL_COST, reps=200),
        "beta_neutral":   walk_forward(closes, betas, mode="beta", cost=HL_COST, reps=200),
    }

    # report
    print("=== 1. HIDDEN BETA (dollar-neutral momentum, lb=120/rebal=5) ===")
    hb = out["hidden_beta"]
    print(f"  net beta: mean {hb['mean_net_beta']}  std {hb['std_net_beta']}  range [{hb['min_net_beta']}, {hb['max_net_beta']}]")
    print(f"  days net-LONG beta: {hb['frac_days_net_long_beta']*100:.0f}%   corr(net_beta, BTC 90d trend) = {hb['corr_netbeta_vs_btc_90d_trend']}")
    print(f"  -> {hb['note']}")
    print("\n=== 2. P&L DECOMPOSITION (gross/gross, regime-split) ===")
    pd_ = out["pnl_decomposition"]
    print(f"  beta share: full {pd_['beta_share_full']} | 2023-bull(train) {pd_['beta_share_train_2023bull']}"
          f" | holdout 2024-25 {pd_['beta_share_holdout_2024_25']}")
    print("\n=== 3. DOLLAR-NEUTRAL vs BETA-NEUTRAL (HL cost) ===")
    print(f"  {'mode':16}{'net%':>8}{'null':>6}{'holdNet':>9}{'holdNull':>9}{'turn/yr':>8}")
    for label, r in (("dollar-neutral", dn), ("beta-neutral", bn)):
        print(f"  {label:16}{r['net_pct']:>8}{r['null_pct']:>6}{r['hold_net_pct']:>9}{r['hold_null_pct']:>9}{r['turnover_per_yr']:>8}")
    print("\n  walk-forward null-clears (of 4):")
    for label in ("dollar_neutral", "beta_neutral"):
        wf = out["walk_forward"][label]
        cells = " ".join(f"{w['net_pct']:>6}({w['null_pct']:>4})" for w in wf)
        print(f"    {label:16} {cells}  clears {sum(1 for w in wf if w['null_pct']>95)}/4")

    # verdict
    bn_holds = bn["hold_null_pct"] > 95
    bn_full = bn["null_pct"] > 95
    out["verdict"] = ("BETA-NEUTRAL-EDGE-SURVIVES" if (bn_full and bn_holds)
                      else "BETA-NEUTRAL-EDGE-WEAK" if bn_full else "EDGE-WAS-LARGELY-BETA")
    print("\n" + "=" * 64)
    print("VERDICT:", out["verdict"])
    print(f"  beta share of P&L (gross, full): {pd_['beta_share_full']}")
    print(f"  beta-neutral edge: full null {bn['null_pct']}th, holdout null {bn['hold_null_pct']}th")
    print("=" * 64)

    for r in (dn, bn):
        for kk in ("_port", "_nb", "_bp"):
            r.pop(kk, None)
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "xs_beta.json").write_text(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
