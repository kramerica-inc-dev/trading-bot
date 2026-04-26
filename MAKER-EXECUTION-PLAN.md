# Plan E — Maker-execution upgrade (Plan B revisited)

**Status:** Design only. Code is gated on Plan E paper-PASS (P1 policy).
**Created:** 2026-04-26
**Supersedes:** "Plan B — deferred indefinitely" judgment in DESIGN-PRINCIPLES.md §4.3
**Prereq:** circuit breaker landed (commit `1020f51`); paper-PASS at end of P1
window with taker baseline.

---

## TL;DR

Plan E currently uses taker execution (round-trip cost ≈ 22 bps). The
η-maker probe (`backtest/results/PLAN-E-eta-maker.md`) showed that a 50%
maker fill rate lifts OOS Sharpe from **+1.55 → +2.41** with no DD
increase, no liquidation risk, no funding-cost exposure. That is a larger
risk-adjusted lift than any feasible financial leverage tier on the same
strategy.

This document specifies (a) the live executor design, (b) the paper
sensitivity instance `plan-e-maker50` that runs *now* alongside the
existing flotte to gather live cost-side data, and (c) the gate criteria
for promoting maker execution from paper to live.

The design is intentionally conservative: every order is post-only with
explicit timeout fallback to taker, and the runner tracks per-leg
maker-fill rate so the η-maker assumption can be validated against
production.

---

## Why now (and why not earlier)

The DESIGN-PRINCIPLES.md verdict on Plan B (maker-only execution) was
"deferred indefinitely" because:

1. Realistic round-trip fee saving was estimated at ~4 bps over taker
2. Adverse-selection risk was judged to eat the savings

Both points stand for a **single-asset trend strategy**. They do not
stand for **Plan E**:

- **Cost magnitude:** Plan E backtest at 50% maker shows a ~6 bp/round
  reduction (11 → 5 bps/side = 12 → 10 bps/round… correcting: 22 → 10
  bps round-trip per the η table). At Plan E's rebalance frequency
  (~261 turnovers/yr), the gross savings are ≥ $300/yr per $5k account
  vs. taker.
- **Adverse-selection profile:** Plan E enters a basket precisely
  *because* the basket has moved 72h in one direction; the entry
  hypothesis is reversion. A post-only at last-bar close that takes
  60s+ to fill is sampling from a population where price drift IS the
  edge. Adverse selection works in our favor here, not against — we
  enter cheaper than market when momentum continues briefly, and only
  miss fills when reversion has already started (in which case taker
  fallback at the new price is still the right entry).
- **Latency budget:** 24h rebalance cadence gives an enormous queue —
  even minutes-long timeout/replace cycles fit cleanly.

These are strategy-specific reasons. Plan B's verdict on the legacy
single-asset bot stays. Maker execution becomes Plan E's first
post-paper optimization.

---

## Paper instance (lands now, before live executor)

`configs/plan-e-maker50.json` — same baseline as `plan-e-base` but
fee_rate / slippage_rate set to the η-maker F=0.5 blend (≈ 5 bps/side):

```
fee_rate:      0.00025   # blend of -0.0001 maker + 0.0006 taker @ F=0.5
slippage_rate: 0.00025   # blend of  0.0000 maker + 0.0005 taker @ F=0.5
                         # round-trip cost = (0.00025 + 0.00025) * 2 = 10 bps
```

This is a **simulation-by-cost-knob**, not a real maker test. The paper
executor still fills at last-bar close; the only thing that changes is
the cost deducted. What it tells us:

- Whether the η-maker projected Sharpe lift survives in the live
  paper-window regime (Q2 2026, currently underway).
- The fee-burn delta vs `plan-e-base` over the same dates.

What it does NOT tell us:

- Whether 50% maker fill is achievable in production — that requires
  the live executor below.
- Adverse-selection magnitude — same caveat.

`plan-e-maker50` runs alongside the existing 8 instances on shared
market data, circuit breaker active, identical to `plan-e-base` in every
other parameter.

---

## Live executor — design

Implemented in `scripts/plan_e_runner.py` as a new mode `live` (or as a
new flag `--executor maker`). Paper-mode behavior unchanged.

### Order placement

1. Compute target legs identically to paper.
2. For each leg to open or close, place a **post-only limit order** at
   the rebalance bar's last close (or top-of-book mid, see below).
3. Wait `MAKER_TIMEOUT_SEC` (initial value: 60s) for fill.
4. On full fill: record `fill_type=maker`, fees from rebate.
5. On partial fill: record `partial_qty`, leave remainder as limit for
   another `MAKER_TIMEOUT_SEC` cycle, max `MAKER_RETRIES` (initial: 2).
6. On no fill or remainder after retries: cancel + place market (taker)
   for whatever's left, record `fill_type=taker_fallback`.

### Price reference: top-of-book vs last close

Two candidate references for the limit price:

| Option | Pros | Cons |
|--------|------|------|
| Last bar's close | Deterministic, identical to paper math | Stale by 0–60s; price may have moved away |
| Top-of-book (best bid for sell, best ask for buy, post-only crossable) | Tighter to current state | Two extra API calls per leg per attempt |

