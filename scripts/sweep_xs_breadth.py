#!/usr/bin/env python3
"""Cross-sectional momentum — breadth & legs sweep (2026-06-05).

Question (user): does EXPANDING the ranking universe (10 -> 15 -> 20 assets)
and/or holding MORE legs (m in {2,3,4,5}) beat the current LIVE config
(universe=10, m=3, lookback=120, rebal=5), once we honestly price two caveats:

  Caveat #1 (thin-alt cost): the harness uses a FLAT cost_rate=0.0015. The added
    coins (progressively into U20) have far lower real HL volume than BTC/ETH, so
    the flat rate UNDERSTATES true cost at higher breadth. We run a cost-sensitivity
    pass on U15/U20 at the best m, at cost_rate in {0.0015, 0.0030, 0.0045}.

  Caveat #2 (capital / min-notional): HL min-notional ~= $10/leg. At ~$57 equity a
    dollar-neutral m=3 book is ~$9.50/leg, already at the floor; m=4 needs ~>=$150 and
    m=5 ~>=$200-250 to keep each leg safely above $10. So even a better m=4/5 backtest
    is NOT live-deployable until capital grows. We REPORT this gate; we do not model
    it away.

Cadence is held fixed at the validated lookback=120 / rebal=5 (docs/XS-TRIGGER-STUDY.md)
— only breadth (universe) and legs (m) vary here.

Reuses the hardened harness backtest/sweep/xsectional.py (random-basket null + sham
shuffle control). Writes backtest/results/sweep/xsectional_breadth.json.

NOTE on the panel window: load_panel inner-joins on common dates and dropna()s, so the
shortest-history asset sets the shared window. BNB-USDT (1259 bars, starts 2022-12-23)
is the binding constraint and it is in U10/U15/U20 alike — so the window is identical
across all three universes and breadth is NOT confounded with sample period. INJ-USDT
(only 918 bars) is EXCLUDED for exactly this reason: it would truncate the window.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "backtest"))
sys.path.insert(0, str(PROJECT_ROOT))

from sweep import xsectional  # noqa: E402
from sweep.xsectional import XSConfig  # noqa: E402

RESULTS = PROJECT_ROOT / "backtest" / "results" / "sweep"

U10 = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT",
       "DOGE-USDT", "ADA-USDT", "AVAX-USDT", "DOT-USDT", "LINK-USDT"]
U15 = U10 + ["LTC-USDT", "BCH-USDT", "TRX-USDT", "ATOM-USDT", "OP-USDT"]
U20 = U15 + ["NEAR-USDT", "APT-USDT", "UNI-USDT", "AAVE-USDT", "ETC-USDT"]
UNIVERSES = {"U10": U10, "U15": U15, "U20": U20}

LOOKBACK = 120          # validated cadence — held fixed
REBAL = 5               # validated cadence — held fixed
MS = [2, 3, 4, 5]
REPS = 500
N_SHAM = 3
FLAT_COST = 0.0015
COST_GRID = [0.0015, 0.0030, 0.0045]   # caveat #1 sensitivity (best-m on U15/U20)
SEED = 20260605

# capital gate (caveat #2): HL min-notional ~$10/leg. Dollar-neutral, gross book
# sized to equity => 2m legs total (m long + m short), per-leg = equity / (2m).
# Sanity vs the user's anchors: $57/(2*3)=$9.50/leg (m=3, at the floor); m=4 wants
# ~>=$150 and m=5 ~>=$200-250 to clear $10/leg with a real buffer. We require
# per-leg >= 1.4 * min_notional for slippage/rounding headroom.
MIN_NOTIONAL = 10.0
PER_LEG_BUFFER = 1.4    # require per-leg >= 1.4 * min_notional for headroom
LIVE_EQUITY = 57.0


def _panel_diag(name, universe):
    panel = xsectional.load_panel(universe)
    return {
        "universe": name,
        "n_assets_requested": len(universe),
        "n_assets_loaded": int(panel.shape[1]),
        "n_days": int(len(panel)),
        "start": str(panel.index[0])[:10] if len(panel) else None,
        "end": str(panel.index[-1])[:10] if len(panel) else None,
        "nans": int(panel.isna().sum().sum()),
        "truncated": bool(len(panel) < 1259),   # 1259 = BNB-bound shared window
    }


def _row(universe_name, universe, m, cost_rate, reps, n_sham, baseline=None):
    cfg = XSConfig(lookback=LOOKBACK, rebal=REBAL, m=m, cost_rate=cost_rate)
    v = xsectional.run(cfg=cfg, assets=universe, reps=reps, n_sham=n_sham, seed=SEED)
    met = v.metrics
    sham = met.get("sham_percentiles", [])
    sham_passes = sum(1 for s in sham if isinstance(s, (int, float)) and s > 95.0)
    # m=5 on U10 is degenerate: long top-5 / short bottom-5 = the whole 10-asset
    # universe split in half => no selection, pure dispersion bet.
    degenerate = (2 * m >= len(universe))
    row = {
        "universe": universe_name,
        "n_assets": met.get("n_assets"),
        "n_days": met.get("n_days"),
        "m": m,
        "lookback": LOOKBACK,
        "rebal": REBAL,
        "cost_rate": cost_rate,
        "verdict": v.verdict,
        "net_pct": met.get("net_return_pct"),
        "gross_pct": met.get("gross_return_pct"),
        "cost_share": met.get("cost_share"),
        "sharpe": met.get("sharpe"),
        "null_percentile": met.get("null_percentile"),
        "null_p95_return_pct": met.get("null_p95_return_pct"),
        "xs_ic_mean": met.get("xs_ic_mean"),
        "xs_ic_p": met.get("xs_ic_p"),
        "sham_percentiles": sham,
        "sham_passes": sham_passes,
        "sham_void": sham_passes >= (n_sham // 2 + 1),
        "degenerate_full_split": degenerate,
    }
    if baseline is not None:
        row["d_net_pct"] = round(row["net_pct"] - baseline["net_pct"], 2)
        row["d_sharpe"] = round(row["sharpe"] - baseline["sharpe"], 3)
        row["d_null_pct"] = round(row["null_percentile"] - baseline["null_percentile"], 1)
    return row


def _capital_gate():
    rows = []
    for m in MS:
        per_leg = LIVE_EQUITY / (2 * m)    # dollar-neutral, gross book = equity, 2m legs
        needed = MIN_NOTIONAL * PER_LEG_BUFFER * 2 * m
        rows.append({
            "m": m,
            "per_leg_notional_at_57": round(per_leg, 2),
            "min_notional": MIN_NOTIONAL,
            "deployable_at_57": per_leg >= MIN_NOTIONAL * PER_LEG_BUFFER,
            "equity_needed_for_buffer": round(needed, 0),
        })
    return rows


def main() -> int:
    print("=" * 72)
    print("XS BREADTH & LEGS SWEEP  (lb=120 rebal=5 fixed; reps=%d, n_sham=%d)"
          % (REPS, N_SHAM))
    print("=" * 72)

    # --- panel diagnostics (assert no truncation confound) ---
    panels = {name: _panel_diag(name, u) for name, u in UNIVERSES.items()}
    print("\nPanel diagnostics:")
    n_days_set = set()
    for name in ("U10", "U15", "U20"):
        d = panels[name]
        n_days_set.add(d["n_days"])
        flag = "  <-- TRUNCATED" if d["truncated"] else ""
        print(f"  {name}: assets={d['n_assets_loaded']}/{d['n_assets_requested']} "
              f"n_days={d['n_days']} {d['start']}..{d['end']} nans={d['nans']}{flag}")
    # Honest assertion: all universes must share the SAME window (BNB-bound), else
    # breadth is confounded with sample period.
    consistent_window = len(n_days_set) == 1
    if not consistent_window:
        print("  WARNING: universes do NOT share a common window — breadth is "
              "confounded with sample period. Investigate before trusting deltas.")
    else:
        print(f"  OK: all 3 universes share the same {n_days_set.pop()}-day window "
              "(BNB-bound) — breadth is NOT confounded with sample period.")
    for d in panels.values():
        assert d["nans"] == 0, f"NaNs leaked into {d['universe']} panel"

    # --- main sweep: 3 universes x 4 m at flat cost ---
    print("\nMain sweep (cost_rate=%.4f):" % FLAT_COST)
    sweep = []
    baseline = None
    # compute baseline first (U10/m3) so we can report deltas
    baseline = _row("U10", U10, 3, FLAT_COST, REPS, N_SHAM)
    for name, u in UNIVERSES.items():
        for m in MS:
            if name == "U10" and m == 3:
                row = dict(baseline)
                row["d_net_pct"] = 0.0
                row["d_sharpe"] = 0.0
                row["d_null_pct"] = 0.0
            else:
                row = _row(name, u, m, FLAT_COST, REPS, N_SHAM, baseline=baseline)
            sweep.append(row)
            deg = " [DEGEN full-split]" if row["degenerate_full_split"] else ""
            void = " [VOID sham!]" if row["sham_void"] else ""
            print(f"  {name} m={m}: {row['verdict']:7s} net={row['net_pct']:8.1f}% "
                  f"sharpe={row['sharpe']:6.3f} null={row['null_percentile']:5.1f} "
                  f"d_net={row.get('d_net_pct'):+7.1f} d_sh={row.get('d_sharpe'):+.3f} "
                  f"d_null={row.get('d_null_pct'):+.1f} sham={row['sham_percentiles']}{deg}{void}")

    # --- pick best m per breadth universe (by null then net), among non-degenerate ---
    def _best_for(name):
        cands = [r for r in sweep if r["universe"] == name
                 and not r["degenerate_full_split"] and not r["sham_void"]]
        if not cands:
            return None
        return sorted(cands, key=lambda r: (r["null_percentile"], r["net_pct"]),
                      reverse=True)[0]

    best = {name: _best_for(name) for name in UNIVERSES}
    print("\nBest non-degenerate m per universe (by null,then net):")
    for name in ("U10", "U15", "U20"):
        b = best[name]
        if b:
            print(f"  {name}: m={b['m']} null={b['null_percentile']} net={b['net_pct']}% "
                  f"sharpe={b['sharpe']}")

    # --- caveat #1: cost-sensitivity on U15 / U20 at their best m ---
    print("\nCost-sensitivity (caveat #1 — thin-alt cost) on U15/U20 best-m:")
    cost_sens = []
    for name in ("U15", "U20"):
        b = best[name]
        if not b:
            continue
        m = b["m"]
        for cr in COST_GRID:
            r = _row(name, UNIVERSES[name], m, cr, REPS, N_SHAM, baseline=baseline)
            r["best_m_for_universe"] = m
            cost_sens.append(r)
            print(f"  {name} m={m} cost={cr:.4f}: net={r['net_pct']:8.1f}% "
                  f"null={r['null_percentile']:5.1f} sharpe={r['sharpe']:6.3f} "
                  f"cost_share={r['cost_share']}")

    # --- caveat #2: capital / min-notional gate ---
    cap = _capital_gate()
    print("\nCapital gate (caveat #2 — HL min-notional ~$10/leg) at $%.0f equity:"
          % LIVE_EQUITY)
    for c in cap:
        ok = "DEPLOYABLE" if c["deployable_at_57"] else "GATED"
        print(f"  m={c['m']}: per-leg=${c['per_leg_notional_at_57']} -> {ok} "
              f"(needs ~${c['equity_needed_for_buffer']:.0f} for buffer)")

    # --- assemble verdict-relevant facts ---
    void_configs = [f"{r['universe']}/m{r['m']}@{r['cost_rate']}"
                    for r in sweep + cost_sens if r["sham_void"]]
    # shams that individually cleared the 95 gate (discrimination warning, even if
    # not a formal majority-VOID): the gate is meant to reject shuffled rankings.
    sham_clears = [{"config": f"{r['universe']}/m{r['m']}@{r['cost_rate']}",
                    "sham_percentiles": r["sham_percentiles"]}
                   for r in sweep + cost_sens
                   if any(isinstance(s, (int, float)) and s > 95.0
                          for s in r["sham_percentiles"])]
    # "breadth helps" only if a wider universe ADVANCEs (passes ALL gates incl IC)
    # with higher net than baseline — net/null alone is not enough if IC dies.
    breadth_helps = any(
        r["universe"] in ("U15", "U20") and r["verdict"] == "ADVANCE"
        and r["net_pct"] > baseline["net_pct"]
        for r in sweep)
    # IC dilution: does widening the universe kill the cross-sectional IC?
    ic_by_universe = {}
    for name in ("U10", "U15", "U20"):
        rs = [r for r in sweep if r["universe"] == name]
        if rs:
            ic_by_universe[name] = {"xs_ic_mean": rs[0]["xs_ic_mean"],
                                    "xs_ic_p": rs[0]["xs_ic_p"]}

    summary = {
        "study": "xs_breadth_and_legs",
        "date": "2026-06-05",
        "fixed_cadence": {"lookback": LOOKBACK, "rebal": REBAL},
        "reps": REPS, "n_sham": N_SHAM, "flat_cost_rate": FLAT_COST,
        "seed": SEED,
        "panel_diagnostics": panels,
        "consistent_window": consistent_window,
        "baseline_U10_m3": baseline,
        "sweep": sweep,
        "best_per_universe": {k: v for k, v in best.items()},
        "cost_sensitivity": cost_sens,
        "capital_gate": cap,
        "live_equity": LIVE_EQUITY,
        "void_configs": void_configs,
        "sham_clears_individually": sham_clears,
        "ic_by_universe": ic_by_universe,
        "breadth_helps_flag": bool(breadth_helps),
        "notes": (
            "Baseline anchor = U10/m3 (live). m=5 on U10 (and m>=ceil(N/2)) is a "
            "degenerate full-universe split = no selection. Cost-sensitivity prices "
            "caveat #1 (thin alts cost more than the flat 0.0015). Capital gate prices "
            "caveat #2 (m>3 not deployable at $57). Sham (shuffled ranking) MUST fail "
            "the null gate for a config to be valid; any sham_void config is VOID."
        ),
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / "xsectional_breadth.json"
    out.write_text(json.dumps(summary, indent=2, default=float))
    print(f"\n  -> wrote {out}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
