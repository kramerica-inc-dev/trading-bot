# Axis B — Exit Geometry & Payoff Asymmetry

Strategy `advanced` / MultiIndicatorConfluence, BTC-USDT 5m, 185 trades (all long, ~all `range`/mean-reversion regime), $115 book, ~0.22% round-trip friction (0.06% fee + 0.05% slippage, both sides).

## 1. Payoff math

| Metric | Value |
|---|---|
| Win rate (actual) | 12.4% (23 / 185) |
| Avg win | +$0.054  /  +0.061% |
| Avg loss | −$0.241  /  −0.269% |
| Reward:Risk ( |avg win % / avg loss %| ) | **0.226** |
| Break-even win rate implied by R:R | **81.6%** |
| Gap (need 81.6%, have 12.4%) | −69 pp |

The geometry alone is unwinnable: a 0.23 R:R bet needs an 82% hit rate. Even a *perfect* signal at, say, 55–60% would still lose money with this payoff structure.

### Per exit reason — net vs gross

| exit_reason | n | net PnL | gross PnL | mean gross move | mean net % |
|---|---|---|---|---|---|
| stop_loss | 86 | −$29.11 | −$19.75 | −0.257% | −0.376% |
| take_profit | 68 | **−$2.60** | **+$4.74** | **+0.078%** | −0.042% |
| stale_trade | 28 | −$5.42 | −$2.47 | −0.100% | −0.220% |
| max_hold_bars | 3 | −$0.71 | −$0.40 | −0.146% | −0.266% |
| **total** | 185 | −$37.84 | −$18.28 | | |

**KEY FINDING — the take_profit bucket is gross-positive but net-negative.** TP "wins" make on average +0.078% of price before costs; the round-trip friction is ~0.22%. So every "winning" trade still books a ~−0.04% loss. The TP target is set *inside the friction floor*. Cause: for range trades `take_profit = min(bb_middle, entry + ATR·1.6)` and ATR is tiny on 5m BTC (median `atr_pct` ≈ 0.12%, p90 ≈ 0.20%), and the bb_middle cap usually binds first → effective TP ≈ 0.06–0.08%, i.e. ~1/3 of friction. SL is `entry − ATR·1.2` ≈ 0.15–0.20%, also tiny but at least clears friction so it actually books a loss. Net of the ~$18.3 of total friction, the *gross* edge is still −$18.3 — costs are exactly half the bleed, the signal supplies the other half.

## 2. MFE / MAE (BTC price excursion entry→exit)

| | mean | median |
|---|---|---|
| MFE (all trades) | +0.144% | +0.107% |
| MAE (all trades) | −0.197% | −0.181% |

- **Winners**: realized gross +0.181% vs MFE +0.332% → **exit-capture ratio ≈ 0.57** — winners are cut at roughly half their best price. But MFE itself is minuscule.
- **Losers**: realized gross −0.149% vs MAE −0.199% → **exit/MAE ≈ 0.62** — losers are stopped/staled about 60% of the way to their worst tick, i.e. mostly on the wick.
- **Whipsaw trades: 111 / 185 (60%)** have |MFE| and |MAE| both < 0.3% AND |gross move| < friction — pure round-trip noise the costs convert into a loss.
- **The signal has no room to work**: only 40/185 trades ever saw MFE > 0.22%, only **5** ever exceeded 0.5% favorable, and **zero** ever reached +1%. The entries put the bot into positions that simply don't move.
- `stale_trade` exits: avg MFE +0.10% then exit at −0.10% → gave back ~0.20% per trade (round trips), confirming the entries front-run nothing.

Plots: `backtest/results/diag_b_mfe_mae.png`, `diag_b_capture_scatter.png`, `diag_b_pnl_gross_net.png`.

## 3. Exit ablations (each = 1 backtest re-run)

