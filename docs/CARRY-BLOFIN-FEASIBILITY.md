# BloFin carry feasibility check (Phase 0)

> Status: **RED — STOP.** As of 2026-05-13 (today), BloFin's public REST
> API does **not** support programmatic SPOT order placement. We can read
> spot market data and spot balances, but we cannot place / cancel /
> query spot trade orders via the API. The carry strategy requires a
> spot leg under program control — so BloFin's API in its current shape
> cannot host the carry. The pivot from OKX-EU → BloFin therefore does
> not work either, for a different reason than OKX-EU did. Recommend
> the main session escalate (see §6).
>
> Companion files: probe transcripts captured live against
> `https://openapi.blofin.com` from the dev workstation 2026-05-13.

---

## 0. TL;DR (the five Phase-0 questions)

| # | Question | Verdict | Detail |
|---|---|---|---|
| 1 | BloFin BTC-USDT **spot market** exists & has depth? | **YELLOW** | Yes, the market exists on the UI and the *public data* API exposes it (`/api/v1/spot/market/instruments?instType=SPOT` returns BTC-USDT with `state=live`, `minSize=0.00001`, `lotSize=0.00001`). **BUT** there is no documented public API to *trade* it (see Q1b). |
| 1b | Can a SPOT order be **placed via the API**? | **RED** | No documented spot place-order / cancel / order-detail endpoints. BloFin's official Python SDK (`blofin/blofin-sdk-python`, latest commit) ships zero spot-trading methods. A CCXT maintainer confirmed in 2026-03 that BloFin API is swap-only. The carry needs a programmatically placeable spot leg → this is a hard blocker. |
| 2 | Spot + perp on one account; margin model? | **GREEN-but-moot** | YES, BloFin's Unified Trading Account (UTA, GA April 2025) supports spot + perp + multi-currency margin on a single account, with spot able to act as cross-collateral for the perp short — better than the `docs/STRATEGY-CARRY.md §5` "siloed ~6.3 %/yr" assumption. Effectively comparable to OKX unified margin (~9 %/yr book yield). Irrelevant if we can't programmatically trade spot. |
| 3 | EU/NL regulatory exposure (MiCA, account caps)? | **YELLOW** | BloFin is an **offshore, unlicensed** exchange (no MiCA CASP license). NL's MiCA transition period **expired mid-2025**, so a Dutch resident using BloFin is operating outside the local regulatory perimeter from 2025-07. BloFin itself does *not* restrict NL — NL is not on its 44-country block list — and there are no API-level `acctLv` caps analogous to OKX-EU. The risk is **user-side**: enforcement against the *user*, not against the exchange-API surface. AFM has been actively investigating unlicensed CASPs since mid-2025. |
| 4 | Fees + funding cadence + min size? | **GREEN** | Spot maker/taker base rate 0.10 % / 0.10 % (VIP 0); futures maker/taker 0.02 % / 0.06 %. Round-trip 4-fill cost at base spot = ~0.20 % + futures ~0.08 % ≈ **0.28 % round-trip** vs the `STRATEGY-CARRY.md` ~0.16 % assumption (about 1.75× worse). Funding settles every 8 h. BTC-USDT spot min 0.00001 BTC (≈ \$0.80). BTC-USDT perp min 0.1 contract × 0.001 BTC/contract = 0.0001 BTC (≈ \$8) — comfortable. |
| 5 | Demo / testnet? | **GREEN** | YES, `https://demo-trading-openapi.blofin.com` mirrors the production API surface. BloFin demo supports spot, futures, webhook, and API trading with resettable virtual assets. (Same caveat — only as useful as the live API surface, which doesn't include spot place-order.) |

**Decision gate:** Q1b is RED → STOP, no Phase 1 work. See §6 for the
next-move options for the main session.

---

## 1. Spot market: market data is there, trading isn't

### 1.1 Public spot data — works

Live probe transcripts captured 2026-05-13, no credentials, browser UA:

```
GET https://openapi.blofin.com/api/v1/spot/market/instruments?instType=SPOT
  → 200 OK, code=0, 253 instruments, all instType=SPOT
  → first row:
     instId=BTC-USDT, baseCurrency=BTC, quoteCurrency=USDT,
     minSize=0.00001, lotSize=0.00001, tickSize=0.01, state=live

GET /api/v1/spot/market/tickers?instType=SPOT&instId=BTC-USDT
  → 200 OK, code=0, n=1
  → last=81008.67, askPrice=80996.27 askSize=4.88506,
     bidPrice=80992.44 bidSize=0.15875, vol24h ~3200 BTC/day

GET /api/v1/spot/market/books?instId=BTC-USDT&size=2&instType=SPOT
  → 200 OK, asks=[[80996.27,4.88506],...], bids=[[80992.44,0.15875],...]

GET /api/v1/spot/market/candles?instId=BTC-USDT&bar=1H&limit=2&instType=SPOT
  → 200 OK, 2 candles returned
```

The spot market is real, live, has BTC-USDT, and is queryable without
credentials at `/api/v1/spot/market/*` with `instType=SPOT` required on
every call. Depth is healthy (best bid/ask within 4 bps, ~5 BTC at top
of book, ~3200 BTC/24h volume — fine for a \$3 k-\$30 k carry leg).

> The existing `scripts/blofin_api.py` hits **`/api/v1/market/*`** (no
> `/spot/` prefix), which is documented as the *futures* market data
> path. Funding-rate + perp tickers work there. Spot data requires the
> new `/api/v1/spot/market/*` prefix.

### 1.2 Spot **trading** — not exposed on the public API

| What we'd need | What's documented | Status |
|---|---|---|
| `POST /api/v1/spot/trade/order` (place spot) | No docs entry. Probe returns `401 Unauthorized` — endpoint *might* exist behind auth, but is undocumented (signing semantics, body fields, response codes all unknown). | **RED** |
| `POST /api/v1/spot/trade/cancel-order` | Same: 401 on probe, no docs. | **RED** |
| `GET /api/v1/spot/trade/order-detail` | Same. | **RED** |
| `GET /api/v1/spot/trade/orders-pending` | Same. | **RED** |
| `GET /api/v1/spot/trade/fills-history` | Same. | **RED** |
| `GET /api/v1/asset/balances?accountType=spot` | **Documented** in the SDK (`rest_trading.py:22`), returns spot wallet balances. | **GREEN** |
| `POST /api/v1/asset/transfer` from `spot` → `futures` (and back) | **Documented** in the SDK. We can move USDT between the two wallets. | **GREEN** |

Authoritative evidence the spot place-order endpoint is not a public
API:

1. **Official BloFin Python SDK** (`github.com/blofin/blofin-sdk-python`,
   `src/blofin/rest_trading.py`) — inspected today (2026-05-13). The
   SDK exposes 30+ trading methods (`placeOrder`, `placeTpsl`,
   `placeAlgoOrder`, `placeBatchOrders`, `cancelOrder`, `closePosition`,
   `setMarginMode`, …). Every single one signs to `/api/v1/trade/*` or
   `/api/v1/account/*`. The `placeOrder` method **requires `marginMode`
   ∈ {cross, isolated}** and `positionSide` ∈ {net, long, short} — i.e.
   futures-only by signature. There is **no `placeSpotOrder` method**,
   no `/api/v1/spot/trade/*` reference, no example calling such a path.
2. **Official API docs index** (`docs.blofin.com/index.html`) — fetched
   today. Sections: Overview, Public Data, Account, **Trading**,
   Affiliate, Copy Trading, User, Tax. The Trading section's
   place-order endpoint is `POST /api/v1/trade/order` and the sample
   body uses `marginMode=isolated` — futures shape. No "Spot Trading"
   section.
3. **CCXT issue [#24675](https://github.com/ccxt/ccxt/issues/24675)**:
   user reports `load_markets()` returns only SWAP markets on BloFin;
   CCXT collaborator (`carlosmiei`) confirms **2024-12-28** "blofin
   only supports swap trading through the API", and re-confirms
   **2026-03-04** "I don't think they support it on the API". Issue
   closed without resolution.
4. **JKorf C# SDK** (`JKorf/BloFin.Net`) — described as "client library
   for the BloFin REST and Websocket **Futures** API" (emphasis in the
   repo title). No SpotApi class. Same shape conclusion.

**Conclusion:** spot programmatic trading on BloFin is either (a) not
yet released, (b) gated behind a partner / institutional API tier we
don't have access to, or (c) intentionally not on the public REST.
Either way, **we cannot build the carry's spot leg against this API
today**.

---

## 2. Margin model: UTA *would* work — but it's moot if (1) fails

Originally `docs/STRATEGY-CARRY.md §2.1` described BloFin as having
**siloed margin** (spot wallet separate from futures wallet, no
cross-collateral) → effective book yield ~6.3 %/yr instead of the
~9 %/yr available on OKX unified margin. **That description is out of
date** as of April 2025:

BloFin launched the **Unified Trading Account (UTA)** in April 2025 with
three modes:

1. **Simple/Spot Mode** — spot only, no leverage, no perp.
2. **Spot and Futures Mode** — both products, both visible, but
   margin/PnL still siloed per product type.
3. **Multi-Currency Margin Mode** — unlocked at ≥ \$10 k equity; spot
   BTC counts as collateral for the perp short, PnL offsets across
   products, cross-currency liquidation surface.

For our use case the relevant mode would be **Multi-Currency Margin**.
The \$10 k equity threshold is right at the bottom of the user's target
account band (\$5 k-\$50 k per `[[trading_account_context]]`), so the
strategy could only run that mode after the first scale-up step.

If — hypothetically — the API supported spot trading, BloFin under UTA
Multi-Currency could deliver the same ~9 %/yr book yield as OKX. The
`STRATEGY-CARRY.md §5` "BloFin = siloed = ~6.3 %/yr" assumption was
correct for the 2024 product but is now pessimistic. Update if/when we
revisit this.

This finding **does not change the verdict** — the spot-API gap (§1.2)
blocks everything else.

---

## 3. EU/NL regulatory: not blocked at the API layer, but user-side risk

| Question | Answer |
|---|---|
| Is BloFin MiCA-licensed in the EU? | **No.** It is an offshore exchange (BVI / Cayman base, "global offshore trading infrastructure") that has stated it is "seeking CASP authorization under MiCA before the July 1, 2026, final transition deadline." |
| Are NL users on BloFin's geo-block list? | **No.** BloFin restricts 44 jurisdictions (US, CA, CN, IN, NK, IR, KP, Marshall Is., UAE, Singapore, …). NL is not on the list. No EU member state is on the list except Serbia (which is not in the EU). |
| Does BloFin have a "BloFin EU" entity with MiCA caps? | **No.** Single global entity, single API surface. There is no MiCA-compliant retail tier with leverage caps analogous to OKX-EU's acctLv=1 / 3× leverage cap. The perp BTC-USDT max leverage on BloFin is **150×** with no jurisdictional cap visible via API. |
| Has NL's MiCA transition closed? | **Yes**, mid-2025. NL is one of the shortest-transition member states (6 months). After 2025-07, providing CASP services to NL retail without MiCA authorisation is in breach of EU law. The hard EU-wide deadline is 2026-07-01. |
| What enforcement action has AFM taken? | AFM has been "actively conducting supervisory reviews, spot checks, and investigations" since mid-2025. Enforcement so far has been against *providers* (cf. Binance NL exit 2023, OKX EU acctLv=1 cap), not against retail users directly. The user-level legal grey zone is "depositing/withdrawing to/from an unlicensed CASP." |

**Practical reading.** Unlike OKX-EU (where the API itself enforces the
MiCA cap — acctLv=1, no perp), BloFin's API does *not* enforce anything
EU-specific. From the API's perspective the user is just another global
user with full access. The regulatory risk is therefore:
(a) reputational / contractual: if AFM moves to enforce against users,
the user could be in scope;
(b) counterparty: if BloFin is forced to exit the EU market (Binance NL
precedent), in-flight funds could be frozen / forced-out at short
notice.

**This is a real risk** and worth flagging to the user before any P3
live, but it is **not the blocker** here. The Q1 spot-API gap is.

---

## 4. Fees, funding cadence, min sizes — concrete numbers

| Item | Value | Source |
|---|---|---|
| **Spot maker / taker** (VIP 0, no token discount) | 0.10 % / 0.10 % | BloFin fee schedule, Help Center 2026 |
| **Futures maker / taker** (VIP 0) | 0.02 % / 0.06 % | BloFin fee schedule |
| Spot VIP ceiling (high volume) | 0.01 % both sides | TradersUnion 2026 review |
| Native-token discount | 10-50 % off | BloFin fee page |
| **Round-trip 4-fill cost (best-case all-maker)** | spot 0.10 % × 2 + perp 0.02 % × 2 = **0.24 % round-trip** | Computed |
| **Round-trip 4-fill cost (blended, ~50 % maker on each leg)** | spot 0.075 % × 2 + perp 0.04 % × 2 = **0.23 % round-trip** | Computed |
| **vs. `STRATEGY-CARRY.md` BloFin assumption** | doc assumed 0.16 % blended; reality is ~0.23-0.24 % | **~50 % worse than docced** |
| **vs. doc'd OKX assumption** | OKX ~0.14 % blended | BloFin spot side is the cost driver — OKX spot taker 0.05 %, BloFin spot taker 0.10 % |
| Funding settlement cadence | every 8 h (00:00 / 08:00 / 16:00 UTC) | `/api/v1/market/funding-rate` response payload includes `fundingTime` |
| BTC-USDT spot min order size | 0.00001 BTC ≈ \$0.81 | `/api/v1/spot/market/instruments` payload |
| BTC-USDT spot lot size | 0.00001 BTC | same |
| BTC-USDT spot tick size | 0.01 USDT | same |
| BTC-USDT perp min size | 0.1 contract × 0.001 BTC/contract = 0.0001 BTC ≈ \$8.1 | `/api/v1/market/instruments?instType=SWAP&instId=BTC-USDT` |
| BTC-USDT perp max leverage | 150× | same payload (`maxLeverage:150`) |

**Carry yield impact of the higher fee:** at the spec's $5 k book, 60 %
deployed delta-neutral = \$3 k notional × 4 fills = \$12 k turnover.
At 0.23 % round-trip = \$27.6 of fees per full open+close cycle. At a
v2 "always on" cadence (~2 leg-bursts over 3.3 y per spec §3.1
backtest), that's ~\$17 of fees/yr on \$5 k = **−0.34 %/yr yield drag**.
Negligible compared to the doc's ~10.5 %/yr gross — *if* we could
actually execute. So fees are not a Phase 0 blocker either.

---

## 5. Demo / testnet — works, but only as well as the live API

- Demo endpoint: `https://demo-trading-openapi.blofin.com`
- Demo features: spot trading (UI), futures trading, API trading,
  webhook trading, all on a single account; resettable virtual assets
  (default \$50 k USDT).
- The demo API surface **mirrors** the live API surface — so the same
  spot place-order gap exists on demo. We can read spot tickers / spot
  balance in demo but cannot place spot orders programmatically there
  either.

If/when BloFin opens a spot trading API, demo would work as the P2-
equivalent paper environment and we wouldn't need the "dry-run on LXC
with real market data only" detour the prompt anticipated.

---

## 6. Decision gate + next-move options for the main session

**Phase 0 verdict: RED on Q1b (spot place-order API).** Do not proceed
to Phase 1 (adapter spot extension + carry runner exchange dispatch +
LXC deploy). No code was changed in this session.

The carry-on-BloFin pivot does not work for a structural reason: the
exchange's public REST API does not currently expose programmatic spot
order placement. Building a `place_spot_order` method against an
undocumented `/api/v1/spot/trade/order` URL would be (a) reverse-
engineering an unreleased endpoint with no guarantee of stability and
(b) outside the prompt's "match BloFin's actual endpoint naming and
request/response shape" requirement (there is no documented shape to
match).

