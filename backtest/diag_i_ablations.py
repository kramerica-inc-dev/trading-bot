#!/usr/bin/env python3
"""Axis I: consolidated ablations / counterfactuals. Throwaway, no source edits."""
import csv, json, math, os, sys
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INIT = 115.0
FEE = 0.0006          # per side
SLIP = 0.05 / 100.0   # per fill
RT_COST = 2 * FEE + 2 * SLIP   # ~0.0022 round trip
np.random.seed(20260512)

# ---- load data ----
bars = list(csv.DictReader(open(os.path.join(ROOT, "backtest/results/diag_bars.csv"))))
btc = np.array([float(b["btc_close"]) for b in bars])
ts = [b["ts"] for b in bars]
n_bars = len(btc)

trades = list(csv.DictReader(open(os.path.join(ROOT, "backtest/results/diag_trades.csv"))))
nt = len(trades)
bars_held = np.array([int(t["bars_held"]) for t in trades])
ep_fill = np.array([float(t["entry_price"]) for t in trades])
xp_fill = np.array([float(t["exit_price"]) for t in trades])
# pre-slippage mids (buy entry slipped up, long exit slipped down)
ep_mid = ep_fill / (1 + SLIP)
xp_mid = xp_fill / (1 - SLIP)
size = np.array([float(t["size"]) for t in trades])
CV = 0.001
pnl_real = np.array([float(t["pnl"]) for t in trades])

print(f"n_bars={n_bars}, n_trades={nt}, bars_held median={np.median(bars_held)} mean={bars_held.mean():.2f}")


def mdd(curve):
    peak = curve[0]; m = 0.0
    for v in curve:
        peak = max(peak, v)
        m = max(m, (peak - v) / peak)
    return m * 100.0


def metrics_from_curve(curve, ntrades=None, gross_pnl=None):
    curve = np.asarray(curve, float)
    ret = (curve[-1] / curve[0] - 1) * 100
    d = mdd(curve)
    cal = (ret / d) if d > 1e-9 else float("nan")
    bh_ret = (btc[-1] / btc[0] - 1) * 100  # -9.46%
    return dict(ret=round(ret, 2), wr=None, calmar=round(cal, 3), maxdd=round(d, 2),
                alpha=round(ret - bh_ret, 2), trades=ntrades,
                gross=(round(gross_pnl, 2) if gross_pnl is not None else None))


results = {}

# ============================================================
# (1) BUY-AND-HOLD baselines + stops
# ============================================================
# passive BH (no stop) -- equity = INIT * btc/btc0
bh_curve = INIT * btc / btc[0]
results["BH passive (no stop)"] = metrics_from_curve(bh_curve)

# fixed -X% stop from entry; once stopped, stay in cash for the rest of the period.
def bh_fixed_stop(stop_frac):
    entry = btc[0]
    stop_px = entry * (1 - stop_frac)
    cur = INIT
    out = [cur]
    stopped = False
    for i in range(1, n_bars):
        if not stopped:
            if btc[i] <= stop_px:
                # exit at stop (approx fill = stop_px), pay one-way friction (fee+slip) on exit
                cur = INIT * (stop_px / entry) * (1 - FEE - SLIP)
                stopped = True
            else:
                cur = INIT * (btc[i] / entry)
        out.append(cur)
    return np.array(out), stopped

# trailing -X% stop: stop level = running_max * (1 - X)
def bh_trail_stop(stop_frac):
    entry = btc[0]
    cur = INIT
    out = [cur]
    runmax = entry
    stopped = False
    for i in range(1, n_bars):
        if not stopped:
            runmax = max(runmax, btc[i])
            trail_px = runmax * (1 - stop_frac)
            if btc[i] <= trail_px:
                cur = INIT * (trail_px / entry) * (1 - FEE - SLIP)
                stopped = True
            else:
                cur = INIT * (btc[i] / entry)
        out.append(cur)
    return np.array(out), stopped

for sf, name in [(0.10, "BH + fixed -10% stop"), (0.20, "BH + fixed -20% stop")]:
    c, st = bh_fixed_stop(sf)
    m = metrics_from_curve(c)
    m["note"] = "stop hit" if st else "stop never hit"
    results[name] = m
c, st = bh_trail_stop(0.10)
m = metrics_from_curve(c)
m["note"] = "stop hit" if st else "stop never hit"
results["BH + trailing -10% stop"] = m

