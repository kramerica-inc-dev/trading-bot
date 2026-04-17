# Analysis — Where the Bot's Next Edge Comes From

**Date:** 2026-04-18
**Trigger:** ML Pattern Finder (competition round 2, agent #7) ran a logistic
regression on the seven regime-condition features against 12-bar forward
returns. AUC = **0.5030** — statistically indistinguishable from random.
**Implication:** the existing feature set cannot be tuned into an edge. Any
further threshold/weight work on it is gambler's fallacy. Real lift must come
from new information, new strategy surface, or new breadth.

This document captures the analysis that produced the decisions recorded in
`DECISIONS.md` on the same date. It is the reasoning trail; `DECISIONS.md` is
the terse decision log.

---

## Where profitable trading bots actually get edge

Profitable bots do not find edge in OHLCV indicators. They find it in one (or
more) of the following, ordered roughly by accessibility to a small retail
operator:

### 1. Information edge — data others don't trade on

- **Funding rates.** When perpetual funding goes extreme (>0.1%/8h or
  negative), it reliably reverses within 1-3 days. Lowest-hanging fruit for a
  retail perp bot. Free from every exchange API.
- **Open interest dynamics.** OI rising + price rising = real buying; OI
  falling + price rising = short squeeze (unsustainable). Divergences between
  OI and price are among the few things that survive out-of-sample.
- **Liquidation data.** Coinglass / exchange liquidation feeds. Large
  liquidation clusters create predictable wicks. Many bots hunt these
  explicitly.
- **Perp-spot basis.** The spread between perp and index/spot tells you
  positioning. Negative basis in a bull market = shorts over-positioned =
  squeeze fuel.
- **On-chain flows.** Stablecoin mints (USDT/USDC issuance), exchange
  inflows/outflows, whale wallet moves. Lead spot by hours.
- **Order book imbalance.** Not HFT-level — just top-of-book bid/ask size
  ratio aggregated over minutes. Shows where real resting liquidity is.

### 2. Structural edge — arbitrage, not prediction

- **Funding arbitrage (cash-and-carry).** Long spot + short perp when funding
  is positive. Capture funding yield without directional exposure. 10-30% APY
  historically on BTC/ETH.
- **Cross-exchange arb.** Latency + inventory game. Hard but real.
- **Basis trade.** Futures vs spot on dated futures. CME BTC futures premium
  is still a real edge vehicle.

### 3. Behavioral edge — fade the crowd

- **Liquidation hunting.** Price magnets toward stop clusters. Visible on
  Coinglass heatmaps.
- **Funding contrarian.** When retail is max-long (funding maxed), short.
  Single most consistent perp strategy that retail can execute.
- **Event fades.** CPI, FOMC, BTC halving — overreactions mean-revert within
  hours.

### 4. Cost / execution edge

- **Maker rebates over taker fees.** Flipping from 0.06% taker to 0.02% maker
  (or rebate on some venues) is a 0.08% round-trip saving in the idealized
  case. See Section "Revision note — Option B" below for why this estimate
  gets downgraded to ~0.04% in practice.
- **Smaller but more trades.** If edge is small-but-real, frequency
  compounds. Current 5m cadence is fine; paying taker both sides is not.

### 5. Portfolio / breadth edge

- **Cross-sectional momentum.** Rank 20+ assets by 1-week return, long top
  decile, short bottom. Works on crypto because the cross-section is
  inefficient.
- **Uncorrelated strategies.** Trend + mean-reversion + funding-fade running
  in parallel, capital-weighted by live Sharpe. No single strategy needs to
  be great.

**What retail bots with documented edge actually do:** almost universally,
funding-rate-based mean reversion + regime filter + tight execution. Jim
Simons: markets have "different physics" in different regimes — the actual
trick is not predicting direction, it's knowing when not to trade and sizing
up when signals align.

---

## Six concrete options for this bot

Ranked by effort × expected lift at the time of analysis.

### A. Add funding rate as primary signal (days, high lift)

Blofin exposes `/api/v1/market/funding-rate`. Two simple rules with 15+ years
of precedent in TradFi and 5+ in crypto:

1. **Fade extreme positive funding** — go short when 8h funding > 0.05% and
   RSI > 70
2. **Fade extreme negative funding** — go long when 8h funding < -0.02% and
   price at support

This is literally the #9 proposal from the 10-agent competition. It's not a
tweak to existing logic — it's a *new* signal that isn't in the AUC 0.503
pool.

### B. Switch to maker-only execution where possible (days, medium lift)

Post-only limit orders at bid/ask instead of market orders. Nominally saves
~0.08% per round trip. On current trade cadence that's a real number.
Trade-off: occasional missed fills in fast moves. **See revision note below
— this option was later downgraded and deferred.**

### C. Add OI divergence as a filter (days, medium lift)

Blofin exposes OI per symbol. Four-quadrant logic on 1h (N=12 bars on 5m):

| Price Δ | OI Δ    | Meaning              | Action             |
|---------|---------|----------------------|--------------------|
| Up      | Up      | Real buying          | Longs OK           |
| Up      | Down    | Short covering (weak)| Skip longs         |
| Down    | Up      | Real selling         | Shorts OK          |
| Down    | Down    | Long capitulation    | Skip shorts        |

Cuts trade count ~30% in typical backtests, but cuts losers more than winners.

### D. Add a mean-reversion strategy for chop regimes (weeks, potentially high lift)

The regime classifier already identifies chop. Currently the bot either
trades chop poorly or skips it entirely. In chop, Bollinger-band reversion
with tight stops has edge where trend-following doesn't. Structure: two
strategies, regime classifier routes capital. New surface area — doubles
monitoring and debugging load. Chop is ~40-50% of market time historically,
so the prize is large.

### E. Expand symbol universe, run cross-sectional (weeks, high lift)

Agent #8 from the competition. 15-20 Blofin perps (BTC, ETH, SOL, BNB, XRP,
DOGE, LINK, AVAX, MATIC, DOT, ADA, TRX, ATOM, NEAR, APT, ARB, OP, SUI, TIA,
LTC). Rank by 24h momentum + funding + OI composite. Long top 3, short bottom
3, rebalance every 4h. This is how Two Sigma / AQR make money in equities; it
works in crypto because crypto is even less efficient. Biggest project,
largest durable edge — requires multi-symbol data collection, portfolio
accounting, and a restructured backtest framework.

