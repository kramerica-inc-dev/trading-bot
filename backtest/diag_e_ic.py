import pandas as pd, numpy as np
from scipy.stats import spearmanr, pearsonr
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

tr = pd.read_csv("backtest/results/diag_trades.csv", parse_dates=["entry_time","exit_time"])
c = pd.read_csv("backtest/data/BTC-USDT_5m.csv", parse_dates=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
c["timestamp"] = pd.to_datetime(c["timestamp"], utc=True)
ts_to_idx = {t: i for i, t in enumerate(c["timestamp"])}

# map entry_time to bar index (entry presumably at that bar's close)
def idx_of(t):
    return ts_to_idx.get(pd.Timestamp(t))

tr["bar_idx"] = tr["entry_time"].map(idx_of)
print("trades mapped:", tr["bar_idx"].notna().sum(), "of", len(tr))
tr = tr.dropna(subset=["bar_idx"]).copy()
tr["bar_idx"] = tr["bar_idx"].astype(int)

close = c["close"].values
N = len(close)
H = [1,5,15,60,240]
for h in H:
    fwd = []
    for bi in tr["bar_idx"]:
        if bi+h < N:
            fwd.append(close[bi+h]/close[bi]-1)
        else:
            fwd.append(np.nan)
    tr[f"fwd_{h}"] = fwd

K = [3,12,48]
for k in K:
    pr = []
    for bi in tr["bar_idx"]:
        if bi-k >= 0:
            pr.append(close[bi]/close[bi-k]-1)
        else:
            pr.append(np.nan)
    tr[f"prior_{k}"] = pr

tr["raw_ret"] = tr["exit_price"]/tr["entry_price"]-1

predictors = ["confidence","ind_quality_rr_ratio","ind_quality_atr_pct","ind_quality_volume_score",
              "ind_quality_trend_extension_atr","ind_quality_bb_pos","ind_regime_confidence",
              "ind_htf_alignment_score","ind_efficiency_ratio","ind_rsi_value","ind_bb_pos",
              "ind_macd_hist","ind_trend_bias"]
targets = ["pnl_pct","raw_ret"] + [f"fwd_{h}" for h in H]

rows = []
for p in predictors:
    x = tr[p].astype(float)
    if x.nunique() <= 1:
        continue
    for t in targets:
        y = tr[t].astype(float)
        m = x.notna() & y.notna()
        n = m.sum()
        if n < 10: continue
        sr, sp = spearmanr(x[m], y[m])
        pr_, pp = pearsonr(x[m], y[m])
        rows.append(dict(predictor=p, target=t, n=n, spearman=round(sr,3), sp_p=round(sp,3),
                         pearson=round(pr_,3), pe_p=round(pp,3)))
ic = pd.DataFrame(rows)
ic.to_csv("backtest/results/diag_e_ic_table.csv", index=False)
print("\n=== IC TABLE (pnl_pct & fwd_5 & fwd_60) ===")
print(ic[ic.target.isin(["pnl_pct","fwd_5","fwd_60"])].to_string(index=False))

print("\n=== |Spearman| > 0.1 anywhere ===")
print(ic[ic.spearman.abs()>0.1].to_string(index=False))

# Monotonicity
def buckets(col, q=3):
    out = []
    try:
        b = pd.qcut(tr[col], q, duplicates="drop")
    except Exception:
        return None
    g = tr.groupby(b, observed=True)
    for name, grp in g:
        out.append(dict(bucket=str(name), n=len(grp), mean_pnl_pct=round(grp.pnl_pct.mean(),3),
                        win_rate=round((grp.pnl_pct>0).mean(),3), mean_fwd5=round(grp.fwd_5.mean()*100,3)))
    return pd.DataFrame(out)

print("\n=== Terciles by confidence ===")
print(buckets("confidence"))
print("\n=== Terciles by ind_quality_rr_ratio ===")
print(buckets("ind_quality_rr_ratio"))
print("\n=== Terciles by ind_regime_confidence ===")
print(buckets("ind_regime_confidence"))
print("\n=== Quintiles by confidence ===")
print(buckets("confidence",5))

# chasing vs contrarian
print("\n=== prior-run-up vs forward return ===")
for k in K:
    for h in [5,15,60]:
        m = tr[f"prior_{k}"].notna() & tr[f"fwd_{h}"].notna()
        sr, sp = spearmanr(tr.loc[m,f"prior_{k}"], tr.loc[m,f"fwd_{h}"])
        print(f"prior_{k} vs fwd_{h}: spearman={sr:.3f} p={sp:.3f}  mean_prior={tr[f'prior_{k}'].mean()*100:.3f}%  mean_fwd={tr[f'fwd_{h}'].mean()*100:.3f}%")

print("\n=== mean forward returns (all trades) vs zero ===")
for h in H:
    y = tr[f"fwd_{h}"].dropna()
    from scipy.stats import ttest_1samp
    t,pp = ttest_1samp(y,0)
    print(f"fwd_{h}: mean={y.mean()*100:.4f}%  median={y.median()*100:.4f}%  n={len(y)}  t={t:.2f} p={pp:.3f}  %positive={(y>0).mean():.3f}")

# random baseline: pick N random bars, compare fwd_5
rng = np.random.default_rng(0)
rand_means = []
for _ in range(2000):
    samp = rng.integers(0, N-240, len(tr))
    rand_means.append(np.mean([close[i+5]/close[i]-1 for i in samp]))
rand_means = np.array(rand_means)
obs = tr["fwd_5"].dropna().mean()
print(f"\nfwd_5 observed mean={obs*100:.4f}%  random-entry distribution: mean={rand_means.mean()*100:.4f}% pct(rand<obs)={(rand_means<obs).mean():.3f}")

# plots
fig, axes = plt.subplots(1,2,figsize=(12,4))
axes[0].scatter(tr["confidence"], tr["pnl_pct"], alpha=0.5)
axes[0].set_xlabel("confidence"); axes[0].set_ylabel("pnl_pct"); axes[0].set_title("confidence vs pnl_pct")
axes[1].scatter(tr["prior_12"]*100, tr["fwd_15"]*100, alpha=0.5)
axes[1].axhline(0,c="k",lw=.5); axes[1].axvline(0,c="k",lw=.5)
axes[1].set_xlabel("prior 12-bar return %"); axes[1].set_ylabel("fwd 15-bar return %"); axes[1].set_title("run-up vs post-entry")
plt.tight_layout(); plt.savefig("backtest/results/diag_e_scatter.png", dpi=90)

fig, ax = plt.subplots(figsize=(8,4))
hm = ic.pivot(index="predictor", columns="target", values="spearman")
im = ax.imshow(hm.values, cmap="RdBu", vmin=-0.3, vmax=0.3)
ax.set_xticks(range(len(hm.columns))); ax.set_xticklabels(hm.columns, rotation=45, ha="right")
ax.set_yticks(range(len(hm.index))); ax.set_yticklabels(hm.index)
for i in range(len(hm.index)):
    for j in range(len(hm.columns)):
        ax.text(j,i,f"{hm.values[i,j]:.2f}",ha="center",va="center",fontsize=7)
plt.colorbar(im); plt.title("Spearman IC heatmap"); plt.tight_layout()
plt.savefig("backtest/results/diag_e_ic_heatmap.png", dpi=90)
print("\ndone")
