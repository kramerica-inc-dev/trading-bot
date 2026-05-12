# Strategy: BTC funding / basis carry (cash-and-carry) — scoping & build spec

> Status: **GO (conditional)** — verdict 2026-05-12.  Net edge is real,
> structural, and the right size for a $5k–$50k book, but it is a *low-single-
> to-~8%-annualized, near-zero-drawdown* strategy: it crushes BTC buy-and-hold
> on **risk-adjusted** terms (Calmar ~6–10 vs BH ~0.5–0.8) and loses to it on
> **absolute return** in a bull market. The remaining work is mostly *exchange
> integration* (a spot order leg neither the BloFin nor the OKX adapter has
> today) plus a margin/basis monitor — not new strategy research.
>
> Companion code: `backtest/carry_backtest.py`, `tests/test_carry_backtest.py`,
> plot `backtest/results/carry_equity.png`. Funding series:
> `backtest/data/funding_btc_usdt.csv` (3648 8h settlements, 2023-01-12 →
> 2026-05-12, ~3.33 y).

---

## 1. The thesis (and how it differs from Fase 3)

Perpetual-futures funding on BTC-USDT is **persistently mildly positive** —
longs pay shorts. Per-8h percentiles (`docs/funding-analysis.md`): p5 −1.14 bps,
p25 −0.07, p50 +0.88, p75 +1.38, p95 +1.79 bps. Over the full 3.33 y CSV the
mean settlement rate is **+0.96 bps/8h ≈ +10.5%/yr**, and only **20% of all
settlements** are negative (mostly the rare deleveraging tail).

A **delta-neutral** position — long X USD of BTC **spot** + short X USD of the
BTC-USDT **perp** — has ~zero price exposure (the two legs cancel) and collects
that funding stream on the short leg. The P&L drivers are: funding accrual
(+), the spot↔perp **basis** drift between entry and exit (small, mean-reverting,
±), and the round-trip fees on the **four** fills (open: buy spot + sell perp;
close: sell spot + buy perp).

This is **not** the Fase-3 result. Fase 3 asked "does the funding *level* predict
the next 4h/24h BTC *return*?" → NO-GO (no robust *timing* edge). This strategy
ignores price direction entirely and harvests the funding *level* itself — a
structural cash-flow, not a forecast. Different question, different (and real)
answer.

---

## 2. Practical executability

### 2.1 BloFin

- **Spot + perp on one account?** BloFin lists BTC-USDT *spot* and a BTC-USDT
  *perpetual swap*; an account can hold both. **But the repo's adapters
  (`scripts/blofin_adapter.py`, `scripts/blofin_api.py`) are perp-only** —
  `place_order` posts swap orders with `marginMode`/`positionSide`; there is no
  spot-order path, no spot-balance read, no spot↔futures internal transfer call.
  The live system (Plan E) has only ever traded perps. **A spot order leg is net
  new exchange-integration work** — not large (one new endpoint family on a venue
  we already sign requests to), but real.
- **Margin model.** BloFin's futures account is cross-or-isolated *within the
  futures wallet*; spot BTC sits in the *spot wallet* and (unlike a true unified
  account) does **not** auto-collateralize the perp short. So the perp short
  needs its own USDT margin. Run it **low-leverage (≤2–3×) with a fat buffer**
  (see §6) so a BTC rally that moves the short against you doesn't liquidate.