### F. Stop optimizing the current strategy (free, negative cost)

The AUC finding says any further threshold/weight work on the existing
seven-feature set is gambler's fallacy. Rearranging deck chairs. This isn't
an additional project; it's a commitment to *stop* a category of work so
attention can move to A-E. Now recorded in `DECISIONS.md` as a 3-month
moratorium with explicit banned/allowed lists.

---

## Initial recommendation

**Do A first** — funding rate signal. Highest expected value:
- Introduces genuinely new information into the predictor set
- Cheap to implement (days, not weeks)
- Empirical literature is unusually consistent
- Reversible — it's a gate first, active strategy later

**Then B** — maker-only execution — to cut the cost floor once more trades
flow through funding gate logic.

**Then evaluate** whether the base bot's edge has moved off 0.50 AUC with the
new features before going further into C/D/E.

---

## Revision note — Option B demoted (same day)

The initial ranking put B as priority #2. On closer inspection of the
realistic math, B dropped out of the near-term path. Recording the reasoning
here so the decision isn't silently lost.

**Revised fee-savings math:**

- Entries can be maker: saves 0.04% vs taker (0.02% vs 0.06%)
- Exits via server-side TP/SL are **triggered orders**, filled as takers
  (0.06%). They cannot be makers — a stop-loss that waits for a maker fill
  isn't a stop-loss.
- Realistic round-trip saving: **~0.04%, not 0.08%** — half the original
  estimate.

**Costs not originally weighed:**

1. **Adverse selection (maker's curse).** Limit orders disproportionately
   fill when price crosses through them — i.e., when the market is moving
   against the entry. Documented retail maker flow shows 15-30 bps of adverse
   selection. This likely eats most of the 4 bps saving.
2. **Missed fills in fast moves.** Exactly when the signal is strongest
   (breakouts, trend accelerations), post-only limits sit unfilled. Fills
   bias toward losers, misses winners.
3. **Implementation complexity.** Post-only timeout, partial-fill handling,
   replace-on-drift, fallback-to-taker-after-N-seconds. ~300 LoC of execution
   logic that can go wrong in live trading. Risk budget better spent
   elsewhere.

**Revised decision:** B is deferred. Revisit only after A is live and
measured. If A succeeds and fees become >20% of gross P&L, the math flips and
B becomes worth the complexity. Until then, premature optimization.

**Revised sequencing:** A → C → D → E, one at a time, each gating on the
previous being live and measured for at least 2 weeks. This is what
`DECISIONS.md` records.

---

## What this analysis does *not* cover

Deliberately out of scope for this document, flagged for future work:

- **On-chain data integration.** Stablecoin flows, exchange inflows, whale
  wallets. Mentioned in Section 1 but not scoped as an option. Worth
  revisiting after E if the cross-sectional strategy is live.
- **Liquidation heatmap integration.** Coinglass API. Could become option G.
- **Sentiment / news.** Hard to do well; deliberately omitted.
- **HFT / latency strategies.** Not accessible at our infrastructure level.
- **Cross-exchange arbitrage.** Requires capital on multiple venues;
  operational complexity outweighs retail-scale edge.

These are not gaps in the analysis — they are intentional deprioritizations.
Recording them here so they don't get rediscovered as "new ideas" in 3
months.
