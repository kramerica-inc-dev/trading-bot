import pandas as pd, numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

np.random.seed(42)
trades = pd.read_csv("backtest/results/diag_trades.csv", parse_dates=["entry_time","exit_time"])
bars = pd.read_csv("backtest/data/BTC-USDT_5m.csv", parse_dates=["timestamp"])
dbars = pd.read_csv("backtest/results/diag_bars.csv", parse_dates=["ts"])

bars = bars.sort_values("timestamp").reset_index(drop=True)
bars["ts_utc"] = bars["timestamp"]
# index by timestamp
ts_to_idx = {t: i for i, t in enumerate(bars["timestamp"])}
close = bars["close"].values
n = len(bars)

horizons = [1, 5, 15, 60]
FRICTION = 0.0022  # round trip

# map each trade entry to a bar index
def nearest_idx(t):
    return ts_to_idx.get(t, None)

rows = []
missing = 0
for _, r in trades.iterrows():
    i = nearest_idx(r["entry_time"])
    if i is None:
        # find by searchsorted
        pos = bars["timestamp"].searchsorted(r["entry_time"])
        if pos < n and abs((bars["timestamp"].iloc[pos]-r["entry_time"]).total_seconds())<=300:
            i = pos
        else:
            missing += 1
            continue
    rec = {"i": i, "entry_time": r["entry_time"], "pnl_pct": r["pnl_pct"], "exit_reason": r["exit_reason"]}
    for h in horizons:
        if i+h < n:
            rec[f"fwd_{h}"] = close[i+h]/close[i]-1
        else:
            rec[f"fwd_{h}"] = np.nan
    rows.append(rec)
td = pd.DataFrame(rows)
print(f"trades mapped: {len(td)}, missing: {missing}")

# unconditional forward returns over all bars
uncond = {}
for h in horizons:
    fr = close[h:]/close[:-h]-1
    uncond[h] = fr

print("\n=== Step 1: hit rates & forward returns ===")
step1 = []
for h in horizons:
    col = td[f"fwd_{h}"].dropna()
    hr = (col > 0).mean()
    step1.append({"h": h, "n": len(col), "hit_rate": hr, "mean_fwd": col.mean(), "median_fwd": col.median(),
                  "uncond_mean": uncond[h].mean(), "uncond_hit": (uncond[h]>0).mean()})
s1 = pd.DataFrame(step1)
print(s1.to_string(index=False))

# entry->exit return
ee = td["pnl_pct"]/100.0  # pnl_pct is percent
# actually pnl_pct in csv: -0.443 means -0.443%? check: entry 84847 exit 84573 => -0.323% gross. pnl_pct=-0.443 includes friction. ok treat as percent.
print(f"\nentry->exit mean pnl_pct: {trades['pnl_pct'].mean():.4f}%  median: {trades['pnl_pct'].median():.4f}%  win rate(pnl>0): {(trades['pnl']>0).mean():.3f}")

print("\n=== Step 2: NULL distribution (1000 draws of 185 random entries) ===")
N_DRAW = 1000
ntr = len(td)
null_res = {h: {"hit": [], "mean": []} for h in horizons}
for h in horizons:
    valid_max = n - h
    for _ in range(N_DRAW):
        idx = np.random.randint(0, valid_max, ntr)
        fr = close[idx+h]/close[idx]-1
        null_res[h]["hit"].append((fr>0).mean())
        null_res[h]["mean"].append(fr.mean())

step2 = []
for h in horizons:
    col = td[f"fwd_{h}"].dropna()
    act_hit = (col>0).mean(); act_mean = col.mean()
    nh = np.array(null_res[h]["hit"]); nm = np.array(null_res[h]["mean"])
    step2.append({"h": h,
        "act_hit": act_hit, "null_hit_mean": nh.mean(), "hit_pctile": (nh < act_hit).mean()*100, "hit_z": (act_hit-nh.mean())/nh.std(),
        "act_mean": act_mean, "null_mean_mean": nm.mean(), "mean_pctile": (nm < act_mean).mean()*100, "mean_z": (act_mean-nm.mean())/nm.std()})
s2 = pd.DataFrame(step2)
print(s2.to_string(index=False))

print("\n=== Step 3: regime breakdown ===")
# join entry_time to dbars regime - dbars is sparse (one per ... actually 106360 rows ~ per bar). join on nearest <= ts
dbars = dbars.sort_values("ts").reset_index(drop=True)
reg_idx = dbars["ts"].values
def regime_for(t):
    pos = dbars["ts"].searchsorted(t, side="right")-1
    if pos < 0: pos = 0
    return dbars["regime"].iloc[pos]
