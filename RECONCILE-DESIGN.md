# Plan E — Reconciliation hardening (P7 fail-closed)

**Status:** Design + paper-mode implementation. Live-mode interface
landed but unused until paper-PASS.
**Created:** 2026-04-26
**Implements:** P7 from DESIGN-PRINCIPLES.md ("Fail closed on
reconciliation mismatches"). Currently only the legacy bot honored P7;
Plan E had no reconciler at all.
**Prereq for:** any cross-margin live mode (last of four leverage prereqs).

---

## Problem

The Plan E runner trusts `portfolio.json` as ground truth. Every cycle
reads it, mutates it, atomic-renames it back. There are at least three
classes of failure this misses:

1. **State corruption** — a bug in `to_json`/`from_json`, a manual edit
   that leaves cash/equity inconsistent, a CB-reset that forgets to
   clear all the `cb_tripped_*` fields. None of this aborts the runner;
   it just silently runs with wrong numbers.

2. **Local/exchange divergence (live mode, future)** — Blofin shows a
   position the runner doesn't know about, or vice versa. P7 says fail
   closed; without a reconciler we just open another position on top.

3. **Orphan orders (live mode, future)** — runner crashes between
   `place_order` and the ack. The order sits live on the exchange but
   the runner has no record of it. Next rebalance assumes a clean book.

This document specifies the reconciler that closes these holes. The
design works in paper mode now (catching class 1) and exposes the live
interface for the maker-execution PRD to plug into without rework.

---

## Two reconciliation surfaces

### Self-consistency (paper + live)

Pure local check. No network. Always runnable.

| Rule | Description |
|---|---|
| R1 | `cash + sum(unrealized_pnl(pos, entry_price)) ≈ equity` (using entry as proxy when no price feed; tolerance $1) |
| R2 | `peak_equity >= equity` (always; peak is monotonic) |
| R3 | `cb_state ∈ {normal, halved, halted}` |
| R4 | If `cb_state != "normal"`, all `cb_tripped_*` fields populated |
| R5 | Position count ≤ `long_n + short_n` |
| R6 | Each position has valid side, positive notional, positive entry_price |
| R7 | `last_funding_ts <= now` (no future-dated entries) |
| R8 | `funding_paid_total` finite (no NaN/inf from API parse errors) |

### Exchange-vs-local (live mode only)

Caller fetches `Dict[symbol → ExchangePosition]` from
`/api/v1/account/positions` (or from a fixture for tests) and passes it
to `reconcile_against_exchange`.

| Rule | Description |
|---|---|
| X1 | Every local position has a corresponding remote position with same side |
| X2 | Notional difference ≤ tolerance (default 5% relative) |
| X3 | No remote position is missing locally — i.e., no "foreign" leg unless `--allow-foreign-positions` is set |
| X4 | No local position is missing remotely (closed by exchange without our knowledge — e.g., liquidation) |

X4 is informational in paper. In live, this is a critical alert.

### Open-order orphan check (live mode only)

Caller fetches open orders from `/api/v1/trade/orders-pending`. The
reconciler identifies orders whose `clientOrderId` was minted by this
instance but no longer corresponds to a desired leg.

`clientOrderId` scheme: `pe-<instance>-<ts_compact>-<sym_compact>-<side>`

Example: `pe-base-20260427T0800-BTC-l`

The format is reversible (`parse_client_order_id`) so the reconciler can:
- Filter out orders not minted by us (other strategies/sub-accounts)
- Identify orders that didn't match against any current intent
- Cancel orphans before opening new orders

---

## Failure modes

When `reconcile_self` reports any error, the runner's behavior depends
on the CLI flag:

| Flag | Behavior on errors |
|---|---|
| (default) | Log warning, continue. Last result persisted. Dashboard shows red banner. |
| `--reconcile-strict` | Log + abort startup. Operator must investigate before re-enabling. |
| `--force-reconcile` (live only, future) | Overwrite local state with exchange state. Dangerous; matches legacy P7 behavior. |

---

## Persistence

- Per-cycle result appended to `state/<instance>/trades.log` as an event
  with `action: "reconcile"`.
- Latest result mirrored in `portfolio.json` so the dashboard can show
  it without scanning the log: `last_reconcile_ts`, `last_reconcile_ok`,
  `last_reconcile_errors_count`.
- Reconciler runs at:
  - **Runner startup** (always, log-only by default)
  - **Each rebalance cycle** (always, log-only)
  - On the runner's first `--reconcile-strict` startup (mandatory pass)

---

## Cross-margin specifics

For Blofin's cross-margin mode (the prerequisite this design unlocks):

- The exchange returns positions per-symbol still; cross-margin is an
  *account-level* property, not a position-level one. So the
  reconciliation API contract is unchanged.
- Funding accrual is per-position regardless of margin mode — already
  handled by `apply_funding_charges`.
- Liquidation can close positions silently. X4 catches this; for live
  this should also flip `cb_state="halted"` defensively.
- Margin usage is exposed via `/api/v1/account/balance`. Future
  enhancement: a margin-usage check rule (M1: implied margin ≤ X% of
  equity) — out of scope for this commit.

---

## Out of scope for this commit

- Live executor itself (per maker PRD, gated on paper-PASS)
- Margin-usage rule (M1) — added when leverage > 1x is enabled
- Reconciliation of trail-stop state vs exchange stop-loss orders —
  added with the live executor for `plan-e-trail`
- WebSocket-based incremental reconciliation — current design is
  pull-only at startup + cycle boundaries

---

## Test plan

Embedded in the validation script (mirroring the funding/CB tests):

- 8 self-consistency rules each with a deliberately-broken portfolio.json
- 4 exchange-mismatch scenarios (missing local, missing remote, wrong side, wrong notional)
- COID round-trip (mint → parse → round-trip identity)
- Backwards-compat: old portfolio.json without reconcile fields loads
- CLI flags: --reconcile-strict aborts on broken state, default mode warns
