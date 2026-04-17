# Plan A — Funding-Rate Signal Integration

**Status:** Proposed
**Created:** 2026-04-18
**Gates on:** DECISIONS.md 2026-04-18 (feature freeze + A→C→D→E path)
**Gate for:** Plan C (OI divergence) — do not start C until A is live and measured

---

## Goal

Introduce funding rate as a **new information source** for the trading bot. The
AUC 0.503 diagnostic on the existing seven-feature set proved the current
predictor has no measurable edge on forward returns. Funding rate is one of
the few retail-accessible signals with consistently documented predictive
power in crypto perpetuals.

Initial deployment is a **binary gate**, not an active strategy: skip entries
when the crowd is over-positioned in the same direction we'd be entering. The
funding-fade active strategy (take the opposite side at extremes) is
explicitly deferred until the gate is validated live.

## Success criteria

After 4 weeks of live operation with the funding gate enabled:
- Trade count reduction is modest (target 20-40%, not 80%+)
- Win rate on remaining trades is equal or higher than pre-gate baseline
- No increase in max drawdown
- Sharpe ratio measurably improved, or explicit decision to revert
- **Expectancy per trade** (avg_win × WR − avg_loss × (1−WR)) must be
  positive and higher than pre-gate baseline
- **Net P&L after fees** (not just gross) must be positive and improved

Failure criteria (revert and reassess): win rate degrades, gate suppresses
>60% of trades, no measurable effect on Sharpe after 4 weeks, or expectancy
per trade turns negative after fees.

---

## Six-step execution plan

### Step 1 — Add Blofin funding API client methods

**Scope:** extend `scripts/blofin_api.py` with two read-only methods:
- `get_funding_rate(instId)` — current funding rate, next funding time
- `get_funding_rate_history(instId, before=None, after=None, limit=100)` — historical funding

Blofin endpoints: `/api/v1/market/funding-rate`,
`/api/v1/market/funding-rate-history`. Both public, no auth required.

**Deliverable:** methods present, unit-testable, no integration with strategy
yet. Zero trade impact.

**Effort:** half day.

---

### Step 2 — Historical funding backfill

**Scope:** new script `backtest/funding_backfill.py`. One-shot: pulls
historical funding from Blofin (paginated) for BTC-USDT going back at least
12 months, writes to `backtest/data/funding_btc_usdt.csv` (columns:
`fundingTime`, `fundingRate`, `instId`).

If Blofin's history lookback is insufficient, fall back to Coinglass free API
for the missing range.

**Deliverable:** CSV with ≥12 months of 8-hourly funding data, timezone-aligned
to existing candle CSVs.

**Effort:** half to one day.

---

### Step 3 — Backtest baseline with funding gate overlay

**Scope:** extend `backtest/backtester.py` (or write a sibling script) to
forward-merge funding values onto each candle (last-known value per bar).
Re-run the existing live strategy on historical data with and without a
funding gate applied as a pre-entry filter:

- Skip long entries if `funding_rate > max_long_funding_pct` (try 0.03%, 0.05%, 0.08%)
- Skip short entries if `funding_rate < min_short_funding_pct` (try -0.01%, -0.02%, -0.03%)

Compare Sharpe, max drawdown, win rate, trade count across the grid.

**Decision point:** does any threshold pair improve Sharpe without cutting
trades >50%? If yes, proceed. If no, stop and re-examine — maybe funding
doesn't help *this* strategy and we skip to Plan C or Plan E directly.

**Deliverable:** `backtest/results/funding_gate_grid.csv` with metrics per
threshold pair, plus a 1-page written conclusion recommending specific
thresholds (or recommending we abandon A).

**Effort:** one to two days.

---

### Step 4 — Live funding poller

**Scope:** new module `scripts/funding_data.py`. Polls
`get_funding_rate(BTC-USDT)` every hour (funding settles every 8h on Blofin,
so hourly is over-sampled but cheap). Writes latest value + timestamp to
`memory/funding-state.json`. Exposes a `get_current_funding()` helper for the
strategy to call.

Integrates as a background task under the existing `async_runtime.py`
scheduler.

**Deliverable:** funding values updating live in `memory/funding-state.json`,
verifiable via `tail` or dashboard. Still no trade impact.

**Effort:** one day.

---

### Step 5 — Strategy integration in shadow mode

**Scope:** wire the funding gate into `scripts/advanced_strategy.py`. New
config block:

```json
"strategy": {
  "funding_filter": {
    "enabled": false,
    "shadow_mode": true,
    "max_long_funding_pct": 0.05,
    "min_short_funding_pct": -0.02,
    "stale_threshold_minutes": 60
  }
}
```