### Options the user can evaluate

1. **Park the carry, fall back to the DECISIONS.md endpoint.** The
   2026-05-12 entry already records this as the legitimate fallback if
   M1 (spot adapter on either venue) turns out to be a slog. The carry
   was the project's first structurally-positive bet, but the venue
   side has now failed on both OKX-EU (regulatory) and BloFin
   (API-surface). Reverting to "hold BTC/ETH with a trailing-stop +
   vol-target circuit-breaker" is the recorded legitimate outcome.

2. **Pursue OKX KYC Lv3** as the prior `DECISIONS.md` 2026-05-13 entry
   outlines, before pivoting at all. Same entry estimates ~30 % chance
   it unlocks acctLv ≥ 2 (perp). If it does, OKX-EU carry is back on
   the table with the P1 work already done and zero further venue
   integration needed. Worth doing first because it's ~15 min of user
   time vs. ~1-2 dev days of BloFin adapter rebuild even if the
   BloFin spot API opens up.

3. **Contact BloFin support / partner API team.** It is possible
   BloFin exposes spot trading via a partner API tier (institutional /
   market-maker), a private beta, or a forthcoming GA release.
   Worth a written enquiry: "Does BloFin's REST API support placing
   spot orders programmatically? If yes, please point us to the
   documented endpoint; if not, is one on the roadmap?" Estimated
   1-3 day turnaround. If yes → revisit, otherwise (1) or (2).

