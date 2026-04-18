# Plan E — Final decision doc

**Date:** 2026-04-18
**Status:** Validated. Proceed to paper-trade (P1 policy: 2-4 weeks).
**Commit:** 78686d2

## TL;DR

Cross-sectional reversal on 10-asset crypto universe, 24h rebalance, 72h
lookback, with rank hysteresis (k_exit=6), passes in-sample (Sharpe +0.81 taker)
**and** walk-forward (Sharpe +1.55 taker on Q1 2026 out-of-sample). Gate met.

At a realistic 50% maker fill rate, Sharpe lifts to ~1.4 in-sample / ~2.3 OOS.

Deploy $5k initial. Engineer paper-trade runner first. No live capital until
P1 window (2-4 weeks paper) completes.

## Signal and rules

| Parameter | Value |
|---|---|
| Universe | BTC, ETH, SOL, XRP, BNB, DOGE, ADA, AVAX, DOT, LINK (Blofin perps) |
| Bar | 1h |
| Signal | `log(close[t] / close[t-72])` — 72-hour cumulative log return |
| Sign | **REV** (multiply by -1 — we long the laggers, short the leaders) |
| Rebalance cadence | Every 24h at UTC 00:00 |
| Selection | Rank cross-sectionally, long top 3 / short bottom 3 |
| **Hysteresis** | Retain an existing leg while it sits inside the top/bottom 6 (`k_exit=6`). Only evict if rank falls outside the band. |
| Sizing | Equal-weight 10% of equity per leg → 60% gross exposure |
| Stops | None (portfolio-level DD is self-managed by rebalance logic) |
| Account | $5,000 initial |

## Why this configuration

- **REV over MOM:** sweep confirmed short-horizon (≤1 week) cross-sectional
  reversal is the edge in crypto 1h data, matching equities literature. MOM
  with same lookback/cadence loses 30%+ annually.
- **72h lookback:** robust region. 24h is noisy, 720h (30d) has a single
  k_exit sweet spot that's clearly overfit (neighbors flip negative).
- **24h rebalance:** 4h cadence pays 3–10× fees for ~no signal improvement.
  Weekly (168h) wasn't meaningfully tested in the current sweep (bug noted)
  but daily is already at the turnover sweet spot with hysteresis.
- **k_exit=6:** the θ-refinement grid showed k=6 and k=7 both give Sharpe
  ~0.81 taker — stable plateau, not a lucky peak. k=3 (no hysteresis)
  gives Sharpe 0.27. k=8 drops back to 0.21.

## What the walk-forward told us

Train = Apr–Dec 2025. Test = Jan–Apr 2026 (~3.5 months OOS, held out).

At selected k=6, taker execution:
- Train Sharpe +0.57, Return +4.0%, DD -9.4%
- **Test Sharpe +1.55, Return +4.0% (~14% annualized), DD -3.9%**

At F=0.5 maker:
- Train Sharpe +1.11, Return +8.3%, DD -8.1%
- **Test Sharpe +2.28, Return +5.9% (~20%+ annualized), DD -3.1%**

**Honest caveat:** *every* k_exit showed OOS Sharpe > IS Sharpe, which means
Q1 2026 was a favorable regime for cross-sectional reversal rather than pure
strategy robustness. Paper-trading window needs to include at least some
less-favorable weeks to confirm expected-case performance, not just regime-
tailwind performance.

## What was ruled out on the way

- **Plan D-ζ mean reversion** (single-asset, classifier-gated): closed FAIL.
  Even with strategy-aligned target, conditional hit rate caps at 33%.
  Geometry forces negative EV before fees. Don't revisit.
- **MOM sign** at any lookback/rebalance combo: net-negative across all 11
  configs. Cross-sectional momentum at ≤1-week horizons is anti-edge in this
  universe.
- **4h rebalance:** fee share 60-300% at this cadence regardless of signal.
- **Signal blending (72h + 720h):** underperformed either alone.
- **Shorter lookback (6h, 24h):** 6h is pure noise. 24h was tested widely,
  always underperforms 72h.

## Engineering requirements for paper-trade

New module (not a modification of the existing `trading_bot.py` — different
architecture). Proposed file layout:

```
scripts/plan_e_runner.py            # main runner: paper + live modes
config/config.plan-e.json           # tunable params
state/plan_e_portfolio.json         # persisted portfolio state
state/plan_e_trades.log             # JSONL append log of every rebalance
```

Components:
1. **Multi-asset data fetcher** — periodically pull last ~100 hours of 1h
   candles for each of the 10 assets. Reuse `DataCollector` logic.
2. **Signal computer** — `log(close[-1] / close[-73])` per asset, rank
   cross-sectionally.
3. **Rebalance scheduler** — fire at UTC 00:00 daily. Cron-friendly
   `--once` mode + systemd-friendly `--loop` mode.
4. **Hysteresis position selector** — identical to the θ-refinement logic
   (keep legs in top-6, fill gaps from top-3).
5. **Paper executor** — simulate fills at the rebalance bar's last close,
   update `state/plan_e_portfolio.json`, append to `state/plan_e_trades.log`.
6. **Metrics** — every rebalance: equity, P&L since start, P&L since prior
   rebalance, positions, fees paid. Produce a rolling summary every week.

Components explicitly NOT in this first cut:
- Live execution (takes paper-trade success first).
- Maker execution (taker is good enough for the gate; maker is optimization).
- Multiple timeframe fallbacks / regime switches.
- Per-asset risk sizing / ATR-based position sizes (equal-weight is enough).

## Paper-trade plan (P1 policy)

1. Deploy runner on the Proxmox LXC that previously hosted the old bot.
2. Old BTC-USDT bot stays stopped. Plan E runs alongside, paper-mode only.
3. Minimum 2 weeks paper. Extend to 4 if:
   - OOS Sharpe drops below 0.5 in any rolling 2-week window.
   - Any single leg shows >20% slippage vs assumption.
   - Signal enters a regime shift (e.g., rolling 7-day return of the
     long/short basket goes sharply negative).
4. Go/no-go review at end of paper window:
   - **Go:** Flip to live with $5k, $25-30 per leg, start at 10% notional
     per leg. Monitor weekly, quarterly Sharpe check.
   - **No-go:** Extend paper or kill project.

## Risks / known unknowns

- **Regime dependence:** Q1 2026 test period was favorable. Paper window
  may hit a less-favorable regime — that's a feature, not a bug.
- **Liquidity:** smaller assets (DOT, LINK, ADA, DOGE) have thinner order
  books than BTC/ETH. Real slippage might exceed the 5bps assumption on
  those during rebalance, especially for the short leg.
- **Data gaps / exchange outages:** rebalance skipped → positions drift.
  Need conservative handling: keep existing positions if fresh data for
  ≥50% of universe is missing.
- **Min-notional constraint:** $500/leg at $5k equity is safe for all 10
  assets' min-contract sizes on Blofin. If equity drops to $2.5k (-50%),
  some legs will fall below minimum — runner must detect and halt gracefully.

## Timeline

- **Now:** implement runner (this session, ~few hours engineering).
- **Day 0 of paper:** deploy to LXC, begin fetching and rebalancing daily.
- **Day 14 checkpoint:** review rolling Sharpe, slippage realism, rebalance
  logs. Decide extend / go-live / kill.
- **Day 28 (if extended):** same review.
- **Go-live decision:** only with explicit sign-off from user.
