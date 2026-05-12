#!/usr/bin/env python3
import pandas as pd, numpy as np, json, matplotlib
matplotlib.use('Agg'); import matplotlib.pyplot as plt

CV = 0.001
FEE = 0.0006
SLIP = 0.05/100.0
INIT = 115.0

t = pd.read_csv('backtest/results/diag_trades.csv')
n = len(t)
# entry_price/exit_price in csv are POST-slippage fill prices.
# Recover pre-slippage mid: buy entry slipped up by SLIP, long exit slipped down by SLIP.
ep_fill = t['entry_price'].values
xp_fill = t['exit_price'].values
size = t['size'].values
notional_entry = size*CV*ep_fill
notional_exit  = size*CV*xp_fill

# Fees actually charged
entry_fee = FEE*notional_entry
exit_fee  = FEE*notional_exit
total_fees = (entry_fee+exit_fee).sum()

# Slippage cost: difference between fill and mid.
# entry mid = ep_fill/(1+SLIP); buy paid extra = ep_fill - mid = ep_fill*SLIP/(1+SLIP)
ep_mid = ep_fill/(1+SLIP)
xp_mid = xp_fill/(1-SLIP)
slip_entry_cost = (ep_fill-ep_mid)*size*CV
slip_exit_cost  = (xp_mid-xp_fill)*size*CV
total_slip = (slip_entry_cost+slip_exit_cost).sum()

# Gross PnL (no fees, but using fill prices) vs the recorded net pnl
gross_pnl_fills = ((xp_fill-ep_fill)*size*CV).sum()
net_pnl = t['pnl'].sum()
# sanity: net = gross_fills - fees
print("net recorded", net_pnl, "gross(fills)-fees", gross_pnl_fills-total_fees)

# Gross PnL using MID prices (i.e. zero-cost counterfactual on the same trades)
gross_pnl_mid = ((xp_mid-ep_mid)*size*CV).sum()

total_friction = total_fees + total_slip
# check: gross_mid - friction ~= net
print("gross_mid - friction", gross_pnl_mid-total_friction, "vs net", net_pnl)

avg_notional = notional_entry.mean()
cost_per_trade = total_friction/n
print(f"\n=== COST BREAKDOWN (n={n} trades, {ep_fill.mean():.0f} avg px) ===")
print(f"Total fees:       ${total_fees:.2f}")
print(f"Total slippage:   ${total_slip:.2f}")
print(f"Total friction:   ${total_friction:.2f}")
print(f"Funding:          not modelled in Backtester")
print(f"Net PnL recorded: ${net_pnl:.2f}  ROI {net_pnl/INIT*100:.1f}%")
print(f"Gross PnL (mid, zero-cost, same trades): ${gross_pnl_mid:.2f}  ROI {gross_pnl_mid/INIT*100:.1f}%")
print(f"|net PnL| = ${abs(net_pnl):.2f}; friction as % of |net|: {total_friction/abs(net_pnl)*100:.0f}%")
print(f"|gross PnL mid| = ${abs(gross_pnl_mid):.2f}; friction as % of |gross|: {total_friction/abs(gross_pnl_mid)*100:.0f}%")
print(f"Avg trade notional: ${avg_notional:.2f}")
print(f"Cost per trade: ${cost_per_trade:.4f} = {cost_per_trade/avg_notional*100:.3f}% of notional")
print(f"  fees/trade ${total_fees/n:.4f}, slip/trade ${total_slip/n:.4f}")

# Per-trade gross move (mid-to-mid abs)
gross_move = np.abs(xp_mid/ep_mid - 1.0)
# also fill-to-fill realized move
realized_move = np.abs(xp_fill/ep_fill - 1.0)
rt_cost_pct = 2*FEE + 2*SLIP   # ~0.0022
print(f"\nRound-trip cost threshold: {rt_cost_pct*100:.3f}%")
print("Gross move (mid-to-mid) percentiles (%):")
for p in [10,25,50,75,90,95]:
    print(f"  p{p}: {np.percentile(gross_move,p)*100:.3f}")
print(f"  mean: {gross_move.mean()*100:.3f}  median: {np.median(gross_move)*100:.3f}")
doa = (gross_move < rt_cost_pct).sum()
print(f"Dead-on-arrival (gross move < {rt_cost_pct*100:.2f}%): {doa}/{n} = {doa/n*100:.0f}%")
# also where the move never even covered ONE side cost
doa1 = (gross_move < (FEE+SLIP)).sum()
print(f"  move < one-way cost ({(FEE+SLIP)*100:.2f}%): {doa1}/{n}")