| variant | trades | win rate | return % | Calmar | max DD % | alpha vs BH |
|---|---|---|---|---|---|---|
| **baseline** | 185 | 12.4% | −32.9% | −0.98 | 33.1% | −23.4% |
| (a) TP widened (ATR·300; bb_middle cap still binds for range) | 185 | 15.1% | −32.1% | −0.98 | 32.3% | −22.7% |
| (b) SL ×2 (ATR·2.4/3.6) | 184 | 15.2% | −32.6% | −0.98 | 32.8% | −23.1% |
| (b) SL ×3 (ATR·3.6/5.4) | 183 | 15.9% | −32.2% | −0.98 | 32.4% | −22.7% |
| (b) SL ×10 (ATR·12/18) | 179 | 15.1% | −30.6% | −0.99 | 30.7% | −21.1% |
| (c) time-exits off | 185 | 16.2% | −31.9% | −0.98 | 32.1% | −22.4% |
| (d) **passive hold** — strategy entries, SL/TP ≈ ±40%, no time-exit | 22 | 0% | **−0.27%** | −0.24 | 1.1% | **+9.2%** |
| (e) TP/SL ≈ off, only time-exit | 25 | 0% | −0.58% | −0.98 | 0.6% | +8.9% |

Notes: (d)/(e) generate far fewer trades because positions stay open (only one at a time). Their key message: stripped of *all* exit machinery, the entry signal is essentially flat (≈ −0.3% over the year, +9% alpha vs a −9.5% BH). Adding any of the existing exit components turns that flat line into a −32% loss; widening SL or killing time-exits each recovers only ~1–2 pp. **The single component that destroys the most value is the stop-loss bucket (−$29 of −$38), but it's a symptom: the SL only fires that often because (a) it sits ~0.18% away, inside normal 5m noise, and (b) the TP is even tighter so trades rarely escape upward first.** The TP/SL pair is jointly mis-scaled relative to friction and to bar noise.

## 4. Verdict

**Expectancy is negative by construction.** A 0.23 R:R demands 82% accuracy; the TP target lives inside the cost floor so even gross-winning trades book losses; SL sits inside 5m noise so it converts 86 trades into −0.38% each. None of this depends on signal quality — flatten the entry to a coin flip and the payoff geometry still loses.

But the ablations also show an exit-only fix **cannot** rescue this. The passive-hold counterfactual (best possible "let it run" exit) is only break-even, and that's because the entries have ~zero edge (MFE caps at 1%, 60% are noise). Widening SL/TP just trades a −32% loss for a ~−30% loss. To get a *positive* expectancy you'd need (i) a much larger R:R target — but the price moves needed (≥0.5–1% favorable, comfortably above friction) **almost never occur on this signal**, and (ii) therefore a different entry that actually catches moves big enough to clear costs.

### Implications for go/no-go

- **Fix (1) — fix exit structure: insufficient.** Necessary hygiene (any future strategy must size TP/SL well outside the ~0.22% friction band and outside 1-bar ATR noise), but on these entries it only moves −32% → ~−30%. NO-GO as a standalone fix.
- **Fix (2) — redesign entry signal: necessary but not sufficient on its own.** The entries don't produce moves large enough to overcome friction on 5m; a redesign would have to target a slower timeframe / larger expected move AND be paired with a proper exit geometry. Conditional GO only as part of a joint entry+exit+timeframe rebuild — which is effectively a new strategy.
- **Fix (3) — abandon this strategy family: favored.** The "mean-reversion at BB extremes on 5m BTC, ATR·1.2/1.6 brackets" design is structurally incompatible with the cost base: target moves (~0.06–0.08%) are 3× below friction and adverse noise (~0.2%) is above it. Combined with Fases 3/4/5/6 all NO-GO, axis B finds no exit-side rescue. Recommend abandoning the family; if anything is salvaged it is the *constraint* "trade something with expected favorable move ≥ ~0.5% and place brackets accordingly", which points to a higher timeframe / different instrument, not a patch.