**Recommendation:** start with **last close** for v1 (simpler, matches
backtest). If the live fill rate is materially below 50%, upgrade to
top-of-book mid in v2.

### Per-leg fill accounting

Append to `state/<instance>/trades.log` per rebalance event:

```json
{
  "ts": "...",
  "action": "rebalance",
  "executor": "maker",
  "fills": [
    {"symbol": "BTC-USDT", "side": "long", "qty": 0.05,
     "filled_maker": 0.04, "filled_taker": 0.01,
     "maker_avg_px": 67234.5, "taker_avg_px": 67241.0,
     "fee_paid": 0.18, "slippage_bps": 1.0}
  ],
  "maker_fill_rate": 0.83,
  ...
}
```

This is the data we need to validate or refute the η-maker assumption.

### Reconciliation

P7 says fail-closed. Maker execution adds two failure modes:

- **Stuck open order:** runner crashes between place + ack. Reconciler
  on startup must enumerate open orders for the configured universe and
  cancel any belonging to plan-e-runner (use a unique `clientOrderId`
  prefix, e.g. `pe-<instance>-<ts>`).
- **Partial fill not closed:** runner timed out, but a fill arrived
  during the timeout. Reconciler must read fills since `last_check_ts`
  and update positions before the next rebalance touches them.

Both reuse the legacy execution stack's WebSocket private-order stream
(`scripts/private_order_stream.py`). This is the third-largest reason
to keep the legacy plumbing alive (alongside TP/SL reattach and atomic
state writes).

### Circuit-breaker interaction

Existing breaker logic is unchanged. Maker execution does not affect
DD math — only cost. If the breaker halts during a rebalance, all open
orders must be cancelled within 5s (cancel-all by clientOrderId
prefix). Add this as the first action of the halt path in
`do_rebalance_cycle`.

### Funding cost tracking

Independent of maker execution but a prereq for any leverage > 1x
(per the leverage analysis). Adds `state.funding_paid_total` and a
periodic poll of `/api/v1/account/funding-history` to credit/debit the
running total. Surfaces in the dashboard as a separate fee line. Out
of scope for this plan; track as a follow-up.

---

## Gate criteria

### Paper window (now, ≥2 weeks from today)

- `plan-e-maker50` Sharpe (rolling 14d) ≥ 0.5 OOS.
- Fee delta vs `plan-e-base` ≥ +$3/wk (~half of η-maker projection at
  $5k notional).
- No CB halt on either instance unless both halt within the same week.

### Live executor go-live (post-paper-PASS)

- Plan E paper as a whole passes P1 (Sharpe ≥ 0.5 OOS, no halt, ≥ 2 wk
  data).
- Maker50 paper sensitivity confirms the cost-side lift.
- Legacy execution stack reused for reconciliation/private-stream/
  atomic-write — no new code on those concerns.
- Initial deploy at 0.10 leg notional, no leverage. Maker fallback
  rules above. Run alongside taker live for ≥ 1 week to compare
  realized vs expected fill rates.

### Promote-to-default (post-live)

- Realized maker-fill rate ≥ 0.40 averaged over 4 weeks (η-maker gate
  pass at F=0.30 was the breakeven; we want margin).
- Realized round-trip cost ≤ 8 bps averaged over 4 weeks.
- No reconciliation incident (orphan orders, stuck partials,
  position-state divergence).

If gates are met, the maker executor becomes the live-default; taker is
kept as a manual flag for emergency.

---

## Engineering effort estimate

| Component | Estimate | Risk |
|---|---:|---|
| `plan-e-maker50` paper config (this commit) | ~done | low |
| Dashboard CB + cost-bps surfacing (this commit) | ~done | low |
| Live executor scaffolding + post-only place_order | ~4h | low |
| Timeout/retry loop + taker fallback | ~2h | medium (race conditions) |
| Per-leg fill accounting in trades.log | ~1h | low |
| Reconciliation: open-order enumeration + cancel | ~3h | medium |
| Reconciliation: partial-fill position update | ~3h | high (correctness) |
| Cancel-all on CB halt | ~1h | low |
| End-to-end test on Blofin testnet (if available) | ~4h | medium |

Total: ~18h focused work post-paper-PASS. Aligned with the original
"~half-day" estimate ONLY if the legacy stack ports cleanly; given
that the legacy stack assumed a single open position per symbol vs
Plan E's 6 simultaneous legs across symbols, expect closer to a full
2-day implementation.

---

## What this plan explicitly does NOT do

- Does not change paper behavior of any existing instance.
- Does not introduce financial leverage. Any leverage discussion is a
  separate plan, gated on this plan working in live first.
- Does not change the signal, lookback, k_exit, sizing, or any other
  edge-relevant parameter. Cost-only optimization.
- Does not touch the legacy "advanced" strategy (P4 freeze).

---

## Decision log

- 2026-04-26: paper variant `plan-e-maker50` deployed alongside the
  existing 8 instances. Live executor work parked until paper-PASS.