4. **Move the carry to a third venue.** Bybit, Binance, KuCoin, OKX
   (global, non-EU) all have documented spot+perp APIs with unified-
   margin support. All three are also unlicensed-in-NL the same way
   BloFin is, so the EU/NL regulatory exposure (§3) is identical or
   worse. Estimated 3-5 dev days per new exchange adapter (spot + perp
   surfaces from scratch, vs. ~1 day for BloFin if its spot API
   existed). Defer unless (1)-(3) all fail.

5. **Two-venue split, spot somewhere documented + perp on BloFin.**
   Explicitly rejected in `STRATEGY-CARRY.md §2.3` ("doubles
   counterparty risk, withdrawal latency between legs, not worth it at
   this size"). Not recommended.

### What gets *kept* from Phase 0

- The findings in this doc — they make `STRATEGY-CARRY.md §2.1`'s
  BloFin section out of date (siloed-margin assumption is wrong after
  UTA GA April 2025; spot-API gap is the actual blocker).
- The probe transcripts above are reusable if BloFin opens a spot API
  in the future.

### What does NOT get done in this session

- No `scripts/blofin_api.py` changes (no spot-endpoint additions).
- No `scripts/blofin_adapter.py` changes.
- No `scripts/carry_runner.py` changes (no exchange-dispatch logic).
- No `configs/carry-btc-blofin.json`.
- No new tests under `tests/`.
- No `carry@btc-blofin.service` deployment to the LXC.
- No `docs/CARRY-OPS.md` update.

These are all gated on Q1b flipping GREEN.

---

## 7. Citations / probe log

Live probes captured 2026-05-13 from the dev workstation against
`https://openapi.blofin.com` (no credentials):

```
/api/v1/spot/market/instruments?instType=SPOT   → 200 OK, 253 SPOT instruments, BTC-USDT present
/api/v1/spot/market/tickers?instType=SPOT&instId=BTC-USDT → 200 OK, last=81008.67
/api/v1/spot/market/books?instId=BTC-USDT&size=2&instType=SPOT → 200 OK
/api/v1/spot/market/candles?instId=BTC-USDT&bar=1H&limit=2&instType=SPOT → 200 OK
/api/v1/spot/trade/order        → 401 Unauthorized (no docs entry, sig shape unknown)
/api/v1/spot/trade/cancel-order → 401 Unauthorized (no docs entry)
/api/v1/spot/trade/order-detail → 401 Unauthorized (no docs entry)
/api/v1/spot/account/balance    → 401 Unauthorized
/api/v1/asset/balances?accountType=spot → 401 Unauthorized (documented, needs auth)
/api/v1/market/funding-rate?instId=BTC-USDT → 200 OK, rate=0.00001474 (8h, +0.0015%)
```

Cloudflare-1010 risk: the probes ran fine with default `python-requests`
UA, default `urllib` UA, and an explicit `Mozilla/5.0` UA. We'd still
add the browser UA preventatively per the OKX 1010 lesson if the build
went ahead.

Web sources consulted:

- `docs.blofin.com/index.html` — official BloFin API docs (TOC scraped
  2026-05-13; no spot trading section).
- `github.com/blofin/blofin-sdk-python` — official Python SDK; no spot
  place-order method.
- `github.com/ccxt/ccxt/issues/24675` — "BloFin API - does not support
  SPOT market?" (closed). CCXT maintainer confirmed swap-only on
  2024-12-28 and re-confirmed 2026-03-04.
- `support.blofin.com` — Demo Trading, Trading Fee, Unified Trading
  Account terms / guide pages.
- `datawallet.com/crypto/blofin-restricted-countries` — restricted
  jurisdictions list (NL not on it).
- `sumsub.com` MiCA transition timeline (NL closed mid-2025).
- `ainvest.com` / `globenewswire.com` BloFin UTA April 2025 launch
  press releases.

---

## 8. Open items if Q1b flips GREEN later

The Phase 1 plan in the original task prompt is mostly still correct;
recording the deltas here so the main session has them:

- `STRATEGY-CARRY.md §5` "siloed yield ~6.3 %/yr" → out of date,
  rewrite for UTA Multi-Currency Margin once activated (≥ \$10 k
  equity → ~9 %/yr like OKX).
- Round-trip fee assumption (~0.16 % blended) → bump to ~0.23 % to
  reflect the higher BloFin spot maker/taker. Yield drag ~0.3 %/yr at
  realistic re-leg cadence, still small.
- The `/api/v1/spot/market/*` prefix (with mandatory `instType=SPOT`
  query param on every endpoint) is the right shape for the market-
  data side; the new adapter would add:
  `get_spot_instruments`, `get_spot_ticker`, `get_spot_orderbook`,
  `get_spot_candles`, plus the `/api/v1/asset/balances?accountType=spot`
  call for the spot balance.
- The browser-UA fix on `BlofinAPI.session.headers` is cheap and
  worth adding preventatively whenever the adapter gets touched.

But none of that ships until BloFin documents a spot place-order API.