trades["bar_regime"] = trades["entry_time"].apply(regime_for)
g = trades.groupby("bar_regime").agg(n=("pnl","size"), win_rate=("pnl", lambda x:(x>0).mean()),
        avg_pnl_pct=("pnl_pct","mean"), tot_pnl=("pnl","sum"))
g["frac"] = g["n"]/g["n"].sum()
print(g.to_string())

# also forward return by regime for mirror short estimate
td2 = td.merge(trades[["entry_time","bar_regime"]], on="entry_time", how="left")
print("\nForward 60-bar return by regime (for mirror-short estimate):")
print(td2.groupby("bar_regime")["fwd_60"].agg(["count","mean","median"]).to_string())

print("\n=== Step 4: strategy vs alternatives ===")
# strategy realized: from diag_summary
import json
summ = json.load(open("backtest/results/diag_summary.json"))
print("summary keys:", list(summ.keys()))
# random-entry PnL estimate: same count, same avg holding time, friction
avg_hold = trades["bars_held"].mean()
print(f"avg bars held: {avg_hold:.2f}")
# null PnL: for each draw, sum of fwd returns at ~avg_hold horizon minus friction per trade, scaled by risk
# risk_per_trade 5% but contracts... simpler: express as % of equity. Actual avg notional? size*price. Let's just do gross return per trade at horizon h≈round(avg_hold)
h_hold = max(1, int(round(avg_hold)))
print(f"using h_hold={h_hold} for random PnL proxy")
# but holding 1-2 bars: friction 0.22% dominates any 1-2 bar move (atr_pct ~0.15%).
valid_max = n - h_hold
null_pnl_pct_per_trade = []
for _ in range(N_DRAW):
    idx = np.random.randint(0, valid_max, ntr)
    fr = close[idx+h_hold]/close[idx]-1
    null_pnl_pct_per_trade.append(fr.mean() - FRICTION)  # net per trade as frac of notional
npp = np.array(null_pnl_pct_per_trade)
print(f"random-entry net per-trade return (frac of notional): mean={npp.mean()*100:.4f}%  -> over {ntr} trades sum={(npp.mean()*ntr)*100:.3f}% of notional")
print(f"strategy actual mean pnl_pct per trade: {trades['pnl_pct'].mean():.4f}%  sum over trades: {trades['pnl_pct'].sum():.3f}%")
print(f"strategy total PnL $: {summ.get('total_pnl', summ.get('pnl'))}, ROI: {summ}")

# Plots
fig, axes = plt.subplots(2,2, figsize=(12,9))
for ax,h in zip(axes.flat, horizons):
    nh = np.array(null_res[h]["mean"])*100
    ax.hist(nh, bins=40, alpha=0.7, label="null mean fwd ret")
    act = td[f"fwd_{h}"].dropna().mean()*100
    ax.axvline(act, color="r", lw=2, label=f"strategy {act:.3f}%")
    ax.axvline(uncond[h].mean()*100, color="g", ls="--", label=f"uncond {uncond[h].mean()*100:.3f}%")
    ax.set_title(f"h={h} bars"); ax.legend(fontsize=8)
plt.tight_layout(); plt.savefig("backtest/results/diag_a_null_meanfwd.png", dpi=110); plt.close()

fig, axes = plt.subplots(2,2, figsize=(12,9))
for ax,h in zip(axes.flat, horizons):
    nh = np.array(null_res[h]["hit"])*100
    ax.hist(nh, bins=40, alpha=0.7)
    act = (td[f"fwd_{h}"].dropna()>0).mean()*100
    ax.axvline(act, color="r", lw=2, label=f"strategy {act:.1f}%")
    ax.axvline(50, color="k", ls=":")
    ax.set_title(f"hit rate BTC up, h={h}"); ax.legend(fontsize=8)
plt.tight_layout(); plt.savefig("backtest/results/diag_a_null_hitrate.png", dpi=110); plt.close()

# scatter fwd vs pnl
fig, ax = plt.subplots(figsize=(8,6))
ax.scatter(td["fwd_5"]*100, td["pnl_pct"], alpha=0.5)
ax.axhline(0, color="k", lw=.5); ax.axvline(0, color="k", lw=.5)
ax.set_xlabel("BTC fwd 5-bar return %"); ax.set_ylabel("trade pnl_pct %")
ax.set_title(f"trade outcome vs 5-bar fwd BTC move (corr={td['fwd_5'].corr(td['pnl_pct']):.2f})")
plt.tight_layout(); plt.savefig("backtest/results/diag_a_fwd_vs_pnl.png", dpi=110); plt.close()
print("\ncorr(fwd5, pnl_pct):", td["fwd_5"].corr(td["pnl_pct"]))
print("corr(fwd1, pnl_pct):", td["fwd_1"].corr(td["pnl_pct"]))
print("done")