- **Fees** (assumed — could not pull the live schedule from the API here; these
  are BloFin's documented standard-tier values, **state-as-assumed**):
  perp taker ~0.06% / maker ~0.02%; spot taker ~0.06% / maker ~0.02%. With
  post-only entries a 50–100% maker fill rate is realistic on a carry trade
  (you are not in a hurry). Round-trip (4 fills): **~0.08% maker-best,
  ~0.16% blended, ~0.24% taker-worst** of notional.
- **Funding mechanics.** Settles every 8h (00:00 / 08:00 / 16:00 UTC). The
  perp **short receives** `notional × rate` when `rate > 0`, **pays** it when
  `rate < 0`.
- **Min order size.** BTC-USDT perp min ~0.001 BTC (~$60–$100); spot similar.
  At $5k–$50k split across two legs this is never binding.

### 2.2 OKX (EU)

- The repo already has OKX adapter prep (`scripts/okx_adapter.py`,
  `scripts/okx_api.py`) — **still perp-only** (`tdMode` cross/isolated/cash,
  `instType=SWAP` everywhere; no spot order path either). BUT OKX's account is a
  **unified (multi-currency margin) account**: spot BTC *can* count as collateral
  for the perp short, which is the collateral-efficient setup we want (a rally
  that hurts the short simultaneously lifts the collateral → lower liquidation
  risk). OKX fees are also marginally lower at low tiers (taker ~0.05% / maker
  ~0.02% spot & perp — assumed values). OKX also offers "Simple Earn / savings"
  on BTC/USDT as an alternative low-yield sink for idle balance, not needed for
  the core trade.
- **If we pick one venue, OKX is the better carry venue** (unified margin) — but
  the OKX EU live-executor bundle is on hold per the existing paper-PASS gate
  (`MEMORY.md`: `project_okx_live_executor_commit`). Carry doesn't *require*
  unified margin (BloFin works with a fatter buffer), so we can prototype on
  BloFin and migrate.

### 2.3 Two-venue alternative

Spot on venue A / perp on venue B works but **doubles counterparty risk**, adds
a withdrawal/transfer leg between the wallets, and makes legging slower (you
can't atomically rebalance both legs). **Not worth it at this size** — single
venue, single account.

### 2.4 Conclusion

**Executable, but needs a modest new piece: a spot order/balance/transfer leg in
the adapter** (BloFin or OKX). The funding-data plumbing
(`fetch_funding_history`), the daily/8h harness patterns, and the perp side of
the adapter all already exist. No new *strategy* research needed.

---

## 3. Realistic net-carry estimate

From `backtest/data/funding_btc_usdt.csv` (3.33 y):

| metric | value |
|---|---|
| Gross carry (mean settlement rate × 3 × 365) | **+10.5%/yr** |
| — 2023 | +7.2%/yr (32% of settlements negative — the bear) |
| — 2024 | +17.4%/yr (only 7% negative — the bull) |
| — 2025 | +10.7%/yr (8% negative) |
| — 2026 YTD (Jan–May) | **−0.9%/yr** (57% of settlements negative — flat/chop, deleveraging) |
| Trailing-90d annualized funding: p5 / p50 / p95 | +0.3% / +10.3% / +26.7% |
| Trailing-90d annualized funding: fraction of days < 0 | **4.1%** |
| Round-trip fees (4 fills), held long & amortized over 3.33 y | ~0.024–0.072%/yr — **negligible** |
| Borrow/margin cost on the short leg | ~0 (perp short needs no coin-borrow; only its USDT margin sits idle) |
| Basis drag at unwind (mean-reverting; modelled ±4 bps OU) | ~0 expected over a long hold; ±a few bps one-off at exit |
| **Net annualized carry, maker-best** | **~10.5%/yr** (fees ≈ 0) |
| **Net annualized carry, taker-worst** | **~10.4%/yr** (fees ≈ 0 if held long) |

**Stability.** Gross carry is positive in 96% of trailing-90d windows over the
3.3 y; the one sustained negative stretch is **2026 YTD**, where funding has
been roughly flat-to-slightly-negative — i.e. the carry has *currently* dried up
on BTC. That is the headline risk: the edge is real and structural over a cycle
but **can compress to ~0 for months**, and we are in such a window right now.

### 3.1 Negative-funding handling — does the on/off rule pay?

Backtest both regimes (`backtest/carry_backtest.py`), $5000, full CSV span:

| variant | total | annualized | max DD | Sharpe | Calmar | legs (open/close bursts) | fees |
|---|---|---|---|---|---|---|---|
| **Always on** (eat the negatives) | +41.3% | **+10.95%** | 1.64% | 6.95 | 6.68 | 2 | $9.66 |
| On/off, fast window (21 settl ≈ 7d, thr 0) | +40.6% | +10.78% | 1.57% | 7.27 | 6.87 | 38 | $165.90 |
| On/off, **slow window** (63 settl ≈ 3wk, thr −0.5 bps hysteresis) | +42.6% | **+11.24%** | 1.12% | 7.38 | **10.06** | 10 | $42.80 |

A **trigger-happy** gate (7-day window) **does not pay** — it churns 38 leg-bursts
and the $166 of extra fees roughly cancels the negative-funding it dodges. A
**slow** gate (3-week trailing window, small negative threshold for hysteresis)
**does** help modestly: +0.3 pp/yr, lower DD, Calmar 10 vs 6.7, only ~3 leg-bursts/yr.
**Decision: ship the carry "always on" first; add the slow on/off gate as a v2
refinement once live mechanics are proven.** The gate's value is most about
*avoiding* the 2026-style flat stretches, not squeezing the median.

---

## 4. Backtest result (the headline numbers)

`backtest/carry_backtest.py`, $5000 deployed delta-neutral over the 3.33 y
funding CSV, funding accrued every 8h, basis modelled as a small seeded OU
series (σ ≈ 4 bps, mean-reverting — washes out over the span), 4-fill round-trip
fees charged once, equity compounded:

| | total return | annualized | max DD | Sharpe | Calmar |
|---|---|---|---|---|---|
| **Carry (blended ~50% maker fees)** | **+41.3%** | **+10.95%/yr** | **1.64%** | **6.95** | **6.68** |
| Carry (maker-best) | +41.4% | +10.97%/yr | 1.60% | 6.97 | 6.87 |
| Carry (taker-worst) | +41.2% | +10.92%/yr | 1.68% | 6.93 | 6.51 |
| **Hold $5000 cash** | 0% | 0% | 0% | — | — |
| **Hold $5000 in BTC (same span)** | **+310%** | **+52.8%/yr** | ~55–65% | ~1.3 | ~0.8 |

The carry's ~1.6% max drawdown comes **entirely from the basis wiggle + fees** —
*not* from BTC's price (which it is neutral to). On a risk-adjusted basis (Calmar
~6.7 vs BH's ~0.8) the carry **wins by ~8×**; on absolute return over a 4×-up
BTC market it loses by ~7×. Plot: `backtest/results/carry_equity.png` (equity
curves for all four carry variants + the trailing-~90d annualized funding panel
showing the 2026 dip toward zero).

> **Approximations (be honest):** (i) spot ≈ perp price for the price legs — we
> model the basis as the *residual* (a small OU series), not from a real fetched
> perp-vs-spot pair; in stress the perp can dislocate far further than ±4 bps
> (usually to a *discount* in a crash, which *helps* a short-perp leg). (ii) Fees
> are charged once for a buy-and-hold-the-carry run; if you roll/re-leg often,
> multiply. (iii) No financing cost on the idle USDT margin (could deploy it to
> "earn" for ~2–4%/yr extra — out of scope). (iv) No slippage beyond the maker/
> taker fee — at this size on BTC, fine. (v) Funding is BloFin's series; OKX's
> would differ slightly.

---

## 5. Position sizing

For a book of `B` (target $5k–$50k):

- **Deployed notional**: long `N` USD of BTC spot + short `N` USD of perp, with
  the perp short on **≤2–3× leverage** → perp margin posted ≈ `N / 3`.
- **Layout (BloFin, no spot-as-collateral)**: spot leg uses `N` USD of the book
  (held as BTC); perp margin uses ~`N/3`; keep the rest as a **margin buffer**
  in the futures wallet. With `N ≈ 0.6·B`: spot ≈ 0.6 B, perp margin ≈ 0.2 B,
  buffer ≈ 0.2 B. Effective book yield ≈ 0.6 × 10.5% ≈ **+6.3%/yr** on `B`
  (the idle 0.4 B drags the headline down — on OKX with spot-as-collateral you
  can run `N` closer to `B` and recover most of that).
- **On OKX (unified margin)**: spot BTC collateralizes the short → `N ≈ 0.9·B`
  with a healthy maintenance buffer; effective yield ≈ **+9%/yr** on `B`.

---

## 6. Risk assessment

| risk | magnitude | mitigant |
|---|---|---|
| **Liquidation of the perp short** on a sharp BTC rally | At isolated `L×` leverage the short liquidates roughly when BTC rises ~`(1/L − maint%)` ≈ **+45–50% at 2×, +30% at 3×** from entry; a +30% BTC week is rare but has happened. | (a) ≤2–3× + fat buffer; (b) on OKX use cross/unified so the spot BTC's gain offsets the short's loss → liquidation only on a *funding-flow* shortfall, effectively never at low leverage; (c) auto-top-up rule in the margin monitor (move buffer → perp margin if maintenance ratio < threshold). |
| **Basis blow-out** | Perp can trade far from spot in stress. In a *crash* the perp usually goes to a **discount** → the short-perp + long-spot pair **profits** (a tailwind, not a risk). The adverse case is a *euphoric squeeze* where the perp trades at a fat *premium* and you'd unwind into it — costs a one-off few-to-tens of bps. | Don't force an unwind in a stressed tape; the basis mean-reverts within hours–days. Basis monitor: alert if |perp−spot| > X bps; only unwind when it's back inside band. |
| **Funding compression / regime flip** | The whole edge can fall to ~0 for months (it has — 2026 YTD ≈ −0.9%/yr). Not a *loss* engine (DD stays tiny) but an *opportunity-cost* one. | The slow on/off gate (§3.1) parks the trade in cash when trailing-3wk funding goes negative; review the trade quarterly — if 90d-annualized funding < ~3% sustained, the trade isn't worth the operational overhead and you sit in cash/BTC. |
| **Exchange / counterparty** | Funds tied up on one venue (whole book). | One reputable venue only (BloFin or OKX EU); never split spot/perp across venues; cap the book per the project's per-venue limit. |
| **Legging / execution** | Between filling leg 1 and leg 2 you carry directional exposure. | Place both legs near-simultaneously (limit/post-only on the side with depth, market-or-aggressive on the other) — target a **<5 s** window; size the legs to round to the same BTC qty; if leg 2 fails, immediately flatten leg 1. Re-leg on a schedule (e.g. monthly) only if drift demands it, not reactively. |
| **Capacity** | None at $5k–$50k — BTC perp + spot books are deep. The funding premium itself can compress if everyone runs the basis trade, but that's a market-wide effect, not a size constraint for us. | — |

---

## 7. Build plan (milestones)

- **M0 — backtest (DONE).** `backtest/carry_backtest.py` + `tests/test_carry_backtest.py`
  + `backtest/results/carry_equity.png`. Net carry ~10.5%/yr gross, ~1.6% DD,
  Calmar ~6.7; on/off gate analysed. ← *this document.*
- **M1 — spot adapter leg.** Add spot order placement + spot balance read +
  (BloFin) spot↔futures internal transfer / (OKX) unified-account funding to
  `scripts/{blofin,okx}_adapter.py` + `_api.py`. Unit tests with mocked HTTP
  (mirror `tests/test_okx_adapter.py`). Pull the **live fee schedule** via the
  API and replace the assumed values in `CarryConfig`.
- **M2 — carry executor (paper).** A small runner: open the delta-neutral pair,
  monitor funding/basis/margin, accrue (paper) funding from the live endpoint,
  reconcile both legs each cycle. Reuse the Plan-E reconciliation patterns
  (`scripts/...reconciliation`). Paper-run **≥4 weeks** (per the DECISIONS.md
  process rule). Kill-criterion set in advance: if realized net carry over the
  paper window is < 0 *or* the basis monitor shows persistent mispricing the
  executor can't track, stop.
- **M3 — go/no-go + (if GO) live, tiny.** Validate against the relevant null —
  which here is **"hold cash (0%)" and "hold BTC"**, not a random-entry null
  (carry has no entry timing): the question is *net carry > 0 robustly OOS*.
  Go-live with the smallest meaningful book ($1–2k), ≤2× perp leverage, the
  margin + basis monitors armed, kill-switch wired. Add the slow on/off gate
  (§3.1) as a v2 once the always-on version is proven.
- **M4 — (optional) OKX migration / unified margin** to recover the idle-cash
  drag (§5), once the OKX EU live-executor gate is otherwise cleared.

---

## 8. Verdict

**GO, conditionally.** The carry has the property the project has been hunting
for and the three directional strategies lacked: a **structural, non-statistical
edge** that **survives out-of-sample by construction** (it's a cash-flow, not a
forecast), with a **tiny drawdown**. On the project's stated goal ("beat BH on
risk-adjusted terms") it does so emphatically — Calmar ~6.7 vs BH ~0.8.

The honest caveats, recorded so they aren't a surprise: (1) it does **not** beat
BH on absolute return in a bull market and never will — a +5–10%/yr near-zero-DD
sleeve is the *whole* offer; (2) the edge **can compress to ~0 for months** and
**is compressed right now** (2026 YTD funding ≈ flat), so go-live timing and the
on/off gate matter; (3) it needs a **new spot adapter leg** (M1) before it can
run — modest but real engineering, not zero. If M1 turns out to be a
disproportionate slog on both venues, the fallback (per DECISIONS.md) — "this
account holds BTC/ETH with a drawdown circuit-breaker + vol-targeting and stops
trying to beat it" — remains the legitimate endpoint.