# Gross expectancy per trade: mean of mid-to-mid pct move SIGNED (all buys so sign = direction)
# signed gross return = xp_mid/ep_mid - 1
signed_gross = (xp_mid/ep_mid - 1.0)
print(f"\nGross expectancy per trade (mean signed mid-to-mid move): {signed_gross.mean()*100:.4f}%")
print(f"  median signed: {np.median(signed_gross)*100:.4f}%")
print(f"  net pnl_pct mean (recorded, /notional incl 2x leverage-ish? actually pnl/entry_value): {t['pnl_pct'].mean():.4f}%")
# net expectancy in $:
print(f"  net $ per trade: ${net_pnl/n:.4f}")
print(f"  gross-mid $ per trade: ${gross_pnl_mid/n:.4f}")

# Counterfactuals analytically (same trades, same sizes)
def roi(pnl): return pnl/INIT*100
# zero cost: use mid prices, no fees
cf0 = gross_pnl_mid
cf_half = gross_pnl_mid - total_friction*0.5
cf_real = net_pnl
def wr(pnls): return (pnls>0).mean()*100
pnl_real = t['pnl'].values
pnl_mid = (xp_mid-ep_mid)*size*CV
pnl_half = (xp_fill-ep_fill)*size*CV - (entry_fee+exit_fee)*0.5  # approx half fee, full slip already in fills...
# better half-cost: half fees AND half slippage -> use prices halfway between mid and fill
ep_half = ep_mid*(1+SLIP/2); xp_half = xp_mid*(1-SLIP/2)
pnl_half = (xp_half-ep_half)*size*CV - (FEE/2)*(size*CV*ep_half + size*CV*xp_half)
cf_half = pnl_half.sum()
print(f"\n=== COUNTERFACTUALS (same 185 trades) ===")
print(f"(a) zero cost:     PnL ${pnl_mid.sum():.2f}  ROI {roi(pnl_mid.sum()):+.1f}%  WR {wr(pnl_mid):.1f}%")
print(f"(b) half cost:     PnL ${cf_half:.2f}  ROI {roi(cf_half):+.1f}%  WR {wr(pnl_half):.1f}%")
print(f"(c) realistic:     PnL ${pnl_real.sum():.2f}  ROI {roi(pnl_real.sum()):+.1f}%  WR {wr(pnl_real):.1f}%")

# turnover
days = (pd.to_datetime(t['exit_time'].iloc[-1]) - pd.to_datetime(t['entry_time'].iloc[0])).days
print(f"\nSpan ~{days}d, {n} trades = {n/days:.2f}/day; mean bars_held {t['bars_held'].mean():.1f}")

# Plot histogram
fig,ax=plt.subplots(1,2,figsize=(12,4))
ax[0].hist(gross_move*100, bins=40, color='steelblue', edgecolor='k', alpha=0.8)
ax[0].axvline(rt_cost_pct*100, color='red', ls='--', label=f'round-trip cost {rt_cost_pct*100:.2f}%')
ax[0].set_xlabel('|gross move| mid-to-mid (%)'); ax[0].set_title('Per-trade gross move vs cost floor'); ax[0].legend()
ax[1].hist(signed_gross*100, bins=40, color='gray', edgecolor='k', alpha=0.8)
ax[1].axvline(0, color='k'); ax[1].axvline(signed_gross.mean()*100, color='red', ls='--', label=f'mean {signed_gross.mean()*100:.3f}%')
ax[1].set_xlabel('signed gross move (%)'); ax[1].set_title('Gross expectancy distribution'); ax[1].legend()
plt.tight_layout(); plt.savefig('backtest/results/diag_c_gross_move.png', dpi=110)

# cumulative: net vs zero-cost equity
fig,ax=plt.subplots(figsize=(10,4))
ax.plot(np.cumsum(pnl_real)+INIT, label=f'realistic (net ${pnl_real.sum():.1f})')
ax.plot(np.cumsum(pnl_mid)+INIT, label=f'zero-cost (${pnl_mid.sum():.1f})')
ax.plot(np.cumsum(pnl_half)+INIT, label=f'half-cost (${pnl_half.sum():.1f})', ls='--')
ax.axhline(INIT, color='k', lw=0.5); ax.set_xlabel('trade #'); ax.set_ylabel('equity $'); ax.legend(); ax.set_title('Counterfactual equity (same trade sequence)')
plt.tight_layout(); plt.savefig('backtest/results/diag_c_counterfactual.png', dpi=110)
print("\nplots saved.")