# ============================================================
# (2) ACTUAL random-entry backtest: ~185 random long entries,
#     holding times resampled (with replacement) from strategy bars_held,
#     same fee+slippage. 400 repeats.
# ============================================================
N_REP = 400
finals = []
rois = []
for _ in range(N_REP):
    holds = np.random.choice(bars_held, size=nt, replace=True)
    holds = np.maximum(holds, 1)
    # uniform random entry bars such that entry+hold < n_bars
    entries = np.random.randint(0, n_bars - holds.max() - 1, size=nt)
    # compound: each "trade" risks the same notional fraction the strategy did?
    # The strategy sizes ~ risk_per_trade ~ fixed notional ~ size*CV*price ~ const.
    # Simplest faithful model: compound equity, each trade applies pct move minus RT friction.
    eq = INIT
    for e, h in zip(entries, holds):
        p0 = btc[e]; p1 = btc[e + h]
        gross = p1 / p0 - 1.0
        # long: pay slippage on entry (+) and exit (-), fees both sides
        net = (1 + gross) * (1 - SLIP) / (1 + SLIP) * (1 - FEE) ** 2 - 1  # approx
        # apply on the strategy's average notional fraction of equity.
        # strategy notional/equity at entry ~ size*CV*price/equity. Use mean ratio.
        eq = eq * (1 + net)  # full-equity proxy; we'll also do notional-scaled below
    finals.append(eq)
    rois.append((eq / INIT - 1) * 100)

# Notional-scaled variant: match the strategy's actual per-trade notional (~ size*CV*ep_fill, mean)
mean_notional = (size * CV * ep_fill).mean()
finals2 = []
rois2 = []
for _ in range(N_REP):
    holds = np.maximum(np.random.choice(bars_held, size=nt, replace=True), 1)
    entries = np.random.randint(0, n_bars - holds.max() - 1, size=nt)
    eq = INIT
    for e, h in zip(entries, holds):
        p0 = btc[e]; p1 = btc[e + h]
        gross_ret = p1 / p0 - 1.0
        # pnl in $ on a notional of mean_notional, minus friction on that notional
        pnl = mean_notional * gross_ret - RT_COST * mean_notional
        eq += pnl
    finals2.append(eq)
    rois2.append((eq / INIT - 1) * 100)

rois = np.array(rois); rois2 = np.array(rois2)
rnd_band = (np.percentile(rois2, 5), np.percentile(rois2, 95))
print(f"\nrandom-entry (compound proxy): mean ROI {rois.mean():.1f}% median {np.median(rois):.1f}% "
      f"5-95% [{np.percentile(rois,5):.1f}, {np.percentile(rois,95):.1f}]")
print(f"random-entry (notional-scaled, faithful): mean ROI {rois2.mean():.1f}% median {np.median(rois2):.1f}% "
      f"5-95% [{rnd_band[0]:.1f}, {rnd_band[1]:.1f}]")
STRAT_ROI = -32.91
inside = rnd_band[0] <= STRAT_ROI <= rnd_band[1]
pctile = (rois2 < STRAT_ROI).mean() * 100
print(f"strategy ROI {STRAT_ROI}% is at the {pctile:.0f}th percentile of the random-entry band; inside 5-95%? {inside}")

results["Random 185 long entries (notional-scaled, n=400)"] = dict(
    ret=round(rois2.mean(), 2), wr=None, calmar=None, maxdd=None,
    alpha=round(rois2.mean() - (btc[-1]/btc[0]-1)*100, 2), trades=nt,
    note=f"5-95%: [{rnd_band[0]:.1f}%, {rnd_band[1]:.1f}%]; median {np.median(rois2):.1f}%")

# ============================================================
# (3) SIGN-FLIPPED strategy: same 185 entry timestamps, go SHORT.
#     Estimate from entry->exit MID price moves with symmetric friction.
#     long pnl per trade (mid) = notional * (xp_mid/ep_mid - 1)
#     short pnl per trade      = notional * (ep_mid/xp_mid - 1)  ~ -(long gross)
#     Apply same RT friction (2*FEE + 2*SLIP) on the same notional.
# ============================================================
notional = size * CV * ep_mid
long_gross_ret = xp_mid / ep_mid - 1.0
short_gross_ret = ep_mid / xp_mid - 1.0   # exact inverse-ish
# friction $ on round trip (entry + exit), using avg of the two notionals
fric_dollars = RT_COST * (size * CV * (ep_mid + xp_mid) / 2.0)
long_pnl_mid = notional * long_gross_ret
short_pnl = (size * CV * xp_mid) * short_gross_ret  # short: notional fixed at value sold... approx; use ep notional
# cleaner short: you sell `size` contracts at ep_mid, buy back at xp_mid:
short_pnl = size * CV * (ep_mid - xp_mid)
long_pnl_grossmid = size * CV * (xp_mid - ep_mid)
long_net = long_pnl_grossmid - fric_dollars
short_net = short_pnl - fric_dollars
print(f"\nsign-flip: long gross-mid PnL ${long_pnl_grossmid.sum():.2f}, short gross-mid PnL ${short_pnl.sum():.2f}")
print(f"   long net (mid - friction) ${long_net.sum():.2f}  ROI {long_net.sum()/INIT*100:.1f}%  WR {(long_net>0).mean()*100:.1f}%")
print(f"   short net (mid - friction) ${short_net.sum():.2f}  ROI {short_net.sum()/INIT*100:.1f}%  WR {(short_net>0).mean()*100:.1f}%")
results["Sign-flipped (SHORT same 185 entries, est. from mid moves)"] = dict(
    ret=round(short_net.sum()/INIT*100, 2), wr=round((short_net>0).mean()*100,1), calmar=None, maxdd=None,
    alpha=round(short_net.sum()/INIT*100 - (btc[-1]/btc[0]-1)*100, 2), trades=nt,
    gross=round(short_pnl.sum(), 2),
    note=f"long-side gross-mid ${long_pnl_grossmid.sum():.2f}, short-side gross-mid ${short_pnl.sum():.2f}; both ~0 -> noise")