In `shadow_mode: true`, the gate logs what it **would** have done
(`"funding_gate": "skip_long" | "skip_short" | "pass"`) but does not change
trade decisions. Staleness guard: if funding data is older than
`stale_threshold_minutes`, disable the gate and log a warning (fail-open).

Deploy to prod. Run for one week in shadow mode. Compare shadow decisions
against actual trades taken.

**Deliverable:** one week of shadow logs showing the gate fires at reasonable
rates and aligns with backtest expectations.

**Effort:** one day of code, one week of observation.

---

### Step 6 — Enable live, measure for 4 weeks, gate C/D/E on result

**Scope:** flip `enabled: true` on prod config. Restart service. Monitor for
4 weeks:

- Week 1: daily check — gate firing rate, win rate on filtered vs unfiltered
  trades, any anomalies
- Weeks 2-4: weekly review of the full metric set below
- End of week 4: write `backtest/results/funding_gate_live_report.md` with
  the full metric set and a **go / no-go decision on starting Plan C**.

**Full metric set for weekly and final reports:**

*Baseline metrics (pre-gate and post-gate comparison):*
- Sharpe ratio
- Max drawdown
- Win rate
- Trade count

*Per-trade quality:*
- **Expectancy per trade** — (avg_win × WR) − (avg_loss × (1−WR)). The
  single number that determines whether a strategy is worth running. Must
  rise with the gate on; if it falls, the gate is filtering winners.
- Average win size, average loss size (components of expectancy — track both
  to see *why* expectancy moves)

*Gross vs. net economics:*
- **Gross P&L** — raw mark-to-market return without fee/funding debits
- **Net P&L** — after taker fees on entry + TP/SL exit, and after cumulative
  funding paid/received on open positions
- **Fee share of gross alpha** — (total fees paid) / (gross P&L). If fees
  eat >30% of gross, the strategy is running too hot for its edge; if
  >60%, B (maker execution) becomes the next priority regardless of
  DECISIONS.md sequencing.
- Funding paid/received per held position (funding side-effect of the bot
  itself holding longs/shorts across 8h settlements)

*Gate-quality diagnostics:*
- **Filtered winners vs filtered losers** — of the trades the gate *skipped*,
  how many would have won vs lost at the 1R/TP-hit level? If the gate skips
  more winners than losers, it is destroying edge — revert immediately.
  Computed by forward-simulating skipped entries with the same TP/SL logic.
- Gate firing rate (% of signals suppressed) broken down by direction

*Regime-segmented gate impact:*
- Split all of the above metrics by **regime classifier output**
  (bull_trend, bear_trend, chop, breakout, etc. — whatever the live
  classifier emits). The gate may help in some regimes and hurt in others;
  reporting only aggregates can hide strong regime-conditional effects.
  If results are strongly heterogeneous, the funding gate may belong inside
  the regime router rather than as a global filter — log this as a finding
  for a subsequent refinement, do not act on it inside Plan A.

**Tooling:** Step 6 is primarily calendar-bound (4 weeks of live data) but
the report generator itself is a concrete deliverable built in advance —
`scripts/funding_gate_report.py` reads `memory/trading-log.jsonl` plus the
funding cache and emits all of the above as a single markdown report. This
tool is built alongside Step 5 so weekly reports can be run immediately
once shadow mode begins.

If go: begin Plan C (OI divergence) using the same data-infrastructure
pattern established in Steps 1-4.
If no-go: append a new entry to `DECISIONS.md` documenting what was learned,
and re-prioritize between C, D, E.

**Deliverable:** reporting script + weekly reports + final written report,
decision recorded in DECISIONS.md, next plan scoped.

**Effort:** ~1 day to build the reporting script, then ongoing monitoring.

---

## Out of scope for Plan A

Explicitly deferred to future plans to avoid scope creep:

- **Funding-fade active strategy** (enter opposite side at extreme funding).
  Separate strategy, separate risk allocation. Revisit after Plan A gate is
  validated.
- **Multi-symbol funding** (funding signals on ETH, SOL, etc.). Belongs to
  Plan E.
- **Funding + OI composite signals**. Belongs to Plan C.
- **Maker-only execution to offset any fee increase from more trades**.
  Addressed separately (deferred — see DECISIONS.md discussion of option B).

---

## Rollback plan

At any step, if something breaks:
- Steps 1-4 are read-only — no rollback needed, just stop running.
- Step 5 shadow mode — no trade impact, just stop logging.
- Step 6 live — set `strategy.funding_filter.enabled: false` in prod config,
  restart service. Zero state to reverse.
