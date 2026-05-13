# Carry strategy — phased build plan (P1 → P4)

> Status: **P1 complete (dry-run)** as of 2026-05-13. Strategy spec in
> `docs/STRATEGY-CARRY.md`. Decision authority is the 2026-05-13 entry in
> `DECISIONS.md` ("Build the carry now, on OKX EU; decouple from Plan E's
> paper-PASS gate").

This document is the **operational** plan for shipping the carry. The
strategy spec (`STRATEGY-CARRY.md`) is the *what*; this is the *how*, in
four phases, with go/no-go gates between each.

---

## 0. Adapter audit (input to P1)

### 0.1 What was already in `scripts/okx_adapter.py` / `okx_api.py`

| Feature | Status (pre-P1) |
|---|---|
| Auth (OK-ACCESS-KEY/SIGN/TIMESTAMP/PASSPHRASE headers, HMAC-SHA256 b64) | **Wired.** `_sign()` + `_request(auth=True)` are correct per OKX v5 docs. Verified by `tests/test_okx_adapter.py::TestSignatureAndTimestamp`. |
| Demo trading flag | **Wired** (`demo=True` adds `x-simulated-trading: 1`). |
| BASE_URL | Hard-coded `https://www.okx.com` (global). **No EU override before P1.** |
| Rate limiting | Sliding-window throttle, 8 req / 2s. Conservative but fine. |
| `get_ticker` / `get_candles` / `get_mark_price` (perp) | Wired. |
| `get_funding_rate` / `get_funding_rate_history` | Wired (uses `instId=BTC-USDT-SWAP`). |
| `get_balance` | Wired but only flattens the unified-account `details` list — fine for spot+perp under unified margin. |
| `get_positions` | Wired with `instType=SWAP` (perp only). |
| `place_order` / `cancel_order` / `get_order_detail` | **Perp-only.** Hard-coded `instType=SWAP` on history queries; the order endpoint itself is generic but no spot path. |
| `place_tpsl_order` / TP/SL algo | Perp-only (uses `posSide`, `tdMode`). |
| **Spot order placement** | **Missing.** |
| **Spot balance query** | **Missing** (general balance endpoint exists but no spot-only convenience). |
| **Spot instrument metadata (minSz, lotSz)** | **Missing.** |
| **Unified-margin account check (acctLv)** | **Missing.** `get_position_mode()` returns the same `/account/config` endpoint as we'd need, but the adapter doesn't surface `acctLv`. |
| **Joint margin snapshot** | **Missing.** Plan E's perp-only `get_balance` doesn't help us monitor liquidation distance on a unified book. |
| EU API specifics | Unverified. Plan E memo (`project_okx_live_executor_commit`) flagged this. |

### 0.2 Credentials / env vars

Plan E and the carry runner share env-var names:

| Env var | Purpose | Notes |
|---|---|---|
| `OKX_API_KEY` | OK-ACCESS-KEY header | required for private endpoints |
| `OKX_API_SECRET` | HMAC secret | required for private endpoints |
| `OKX_API_PASSPHRASE` | passphrase set on OKX UI when creating the key | required for private endpoints |
| `OKX_API_BASE` (added P1) | optional EU/regional host override | falls back to `https://www.okx.com` |

The OkxAdapter constructor also accepts `demo_mode: bool` and `base_url:
str` via its config dict, so config files can pin a specific host.

### 0.3 What P1 added

- **Spot order surface** on `OkxAPI` + `OkxAdapter`: `place_spot_order`,
  `cancel_spot_order`, `get_spot_order_detail`, `get_spot_active_orders`,
  `get_spot_fills`, `get_spot_ticker`, `get_spot_instruments`,
  `get_spot_min_size`. Spot uses `tdMode="cash"` (un-margined) by default,
  with `"cross"`/`"isolated"` available for unified-margin spot collateral.
- **Unified-margin awareness**: `get_account_config()` exposes the raw
  endpoint; `get_account_level()` parses `acctLv`; `assert_unified_margin()`
  returns a structured `{ok, acct_lv, message}` for the runner to log/abort
  on. We do **not** auto-flip `acctLv`; a human must change it in the OKX
  UI (Account → Account Mode) — this affects every product on the account
  and we don't want surprises.
- **Joint margin snapshot**: `get_margin_snapshot()` reads `totalEq`,
  `availEq`, `mgnRatio`, the spot BTC balance, and the signed perp
  position in one helper — what the runner needs to monitor liquidation
  distance.
- **EU base URL hook**: `OKX_API_BASE` env var + `base_url=` constructor
  arg. Default still `https://www.okx.com`. (See §0.4.)

### 0.4 OKX EU specifics — assumptions, flagged

These are *assumptions* baked into the P1 build; flag them for confirmation
before P2 goes live on a real EU account:

- **EU API base URL**: assumed identical to global `https://www.okx.com` —
  OKX's EU service uses the same v5 REST surface. Confirm against the
  OKX EU developer portal once an EU API key is provisioned.
- **Spot trading on EU retail accounts**: assumed available. Documentation
  indicates spot is fully available for EU retail. Confirm by attempting
  a minimum spot order on the paper/demo account in P2.
- **Perp leverage cap (MiCA / EU retail)**: assumed **2×** for retail
  perp/swap on OKX EU. This is the cap we already designed for (`leverage_cap:
  2.0` in `configs/carry.json`). Confirm via API: `/api/v5/account/leverage-info`
  or by reading `lever` field on `/api/v5/public/instruments`. If the cap
  is lower (e.g. 1×) the carry's effective book yield drops accordingly —
  not a blocker, just a sizing parameter.
- **Unified-margin (acctLv ≥ 3) availability on EU**: assumed available.
  Confirm via `/api/v5/account/config` on the EU paper account.
- **Funding-rate endpoint identity**: assumed identical between global and
  EU (same v5 path). Confirm during P2.

**If any of the above are wrong**, the carry can still run on BloFin's
siloed-margin layout (per `STRATEGY-CARRY.md` §2.2) at the cost of ~50%
of the book yield. That fallback is *not* built in P1 — would require a
separate BloFin spot adapter — but the runner is exchange-pluggable
(`cfg.exchange` field).

---

## P1 — extend OKX adapter for spot + dry-run carry runner [**COMPLETE**]

### Deliverables

| File | Purpose | Status |
|---|---|---|
| `scripts/okx_api.py` | Spot endpoints + `get_account_config`; EU base URL override | **DONE** |
| `scripts/okx_adapter.py` | Spot adapter surface, `assert_unified_margin`, `get_margin_snapshot` | **DONE** |
| `scripts/carry_position.py` | Pure-math `CarryPosition`, target sizing, drift, funding-accrual sign, green-button rule | **DONE** |
| `scripts/carry_runner.py` | Dry-run runner: market-data + decision-trace + reconciliation | **DONE** |
| `configs/carry.json` | Example config | **DONE** |
| `tests/test_carry_position.py` | 25 unit tests, pure math | **DONE** |
| `tests/test_carry_runner_dryrun.py` | 22 tests, mocked HTTP — runner + spot adapter | **DONE** |
| `docs/CARRY-BUILD-PLAN.md` | this document | **DONE** |

### What the runner does in P1

Per cycle (`--once` or `--loop`):
1. Fetch spot+perp price (public OKX endpoints, no auth needed).
2. Fetch current 8h funding rate + (first cycle) historical window.
3. Compute trailing-90d annualised funding from the cached window.
4. Apply green-button: if `trailing_ann > +5%/yr` then **target =
   notional_fraction × initial_notional_usd**, else **target = 0**.
5. Compute target sizing via `target_position_for()`.
6. Compute current drift (delta-neutral + basis) on the runner's
   `simulated_position`.
7. Project next-8h funding income at the current rate.
8. Check basis-blowout (kill-switch precursor): warn if
   `|spot−perp|/spot > basis_kill_pct`.
9. (If credentials present) fetch account snapshot:
   `assert_unified_margin` + `get_margin_snapshot`. Warn if `acctLv < 3`.
10. Run reconciliation (C1–C6 rules). Mismatches **log; do not crash**.
11. Append a structured JSONL trade-log entry to `state/<instance>/trades.log`.
12. Persist state atomically to `state/<instance>/state.json`.
13. Write `state/<instance>/health.json` for the dashboard.

The trade-log entry includes `action.kind ∈ {would_open, would_unwind,
would_resize, noop, skip}` — the runner says exactly what it *would do*
without actually doing it.

### P1 → P2 go/no-go (gate)

P1 is "the wiring is in place." P2 is "we trust it enough to send paper
orders." Required before P2:

- [ ] Spec confirmed: trailing-window samples (`270`), green-button
      threshold (`+5%/yr`), basis-kill (`1%`), leverage cap (`2×`).
- [ ] OKX EU credentials provisioned (demo or live paper-account).
- [ ] EU base URL confirmed (or default `https://www.okx.com` accepted).
- [ ] `acctLv = 3+` confirmed via the carry runner's `assert_unified_margin`
      output.
- [ ] Min spot order size on BTC-USDT confirmed (must be << our deployed
      notional — at $3000 / $60k ≈ 0.05 BTC vs OKX's typical min 0.00001 BTC,
      this is never binding but we still verify).
- [ ] Dry-run loop executed for ≥24h without crashes; trades.log shows
      consistent decisions (no NaN / no Inf / no missing fields).

---

## P2 — paper-trade on OKX demo / paper account

### Scope

- Promote the runner from "would do" to "would-do-on-paper". Place real
  orders on the OKX demo trading endpoint (`x-simulated-trading: 1`,
  `OKX_DEMO=true` in config).
- Track real fills: update `simulated_position` from `get_spot_fills` and
  `get_fills_history` on each cycle.
- Reconcile the runner's view of positions against the paper exchange
  positions on every cycle. Use Plan E's `reconcile_against_exchange`
  pattern (`scripts/plan_e_reconcile.py`).
- Add the legging protection: after spot fill, place perp leg; if perp
  leg fails to fill within 5s, immediately flatten the spot leg.
- Pull live fee schedule from the API and replace the assumed values
  in `CarryConfig`. (Spec §M1.)
- Run paper for ≥4 weeks (per DECISIONS.md process rule). Kill-criterion:
  realized net carry over the paper window < 0, OR basis monitor shows
  a mispricing the runner can't track.

### P2 → P3 go/no-go (gate)

- [ ] ≥4 weeks of paper operation with no crashes / unhandled exceptions.
- [ ] Reconcile-against-exchange ok in ≥95% of cycles. Any persistent
      `X1`/`X2`/`X4` errors investigated and resolved.
- [ ] Realised paper net carry within ±25 bps/yr of the backtest's
      prediction for the same funding window (sanity check that the
      executor isn't bleeding fees we didn't model).
- [ ] Basis-blowout kill-switch fired in dry-run on a synthetic test
      (or in paper on a real basis event), and behaved correctly.
- [ ] Margin-utilisation alarm wired and fired correctly on a test
      account drawdown.
- [ ] Green-button OFF → ON → OFF transition exercised in paper. Number
      of leg-bursts/yr matches the on/off-gate backtest (≤5/yr on the
      slow window).

---

## P3 — tiny live on OKX EU ($500–$1000)

### Scope

- Live trading on a real OKX EU account, **$500–$1000 book**, single
  delta-neutral pair, `leverage_cap ≤ 2×`.
- Monitor live fills, funding, margin, and basis for ≥2 weeks.
- All P2 risk-control wiring active and tested:

### Risk-control checklist (binding before P3)

- [ ] **Perp-short leg leverage cap**: book-vs-notional ≤ 2× (config-enforced,
      tested).
- [ ] **Margin buffer**: configured per `STRATEGY-CARRY.md` §5 layout —
      spot ≈ 0.9·B with unified margin, perp margin ≈ 0.1·B, buffer
      ≈ 0.0·B (on unified) or ≈ 0.2·B (siloed BloFin fallback).
- [ ] **Margin-utilisation alarm**: warn at `mgnRatio < 1.5`, alert at
      `mgnRatio < 1.1`, auto-flatten at `mgnRatio < 1.05`.
- [ ] **Auto-top-up rule**: move buffer USDT → perp margin if mgnRatio
      drops below alarm threshold.
- [ ] **Basis-blowout kill-switch**: flatten if `|basis_drift| / spot >
      basis_kill_pct` (default 1%) AND the drift persists for ≥3
      consecutive cycles (don't flatten on a single wick).
- [ ] **One venue only**: the runner refuses any config that tries to
      split spot/perp across venues.
- [ ] **Legging window < 5s**: after the first leg fills, the second
      leg must fill within 5s or leg-1 is immediately flattened.
- [ ] **Manual circuit-breaker**: a `state/<instance>/halt` sentinel
      file that the runner checks every cycle; if present, runner sits
      flat and exits cleanly.
- [ ] **Funding-on threshold sanity**: don't deploy if
      `trailing_annualised < threshold`. Re-check on every cycle.
- [ ] **Single-pair only**: BTC-USDT only in P3. ETH-USDT etc. deferred
      to P4.

### P3 → P4 go/no-go (gate)

- [ ] ≥2 weeks of live operation at tiny size without manual intervention.
- [ ] Net realized carry > 0 over the window OR explanation for why not
      (e.g. funding compressed; gate parked the trade in cash).
- [ ] No liquidation events. No basis-kill events that the kill-switch
      didn't catch.
- [ ] Reconciliation clean in ≥95% of cycles.
- [ ] Margin utilisation never went below alarm threshold.
- [ ] Books / records / fees match exchange statements to the dollar.

---

## P4 — scale to target book ($5k–$50k)

### Scope

- Scale book size to target (the user's $5k–$50k range per the project
  memory `trading_account_context`).
- (Optional, deferred) add the slow on/off gate as v2 per spec §3.1
  (3wk trailing window, −0.5bps hysteresis threshold). Backtest shows
  +0.3 pp/yr and lower DD; not a hard requirement.
- (Optional, deferred) add ETH-USDT as a second carry pair. Doubles
  monitoring surface; defer until BTC carry has been live ≥1 month.

### P4 → steady-state (gate)

- [ ] Position scaled in three steps (e.g. $5k → $10k → $25k → $50k) with
      ≥1 week of operation at each step.
- [ ] Calmar ratio over the live window > BH Calmar over the same window
      (the project's stated bar).
- [ ] Quarterly review: if 90d-annualised funding < 3% sustained, sit
      out per spec §6 "Funding compression" risk.

---

## Open decisions / questions for the user

These were resolved by sensible defaults in P1 but are flagged here in
case any conflict with intent:

1. **Green-button threshold = +5%/yr trailing-90d.** Per DECISIONS.md
   2026-05-13. Confirm — this is intentionally above the current YTD
   compression (-0.9%/yr) so the runner sits flat in 2026 even with
   credentials.
2. **`target_dn_notional_fraction = 0.6`** (default in `configs/carry.json`).
   On unified margin we could push this closer to 0.9 per spec §5; left
   conservative for P1 ahead of live confirmation that `acctLv = 3` is
   actually enabled on the EU account.
3. **Leverage cap = 2×**. Matches MiCA assumption. If EU retail allows
   3× we can lift it but the carry doesn't *need* it.
4. **Basis-kill = 1%**. Spec doesn't pin a number; 1% is a wide guard
   (BTC basis is usually <10 bps). Tune in P2 once we see live basis
   noise.
5. **Single perp instance only** (BTC-USDT). ETH or others deferred to
   P4. Confirm.

If any of the above defaults are wrong, edit `configs/carry.json` and
the runner picks them up on the next restart — no code change needed.

---

## Risk controls — wired vs pending

| Control | P1 (now) | P2 | P3 | P4 |
|---|---|---|---|---|
| Green-button rule | **wired** (read-only) | wired | wired | wired |
| Basis-blowout monitor | **wired** (logs, no flatten) | wired (flatten paper) | wired (flatten live) | wired |
| Margin-ratio alarm | **wired** (logs) | wired | wired + auto-top-up | wired |
| Unified-margin check | **wired** (assert + warn) | wired (fail closed) | wired (fail closed) | wired |
| Reconciliation (self) | **wired** | wired | wired | wired |
| Reconciliation (vs exchange) | n/a | **needed** | wired | wired |
| Legging window <5s | n/a | **needed** | wired | wired |
| Manual halt sentinel | n/a | **needed** | wired | wired |
| Liquidation distance monitor | partial (snapshot only) | wired | wired | wired |
| Fee schedule auto-pull | n/a (uses spec defaults) | **needed** | wired | wired |