# ============================================================
# (4) zero-cost on the SAME trades (axis C reconfirm, mid prices, no fees)
# ============================================================
results["Strategy entries, zero cost (mid prices, axis C)"] = dict(
    ret=round(long_pnl_grossmid.sum()/INIT*100, 2), wr=round((long_pnl_grossmid>0).mean()*100,1),
    calmar=None, maxdd=None, alpha=round(long_pnl_grossmid.sum()/INIT*100 - (btc[-1]/btc[0]-1)*100,2),
    trades=nt, gross=round(long_pnl_grossmid.sum(),2), note="cite axis C: full-backtest zero-cost = -1.1% ROI / WR 45.9%")

# ============================================================
# PLOTS
# ============================================================
# 1. BH + stops equity curves
fig, ax = plt.subplots(figsize=(11, 5))
xi = np.arange(n_bars)
ax.plot(xi, bh_curve, label=f"BH passive ({results['BH passive (no stop)']['ret']:+.1f}%)", lw=1.5)
for sf, name, ls in [(0.10, "BH + fixed -10% stop", "--"), (0.20, "BH + fixed -20% stop", "-."), ]:
    c, _ = bh_fixed_stop(sf)
    ax.plot(xi, c, ls, label=f"{name} ({results[name]['ret']:+.1f}%)")
c, _ = bh_trail_stop(0.10)
ax.plot(xi, c, ":", label=f"BH + trailing -10% stop ({results['BH + trailing -10% stop']['ret']:+.1f}%)")
# strategy equity for reference
seq = np.array([float(b["equity"]) for b in bars])
ax.plot(xi, seq, color="red", lw=1, alpha=0.7, label="advanced strategy (-32.9%)")
ax.axhline(INIT, color="k", lw=0.5)
ax.set_title("Buy-and-hold + stop-loss variants vs strategy"); ax.set_xlabel("bar #"); ax.set_ylabel("equity $")
ax.legend(fontsize=8)
plt.tight_layout(); plt.savefig(os.path.join(ROOT, "backtest/results/diag_i_bh_stops.png"), dpi=110)

# 2. random-entry distribution
fig, ax = plt.subplots(figsize=(10, 5))
ax.hist(rois2, bins=40, color="steelblue", edgecolor="k", alpha=0.8, label="random 185 long entries (notional-scaled, n=400)")
ax.axvline(STRAT_ROI, color="red", lw=2, label=f"advanced strategy ({STRAT_ROI}%)")
ax.axvline(np.percentile(rois2, 5), color="orange", ls="--", label="5th pct")
ax.axvline(np.percentile(rois2, 95), color="orange", ls="--", label="95th pct")
ax.axvline((btc[-1]/btc[0]-1)*100, color="green", ls=":", label="BH passive (-9.5%)")
ax.set_title("Strategy vs actual random-entry-same-turnover distribution")
ax.set_xlabel("final ROI %"); ax.legend(fontsize=8)
plt.tight_layout(); plt.savefig(os.path.join(ROOT, "backtest/results/diag_i_random_band.png"), dpi=110)

# 3. sign-flip: long vs short cumulative gross & net
fig, ax = plt.subplots(figsize=(10, 5))
ax.plot(np.cumsum(long_pnl_grossmid) + INIT, label=f"strategy long, zero-cost (${long_pnl_grossmid.sum():.1f})")
ax.plot(np.cumsum(short_pnl) + INIT, label=f"sign-flipped short, zero-cost (${short_pnl.sum():.1f})")
ax.plot(np.cumsum(long_net) + INIT, "--", label=f"long, with friction (${long_net.sum():.1f})")
ax.plot(np.cumsum(short_net) + INIT, "--", label=f"short, with friction (${short_net.sum():.1f})")
ax.plot(np.cumsum(pnl_real) + INIT, color="red", alpha=0.6, label=f"actual net (${pnl_real.sum():.1f})")
ax.axhline(INIT, color="k", lw=0.5)
ax.set_title("Sign-flip: inverting the signal does not make money (noise, not anti-signal)")
ax.set_xlabel("trade #"); ax.set_ylabel("equity $"); ax.legend(fontsize=8)
plt.tight_layout(); plt.savefig(os.path.join(ROOT, "backtest/results/diag_i_signflip.png"), dpi=110)

# dump
with open("/tmp/diag_i_results.json", "w") as f:
    json.dump(results, f, indent=2, default=str)
print("\n=== RESULTS ===")
print(json.dumps(results, indent=2, default=str))
print("\nplots: diag_i_bh_stops.png, diag_i_random_band.png, diag_i_signflip.png")
