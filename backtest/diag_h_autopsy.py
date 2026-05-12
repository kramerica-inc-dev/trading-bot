import pandas as pd, numpy as np

tr = pd.read_csv('backtest/results/diag_trades.csv', parse_dates=['entry_time','exit_time'])
c = pd.read_csv('backtest/data/BTC-USDT_5m.csv', parse_dates=['timestamp'])
c = c.set_index('timestamp').sort_index()
ts = c.index

FRIC = 0.0022

def slice_bars(t0, t1):
    i0 = ts.searchsorted(t0)
    i1 = ts.searchsorted(t1)
    return c.iloc[i0:i1+1], i0, i1

rows=[]
for _,r in tr.iterrows():
    seg, i0, i1 = slice_bars(r.entry_time, r.exit_time)
    if len(seg)==0:
        rows.append({}); continue
    ep = r.entry_price
    mfe = (seg['high'].max()-ep)/ep
    mae = (seg['low'].min()-ep)/ep
    # exit slippage: for a buy stop, fill below low? compare exit_price vs candle at exit
    exit_candle = c.iloc[i1] if i1<len(c) else c.iloc[-1]
    # adverse slippage = how far exit_price is below the candle low (for a long exit, worse = lower)
    slip = (r.exit_price - exit_candle['low'])/ep  # negative => filled below the low
    # post-exit returns (long => positive is "trade's direction")
    def fwd(n):
        j = min(i1+n, len(c)-1)
        return (c.iloc[j]['close'] - r.exit_price)/r.exit_price
    rows.append(dict(mfe=mfe, mae=mae, slip=slip, post12=fwd(12), post48=fwd(48),
                     exit_open=exit_candle['open'], exit_low=exit_candle['low'], exit_high=exit_candle['high']))
m = pd.DataFrame(rows)
d = pd.concat([tr.reset_index(drop=True), m], axis=1)
d['win'] = d['pnl']>0

losers = d[~d['win']].copy()
winners = d[d['win']].copy()
print("trades", len(d), "losers", len(losers), "winners", len(winners))
print("exit reasons losers:\n", losers['exit_reason'].value_counts())

# classify losers
def bucket(r):
    # d: gap/slippage - exit materially below candle low (adverse fill > ~0.1%)
    if pd.notna(r.slip) and r.slip < -0.0010:
        return 'd_gap_slippage'
    # b: right direction, exited too early - MFE > 0.3%
    if r.mfe > 0.003:
        return 'b_exited_too_early'
    # c: whipsaw/noise - both small
    if abs(r.mfe) < 0.003 and abs(r.mae) < 0.003:
        return 'c_whipsaw_noise'
    # a: wrong direction - small MFE, hit MAE
    if r.mfe < 0.002:
        return 'a_wrong_direction'
    return 'c_whipsaw_noise'

losers['bucket'] = losers.apply(bucket, axis=1)
print("\n=== LOSER BUCKETS ===")
g = losers.groupby('bucket').agg(n=('pnl','size'), usd=('pnl','sum'), mean_mfe=('mfe','mean'), mean_mae=('mae','mean'), mean_bars=('bars_held','mean'))
print(g)
print("by exit_reason x bucket:\n", pd.crosstab(losers['bucket'], losers['exit_reason']))

# winners genuine vs lucky
winners['kind'] = np.where(winners['mfe']>0.003, 'genuine', np.where(winners['pnl_pct']>0.25,'modest','lucky_whipsaw'))
# refine: lucky = mfe < friction-ish AND captured tiny
winners['kind'] = np.where(winners['mfe'] < 0.0035, 'lucky_whipsaw', 'genuine')
print("\n=== WINNERS ===")
print(winners.groupby('kind').agg(n=('pnl','size'), usd=('pnl','sum'), mean_mfe=('mfe','mean'), mean_pnl_pct=('pnl_pct','mean'), mean_bars=('bars_held','mean')))
print("winner exit reasons:\n", winners['exit_reason'].value_counts())

# capture efficiency for winners: pnl_pct / mfe*100
winners['capture'] = winners['pnl_pct']/(winners['mfe']*100)
print("winner median capture of MFE:", winners['capture'].median())

# post-exit
print("\n=== POST-EXIT (long direction; positive = price kept going trade's way) ===")
for name,sub in [('losers',losers),('winners',winners),('all',d)]:
    print(f"{name}: post12 mean {sub.post12.mean()*100:.3f}%  median {sub.post12.median()*100:.3f}%  | post48 mean {sub.post48.mean()*100:.3f}%  median {sub.post48.median()*100:.3f}%  | %post12>0 {(sub.post12>0).mean()*100:.1f}%")

# for b bucket specifically
b = losers[losers.bucket=='b_exited_too_early']
print(f"\nb-bucket: n={len(b)} mean MFE {b.mfe.mean()*100:.2f}% mean captured pnl_pct {b.pnl_pct.mean():.2f}% -> left on table ~{(b.mfe.mean()*100 - b.pnl_pct.mean()):.2f}%")
print("b-bucket exit reasons:\n", b['exit_reason'].value_counts())
print("b-bucket post12 mean %.3f%%" % (b.post12.mean()*100))

# overall MFE distribution of losers
print("\nLoser MFE pctiles:", np.percentile(losers.mfe.dropna(), [10,25,50,75,90])*100)
print("Loser MAE pctiles:", np.percentile(losers.mae.dropna(), [10,25,50,75,90])*100)
print("Frac losers with MFE < friction(0.22%):", (losers.mfe < FRIC).mean())
print("Frac losers MFE < 0.1%:", (losers.mfe < 0.001).mean())

d.to_csv('backtest/results/diag_h_enriched.csv', index=False)

# plot
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
fig,axs = plt.subplots(1,2, figsize=(13,5))
gg = losers.groupby('bucket').agg(n=('pnl','size'), usd=('pnl','sum'))
gg = gg.reindex(['a_wrong_direction','b_exited_too_early','c_whipsaw_noise','d_gap_slippage']).dropna(how='all')
axs[0].bar(gg.index, gg.n, color='steelblue'); axs[0].set_title('Loser count by bucket'); axs[0].tick_params(axis='x', rotation=20)
axs[1].bar(gg.index, gg.usd, color='firebrick'); axs[1].set_title('Loser $ by bucket'); axs[1].tick_params(axis='x', rotation=20)
plt.tight_layout(); plt.savefig('backtest/results/diag_h_buckets.png', dpi=110)

fig2,ax = plt.subplots(figsize=(9,5))
ax.hist(losers.mfe*100, bins=40, alpha=0.6, label='losers MFE%')
ax.hist(winners.mfe*100, bins=40, alpha=0.6, label='winners MFE%')
ax.axvline(0.22, color='k', ls='--', label='friction 0.22%'); ax.legend(); ax.set_title('MFE distribution')
plt.tight_layout(); plt.savefig('backtest/results/diag_h_mfe.png', dpi=110)
print("done")
