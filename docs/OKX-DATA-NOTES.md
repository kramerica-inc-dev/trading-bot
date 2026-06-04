# OKX data notes (M1 of the OKX strategy-sweep)

Findings from the OKX-sourced backfill (`backtest/okx_backfill.py`, run 2026-06-04).
These govern which OKX-specific economic claims the sweep is allowed to make.

## What we can fetch from OKX (public, no auth)

| Series | Endpoint | Result | Quality |
|---|---|---|---|
| Daily candles, 10 assets | `/api/v5/market/history-candles` (`bar=1Dutc`) | **1299 bars each, 2022-11-13 → 2026-06-03, UTC-aligned, fresh** | ✅ Better than the BloFin cache (longer + 3 weeks fresher). Primary source for all price/momentum candidates. |
| Funding-rate history (BTC perp) | `/api/v5/public/funding-rate-history` | **Only ~281 settlements (~3 months), 2026-03-02 → 2026-06-04** | ⚠️ OKX retains only ~3 months of public funding history. Too short for a robust multi-regime null gate. |

Output lives in `backtest/data/okx/` (gitignored, regenerable). Schemas are
byte-for-byte compatible with the existing loaders (`load_daily_btc`,
`load_funding_series`).

> **Use `1Dutc`, not `1D`.** OKX's `1D` bar is Hong-Kong-aligned (boundary at
> 16:00 UTC); `1Dutc` aligns to 00:00 UTC and matches the BloFin daily series.

## The critical finding: BloFin funding is NOT a valid OKX proxy

We reconciled OKX vs BloFin funding on their 214 overlapping settlements
(2026-03-02 → 2026-05-12):

```
corr(OKX, BloFin)      = 0.30          # weak — major-venue BTC funding usually ~0.7-0.9
mean|diff|             = 0.0000837
OKX  mean funding/8h   = +0.0000057  ≈ +0.62%/yr
BloFin mean funding/8h = -0.0000685  ≈ -7.50%/yr   # OPPOSITE SIGN
```

The two venues' funding **disagree in both magnitude and sign** over the same
window. (Part of the low correlation is the current compressed-funding regime —
when funding hovers near zero, venue micro-noise dominates — but an ~8%/yr mean
divergence is economically material and cannot be hand-waved.)

### Consequences for the sweep

1. **Carry / funding candidates (B1/B2/B3) must use OKX-native funding**, not
   the BloFin `funding_btc_usdt.csv`. The historical carry result
   (+10.95%/yr, Calmar 6.7 — backtested on **BloFin** funding) **does not
   transfer to OKX** and must be re-earned on OKX data.
2. **OKX-native funding is only ~3 months long** → a proper multi-regime null
   gate on OKX funding is not yet possible. Options:
   - (a) Treat carry/funding feasibility on OKX as **data-limited / inconclusive**
     this round; collect OKX funding **forward** (the runner already logs it each
     cycle) until enough history accrues for a null gate.
   - (b) Use BloFin funding only as a *shape* prior, explicitly flagged as
     non-transferable, never as the basis for a go decision.
3. **Revise the carry prior down for OKX specifically.** The plan's P(clears
   null)=0.6 for B1 was predicated on the proven BloFin carry; on OKX the edge
   is unverified and current funding is compressed to ~+0.6%/yr (barely above
   the fee floor). The carry remains the best structural *candidate*, but its
   OKX edge is now **unproven**, not proven.

### Open question to resolve later
The 0.30 correlation is low enough to also warrant a data-quality check on the
BloFin `funding_btc_usdt.csv` (sign/scaling/settlement-timestamp convention)
before trusting *any* cross-venue funding comparison. Deferred — does not block
the price-based candidates, which use the (solid) OKX daily series.
