import pandas as pd, numpy as np
pd.set_option('display.width', 200)

tr = pd.read_csv('backtest/results/diag_trades.csv')
ba = pd.read_csv('backtest/results/diag_bars.csv')
tr['entry_time'] = pd.to_datetime(tr['entry_time'], utc=True)
ba['ts'] = pd.to_datetime(ba['ts'], utc=True)
tr = tr.rename(columns={'regime':'strat_label'})
ba = ba.rename(columns={'regime':'obj_regime'})

ba_s = ba.sort_values('ts')
tr_s = tr.sort_values('entry_time')
m = pd.merge_asof(tr_s, ba_s[['ts','obj_regime','btc_close']], left_on='entry_time', right_on='ts',
                  direction='backward', tolerance=pd.Timedelta('3h'))
print("merged rows:", len(m), "null obj_regime:", m['obj_regime'].isna().sum())

def stats(g):
    return pd.Series({'n': len(g),
        'win_rate': round((g['pnl']>0).mean(),3),
        'mean_pnl_pct': round(g['pnl_pct'].mean(),3),
        'total_pnl': round(g['pnl'].sum(),2)})

print("\n=== strategy label counts ===")
print(m['strat_label'].value_counts())
print("\n=== objective regime at entry counts ===")
print(m['obj_regime'].value_counts())
print("\n=== 1. cross-tab strat_label x obj_regime (counts) ===")
print(pd.crosstab(m['strat_label'], m['obj_regime']))
print("\n=== 1b. by obj_regime: stats ===")
print(m.groupby('obj_regime').apply(stats))
print("\n=== 1c. by strat_label x obj_regime: stats ===")
print(m.groupby(['strat_label','obj_regime']).apply(stats))

# fraction of 'range' entries during bull/bear
rng = m[m['strat_label']=='range']
print(f"\n'range' entries: {len(rng)}; during bull/bear (trending): {(rng['obj_regime']!='sideways').sum()} = {(rng['obj_regime']!='sideways').mean():.1%}")

print("\n=== 2. bb_pos buckets (ind_bb_pos) ===")
m['bb_bucket'] = pd.cut(m['ind_bb_pos'], [-0.01,0.1,0.3,0.5,0.7,0.9,1.01], labels=['0-.1','.1-.3','.3-.5','.5-.7','.7-.9','.9-1'])
print(m.groupby('bb_bucket').apply(stats))
print("\n=== 2b. RSI buckets (ind_rsi_value) ===")
m['rsi_bucket'] = pd.cut(m['ind_rsi_value'], [0,30,40,45,50,55,60,100], labels=['<30','30-40','40-45','45-50','50-55','55-60','>60'])
print(m.groupby('rsi_bucket').apply(stats))
print("\nin_midzone counts:", m['ind_in_midzone'].value_counts().to_dict())
print(m.groupby('ind_in_midzone').apply(stats))
print("\nbb_pos describe:"); print(m['ind_bb_pos'].describe())
print("rsi_value describe:"); print(m['ind_rsi_value'].describe())

print("\n=== 3. regime_confidence deciles ===")
m['conf_dec'] = pd.qcut(m['ind_regime_confidence'], 5, duplicates='drop')
print(m.groupby('conf_dec').apply(stats))
print("\n=== efficiency_ratio buckets ===")
m['er_bucket'] = pd.qcut(m['ind_efficiency_ratio'], 5, duplicates='drop')
print(m.groupby('er_bucket').apply(stats))
print("\nscore composition: mean scores")
print(m[['ind_regime_score_bull','ind_regime_score_bear','ind_regime_score_range','ind_regime_score_chop']].describe())
# correlation conf/er with pnl_pct
print("\ncorr regime_confidence vs pnl_pct:", round(m['ind_regime_confidence'].corr(m['pnl_pct']),3))
print("corr efficiency_ratio vs pnl_pct:", round(m['ind_efficiency_ratio'].corr(m['pnl_pct']),3))
print("corr bb_pos vs pnl_pct:", round(m['ind_bb_pos'].corr(m['pnl_pct']),3))
print("corr rsi_value vs pnl_pct:", round(m['ind_rsi_value'].corr(m['pnl_pct']),3))

# what does objective regime look like over whole period - share of bars
print("\n=== objective regime share of all bars ===")
print(ba['obj_regime'].value_counts(normalize=True))

m.to_csv('backtest/results/diag_d_merged.csv', index=False)
