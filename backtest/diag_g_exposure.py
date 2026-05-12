import pandas as pd, numpy as np, json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

bars = pd.read_csv("backtest/results/diag_bars.csv", parse_dates=["ts"])
trades = pd.read_csv("backtest/results/diag_trades.csv", parse_dates=["entry_time","exit_time"])
CV = 0.001  # contract_value

bars = bars.sort_values("ts").reset_index(drop=True)
bars["ts_utc"] = bars["ts"]
n = len(bars)

# exposure per bar: notional = size*CV*btc_close ; fraction = notional/equity
expo_notional = np.zeros(n)
in_market = np.zeros(n, dtype=bool)
ts_arr = bars["ts"].values
for _, t in trades.iterrows():
    mask = (ts_arr >= np.datetime64(t["entry_time"])) & (ts_arr < np.datetime64(t["exit_time"]))
    # add (trades shouldn't overlap, but be safe)
    expo_notional[mask] += t["size"] * CV * bars["btc_close"].values[mask]
    in_market[mask] = True
bars["expo_notional"] = expo_notional
bars["expo_frac"] = expo_notional / bars["equity"].values
bars["in_market"] = in_market

avg_expo_frac = bars["expo_frac"].mean()
pct_bars_in = in_market.mean()*100
expo_when_in = bars.loc[bars.in_market, "expo_frac"]
avg_notional = bars["expo_notional"].mean()
avg_equity = bars["equity"].mean()

btc0, btc1 = bars["btc_close"].iloc[0], bars["btc_close"].iloc[-1]
btc_ret = btc1/btc0 - 1.0
bench0, bench1 = bars["benchmark"].iloc[0], bars["benchmark"].iloc[-1]
bench_ret = bench1/bench0 - 1.0
strat_ret = bars["equity"].iloc[-1]/bars["equity"].iloc[0] - 1.0

# bar-by-bar btc returns
bars["btc_r"] = bars["btc_close"].pct_change().fillna(0.0)

# R_alloc: hold avg_expo_frac in BTC (rebalanced each bar to that fraction), rest cash
# daily-rebalanced partial: equity_t = prod(1 + e*btc_r)
e = avg_expo_frac
ralloc_rebal = np.prod(1 + e*bars["btc_r"].values) - 1.0
# buy-and-hold partial: e fraction in BTC at t0, never rebalanced
ralloc_bh = e*btc_ret  # since rest is cash flat
# time-weighted realized exposure each bar (the actual e_t), rebalanced
realized_curve = np.cumprod(1 + bars["expo_frac"].values*bars["btc_r"].values)
ralloc_realized = realized_curve[-1] - 1.0

selection_vs_rebal = strat_ret - ralloc_rebal
selection_vs_realized = strat_ret - ralloc_realized

# in-market vs out-of-market BTC bar return
btc_r_in = bars.loc[bars.in_market, "btc_r"]
btc_r_out = bars.loc[~bars.in_market, "btc_r"]

# dollar-weighted: exposure-weighted forward btc return
fwd_r = bars["btc_close"].shift(-1)/bars["btc_close"] - 1.0
dw = np.nansum(bars["expo_notional"].values[:-1]*fwd_r.values[:-1]) / np.nansum(bars["expo_notional"].values[:-1])

print(f"bars={n}, span {bars.ts.iloc[0]} -> {bars.ts.iloc[-1]}")
print(f"avg net exposure frac = {avg_expo_frac*100:.3f}%")
print(f"% bars in market = {pct_bars_in:.2f}%")
print(f"exposure when in market: mean {expo_when_in.mean()*100:.2f}% median {expo_when_in.median()*100:.2f}% min {expo_when_in.min()*100:.2f}% max {expo_when_in.max()*100:.2f}%")
print(f"avg position notional = ${avg_notional:.3f}, avg equity = ${avg_equity:.2f}")
print(f"BTC price return over period = {btc_ret*100:.2f}%")
print(f"Benchmark equity return = {bench_ret*100:.2f}%")
print(f"Strategy return = {strat_ret*100:.2f}%")
print(f"R_alloc (avg-expo {e*100:.2f}% rebalanced) = {ralloc_rebal*100:.3f}%")
print(f"R_alloc (avg-expo {e*100:.2f}% buy&hold)    = {ralloc_bh*100:.3f}%")
print(f"R_alloc (realized per-bar exposure rebal)  = {ralloc_realized*100:.3f}%")
print(f"SELECTION/TIMING vs rebal-avg   = {selection_vs_rebal*100:.2f}%")
print(f"SELECTION/TIMING vs realized-exp = {selection_vs_realized*100:.2f}%")
print(f"BTC bar return IN market:  mean {btc_r_in.mean()*1e4:.3f} bps (n={len(btc_r_in)})")
print(f"BTC bar return OUT market: mean {btc_r_out.mean()*1e4:.3f} bps (n={len(btc_r_out)})")
print(f"all-bar mean btc return: {bars['btc_r'].mean()*1e4:.3f} bps")
print(f"dollar-weighted fwd BTC return (exposure-weighted): {dw*1e4:.3f} bps per bar")

# Plot 1: equity curves
plt.figure(figsize=(12,6))
plt.plot(bars["ts"], bars["equity"], label=f"Strategy ({strat_ret*100:.1f}%)", color="crimson")
plt.plot(bars["ts"], bars["benchmark"], label=f"BTC B&H ({bench_ret*100:.1f}%)", color="black")
rebal_curve = np.cumprod(1+e*bars["btc_r"].values)
plt.plot(bars["ts"], 115*rebal_curve, label=f"Hold {e*100:.1f}% BTC, rest cash ({ralloc_rebal*100:.1f}%)", color="green", ls="--")
plt.plot(bars["ts"], 115*realized_curve, label=f"Realized per-bar exposure passive ({ralloc_realized*100:.1f}%)", color="blue", ls=":")
plt.legend(); plt.title("Exposure attribution: strategy vs allocation-only curves"); plt.ylabel("Equity ($)"); plt.grid(alpha=.3)
plt.tight_layout(); plt.savefig("backtest/results/diag_g_equity.png", dpi=110)

# Plot 2: exposure histogram (when in market)
plt.figure(figsize=(8,5))
plt.hist(expo_when_in*100, bins=40, color="steelblue")
plt.xlabel("Net exposure fraction (% of equity), bars in market"); plt.ylabel("# bars"); plt.title("Exposure distribution when in market")
plt.tight_layout(); plt.savefig("backtest/results/diag_g_expo_hist.png", dpi=110)

# Plot 3: exposure over time vs btc
fig, ax1 = plt.subplots(figsize=(12,5))
ax1.plot(bars["ts"], bars["btc_close"], color="black", lw=0.7, label="BTC close")
ax2 = ax1.twinx()
ax2.fill_between(bars["ts"], bars["expo_frac"]*100, color="orange", alpha=0.5, label="exposure %")
ax1.set_ylabel("BTC"); ax2.set_ylabel("exposure % equity"); plt.title("Exposure timing vs BTC price")
plt.tight_layout(); plt.savefig("backtest/results/diag_g_timing.png", dpi=110)

# also: how much of period has overlapping (>1 trade) — check
print("max simultaneous notional bars with >1 trade:", "n/a (additive)")
print("bars with any exposure:", in_market.sum())
