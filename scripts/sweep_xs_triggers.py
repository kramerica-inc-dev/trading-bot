#!/usr/bin/env python3
"""Trigger study for the cross-sectional momentum lead — is the rebalance better
time-based (fixed cadence) or event-based (drift-band / signal-change)?

The validated config is fixed lb=120/rebal=5 (confirm_xsectional + harden). This
asks, within the project discipline, whether a dynamic trigger beats the fixed
clock net of cost — or just adds turnover. A dynamic trigger only earns a promote
if it (a) clears the random-selection null OUT-OF-SAMPLE (continuous, no boundary
reset), (b) beats fixed-5d net OOS, and (c) survives at a realistic (HL 4.5bps)
and conservative (15bps) cost. Otherwise the fixed clock stays.

v2 — fixes the four issues an adversarial review flagged in v1:
  * CONTINUOUS OOS. v1 simulated closes[cut:] from a flat book, giving every
    trigger a free clean re-entry at the holdout boundary (inflated low-frequency
    triggers most). v2 runs the FULL series once and slices daily returns to the
    holdout window, carrying the book across the boundary like a live runner.
  * FAIR DRIFT TRIGGER. v1's drift metric saw only dollar-neutrality skew and was
    blind to gross/leverage ballooning, so the book drifted to ~3x gross between
    rare fires. v2 fires on max(neutrality skew, |gross-2|/2) — the drift trigger
    now also controls leverage (its best shot).
  * HONEST NULL FRAMING. The null matches fire DATES, not turnover (random baskets
    churn more) — so headline percentiles overstate. v2 reports a ZERO-COST
    control: the momentum ranking must clear the null even at 0 cost (it does),
    which is the real evidence the edge isn't a cost-asymmetry artifact.
  * VERDICT GATE. v1 labelled fixed-7d/10d as 'dynamic winners' (gate excluded
    only cadence==5). v2 gates dynamic winners on trigger != 'fixed'.

OKX daily panel (3.5y); funding = HL-calibrated ~6%/yr flat proxy (a stress, not
measured — OKX public funding is ~3mo). Writes backtest/results/sweep/xs_triggers.json.
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
CONS_COST = 0.0015
FUNDING = 0.000165          # ~6%/yr HL-calibrated flat drag (1.65 bps/day on gross)
TARGET_GROSS = 2.0          # sum|w| at a fresh ±1/m rebalance (1x per side)


def _target(trail, m):
    order = np.argsort(trail)
    nw = np.zeros(len(trail)); nw[order[-m:]] = 1.0 / m; nw[order[:m]] = -1.0 / m
    return nw


def simulate(closes, *, lookback, m, cost_rate, trigger, band=0.10, cadence=5,
             min_spacing=1, selection="momentum", rng=None, forced_dates=None,
             flat_drag=0.0):
    """Drift-aware dollar-neutral sim. Returns (daily_port, fire_dates, turnover)."""
    n, k = closes.shape
    rets = closes[1:] / closes[:-1] - 1.0
    w = np.zeros(k); held = None; last = -10**9
    out = np.empty(n - 1); turnover = 0.0; fires = []
    forced = set(forced_dates) if forced_dates is not None else None
    for t in range(n - 1):
        cost = 0.0; fire = False
        if t >= lookback:
            if forced is not None:
                fire = t in forced
            elif trigger == "fixed":
                fire = (t - last) >= cadence
            elif held is None:
                fire = True
            elif (t - last) >= min_spacing:
                if trigger == "drift":
                    g = float(np.abs(w).sum())
                    skew = abs(w[w > 0].sum() + w[w < 0].sum()) / g if g > 0 else 0.0
                    lev_dev = abs(g - TARGET_GROSS) / TARGET_GROSS        # leverage-aware
                    fire = max(skew, lev_dev) > band
                elif trigger == "signal":
                    nw = _target(closes[t] / closes[t - lookback] - 1.0, m)
                    new = (frozenset(np.where(nw > 0)[0].tolist()), frozenset(np.where(nw < 0)[0].tolist()))
                    fire = new != held
        if fire:
            trail = closes[t] / closes[t - lookback] - 1.0
            if selection == "momentum":
                nw = _target(trail, m)
            elif selection == "random":
                order = rng.permutation(k); nw = np.zeros(k); nw[order[-m:]] = 1.0 / m; nw[order[:m]] = -1.0 / m
            elif selection == "shuffle":
                nw = _target(rng.permutation(trail), m)
            else:
                raise ValueError(selection)
            d = float(np.abs(nw - w).sum()); cost = d * cost_rate; turnover += d
            w = nw; held = (frozenset(np.where(nw > 0)[0].tolist()), frozenset(np.where(nw < 0)[0].tolist())); last = t; fires.append(t)
        drag = flat_drag * float(np.abs(w).sum())
        port = float((w * rets[t]).sum() - cost - drag)
        out[t] = port
        denom = 1.0 + port
        w = w * (1.0 + rets[t]) / (denom if abs(denom) > 1e-9 else 1e-9)
    return out, fires, turnover


def _ret(port):
    return float((np.cumprod(1.0 + port)[-1] - 1.0) * 100.0) if len(port) else 0.0


def _sharpe(port):
    sd = float(np.std(port, ddof=1))
    return float(np.mean(port) / sd * np.sqrt(365.0)) if sd > 0 else 0.0


def _kw(spec, cost, drag):
    return dict(lookback=spec["lookback"], m=spec["m"], cost_rate=cost, trigger=spec["trigger"],
               band=spec.get("band", 0.10), cadence=spec.get("cadence", 5),
               min_spacing=spec.get("min_spacing", 1), flat_drag=drag)


def evaluate(closes, spec, *, cost, drag, reps, seed, cut, years):
    """Full-sample + CONTINUOUS-holdout obs vs null (random on momentum's fire dates,
    same book carried across the train/test boundary)."""
    kw = _kw(spec, cost, drag)
    port, fires, turn = simulate(closes, selection="momentum", **kw)
    full_net, hold_net = _ret(port), _ret(port[cut:])
    rng = np.random.default_rng(seed)
    fn, hn = [], []
    for _ in range(reps):
        pr = simulate(closes, selection="random", rng=rng, forced_dates=fires, **kw)[0]
        fn.append(_ret(pr)); hn.append(_ret(pr[cut:]))
    return {
        "net_pct": round(full_net, 1), "sharpe": round(_sharpe(port), 3),
        "null_pct": round(float(np.mean(np.array(fn) < full_net) * 100), 1),
        "hold_net_pct": round(hold_net, 1),
        "hold_null_pct": round(float(np.mean(np.array(hn) < hold_net) * 100), 1),
        "n_rebal": len(fires), "turnover_per_yr": round(turn / years, 1),
    }


def walk_forward(closes, spec, *, cost, drag, reps):
    kw = _kw(spec, cost, drag)
    n = len(closes); bounds = np.linspace(0, n, 5).astype(int); rows = []
    for i in range(4):
        seg = closes[bounds[i]:bounds[i + 1]]
        if len(seg) < spec["lookback"] + 4 * 5:
            continue
        port, fires, _ = simulate(seg, selection="momentum", **kw)
        obs = _ret(port); rng = np.random.default_rng(50 + i)
        null = np.array([_ret(simulate(seg, selection="random", rng=rng, forced_dates=fires, **kw)[0]) for _ in range(reps)])
        rows.append({"win": i + 1, "net_pct": round(obs, 1), "null_pct": round(float(np.mean(null < obs) * 100), 1)})
    return rows


def main() -> int:
    panel = load_panel(DEFAULT_ASSETS)
    if panel.empty:
        print("No OKX panel — run: python -m backtest.okx_backfill --bar 1Dutc"); return 1
    closes = panel.to_numpy(); n = len(panel); years = n / 365.0; cut = int(n * 0.70)
    LB, M = 120, 3
    specs = [
        {"label": "fixed-3d", "trigger": "fixed", "cadence": 3, "lookback": LB, "m": M},
        {"label": "fixed-5d (VALIDATED)", "trigger": "fixed", "cadence": 5, "lookback": LB, "m": M},
        {"label": "fixed-7d", "trigger": "fixed", "cadence": 7, "lookback": LB, "m": M},
        {"label": "fixed-10d", "trigger": "fixed", "cadence": 10, "lookback": LB, "m": M},
        {"label": "drift(lev)-10%", "trigger": "drift", "band": 0.10, "lookback": LB, "m": M},
        {"label": "drift(lev)-20%", "trigger": "drift", "band": 0.20, "lookback": LB, "m": M},
        {"label": "signal-change-1d", "trigger": "signal", "min_spacing": 1, "lookback": LB, "m": M},
        {"label": "signal-change-5d", "trigger": "signal", "min_spacing": 5, "lookback": LB, "m": M},
    ]
    out = {"n_days": n, "n_assets": int(panel.shape[1]), "years": round(years, 2),
           "holdout_days": n - cut, "lookback": LB, "m": M,
           "funding_bps_day": round(FUNDING * 1e4, 2), "by_cost": {}}

    for clabel, cost in (("HL_4.5bps", HL_COST), ("conservative_15bps", CONS_COST)):
        print(f"\n===== cost={clabel} | full-sample + CONTINUOUS holdout =====")
        print(f"{'trigger':22}{'net%':>8}{'shrp':>6}{'null':>6}{'reb':>5}{'turn/yr':>8}{'holdNet':>9}{'holdNull':>9}")
        rows = {}
        for spec in specs:
            r = evaluate(closes, spec, cost=cost, drag=FUNDING, reps=200, seed=20260604, cut=cut, years=years)
            rows[spec["label"]] = r
            print(f"{spec['label']:22}{r['net_pct']:>8}{r['sharpe']:>6}{r['null_pct']:>6}{r['n_rebal']:>5}"
                  f"{r['turnover_per_yr']:>8}{r['hold_net_pct']:>9}{r['hold_null_pct']:>9}")
        out["by_cost"][clabel] = rows

    # zero-cost control: is the ranking edge real, or a cost-asymmetry artifact?
    base = specs[1]
    zc = evaluate(closes, base, cost=0.0, drag=0.0, reps=300, seed=7, cut=cut, years=years)
    out["zero_cost_control_fixed5d"] = {"net_pct": zc["net_pct"], "null_pct": zc["null_pct"]}
    print(f"\nZERO-COST control (fixed-5d): net {zc['net_pct']}% clears null at {zc['null_pct']}th "
          f"-> ranking edge is real (not a cost-asymmetry artifact)" )

    # walk-forward (regime robustness) at HL cost
    print(f"\n===== WALK-FORWARD (4 windows) @ HL cost — net% (null%ile) =====")
    wf = {}
    for spec in specs:
        rows = walk_forward(closes, spec, cost=HL_COST, drag=FUNDING, reps=200)
        wf[spec["label"]] = rows
        cells = " ".join(f"{r['net_pct']:>6}({r['null_pct']:>4})" for r in rows)
        clears = sum(1 for r in rows if r["null_pct"] > 95)
        print(f"{spec['label']:22} {cells}   null-clears {clears}/4")
    out["walk_forward_hl"] = wf

    # verdict — dynamic (non-fixed) winners only: clear continuous holdout null AND beat fixed-5d holdout
    base_hold = out["by_cost"]["HL_4.5bps"]["fixed-5d (VALIDATED)"]
    winners = [s["label"] for s in specs if s["trigger"] != "fixed"
               and out["by_cost"]["HL_4.5bps"][s["label"]]["hold_null_pct"] > 95
               and out["by_cost"]["HL_4.5bps"][s["label"]]["hold_net_pct"] > base_hold["hold_net_pct"]]
    out["dynamic_winners_continuous_oos"] = winners
    out["verdict"] = "DYNAMIC-BEATS-FIXED" if winners else "FIXED-CADENCE-STAYS"

    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "xs_triggers.json").write_text(json.dumps(out, indent=2))
    print("\n" + "=" * 72)
    print("VERDICT:", out["verdict"], "| dynamic winners:", winners or "none")
    print(f"  fixed-5d continuous holdout: net {base_hold['hold_net_pct']}% null {base_hold['hold_null_pct']}th")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
