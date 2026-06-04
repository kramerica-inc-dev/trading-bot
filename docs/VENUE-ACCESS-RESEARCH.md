# Venue/access research — can an NL retail user execute the validated edges? (2026-06-04)

The project has validated three edges that are all execution-blocked for an NL/EU
retail account: cross-sectional **momentum** (perp long-short), **carry** (spot +
perp), and **VRP** (short-vol options). This research (deep-research harness:
6 search angles, 27 sources fetched, 121 claims, **25 adversarially verified
3-vote, 0 killed**) asks: is there a venue/access path that actually unlocks
them? **Answer: yes — the wall is not absolute.**

> This is a factual feasibility assessment, **not legal/tax advice**. Using
> EU-unregulated offshore/DEX venues as an NL resident is a **grey area**;
> confirm personal NL tax treatment + legality with a qualified adviser before
> deploying capital.

## Headline
- **Momentum + carry are executable TODAY.** Two routes: (1) **Hyperliquid**
  (DEX, no KYC, no NL geoblock, all 10 assets + shorts + funding API — cheapest,
  but grey-area + custody/smart-contract risk); (2) **Kraken Pro EU** or **OKX
  X-Perps** (MiFID-regulated, NL-eligible after a suitability assessment — the
  "clean" route, closer to the existing OKX adapter, but gated + leverage-capped).
- **VRP needs Deribit** (offshore Panama, NL not restricted, retail after KYC) —
  the only deep crypto-options venue reachable by NL retail. Heaviest + most
  uncertain (NL-retail options not 100% confirmed end-to-end).
- **Non-EU entity route (Path C): unsubstantiated + unnecessary** — Paths A/B
  unlock the strategies without it.

## Path A — DEX / on-chain (the cheapest unlock, no entity)

**Hyperliquid — UNLOCKS momentum + carry, actionable today.** [HIGH, 3-0]
- **Access:** NL/EU **not** on the restricted list (only US, Ontario, Iran, NK,
  Syria, Cuba, sanctioned). A critical watchdog (FinTelegram) independently
  confirmed EU residents fund/trade perps with **no KYC, no residency prompt, no
  geoblock** — access via a non-custodial wallet removes the residency/KYC gate
  that blocks NL on EU CEXes.
- **Universe:** 100+ perps, all 10 momentum assets (BTC/ETH/SOL/BNB/XRP/DOGE/
  ADA/AVAX/DOT/LINK) **long AND short**, 3–40x, on-chain order books.
- **API:** public `POST api.hyperliquid.xyz/info` → meta / clearinghouseState /
  **fundingHistory / predictedFundings** (exactly what momentum + carry need) +
  Python SDK + authenticated `/exchange` for orders. Hourly funding. Fees: taker
  0.024–0.045%, maker 0.000–0.015%.
- **Risks:** MiCA/MiFID **grey area** (FinTelegram flags de-facto unauthorized
  investment services to EU residents → future-block/enforcement risk);
  smart-contract + key-custody risk; build = a **new DEX SDK adapter** (wallet +
  gas), not the existing OKX adapter.
- Sources: hyperliquid.gitbook.io (docs: perpetuals / funding / fees),
  datawallet.com (restricted countries), fintelegram.com (EU access test).

**Aevo** unifies perps + options on one cross-margin account (tempting for all 3
at once) but its **options book is too thin** (~$517k/24h vs Deribit ~$79.5B/mo)
→ VRP economics don't survive. Perps candidate only; EU-geoblock status
unresolved. [HIGH, 3-0]

## Path B — MiCA/MiFID-compliant EU CEX (partially open, insufficient alone)

- **Kraken Pro EU** [HIGH, 3-0]: USD-margined perps via Payward Europe Digital
  Solutions (CY) Ltd, **CySEC/MiFID II (#342/17)**. Lists **all 10 momentum
  perps** within 150–300+ markets; NL not restricted (pro-only gate is UK-only);
  ~10x cap. **No retail options** (options API is demo-only / OTC-institutional).
  → unlocks momentum + carry, **not VRP**. Closest to the existing OKX REST
  adapter. Gated by a MiFID suitability questionnaire + opt-in.
- **OKX X-Perps** [HIGH, 3-0]: launched 15 Apr 2026 for EEA retail via **OKX
  Europe Markets Ltd, Malta MFSA (OEML-15905)** — a **different entity** than the
  MiCA-blocked OKX retail one that locked NL to spot-only. NOT a true perpetual:
  5-year-expiry futures with a funding mechanism (perps "cannot exist under
  MiFID II" → would be CFDs), 10x, suitability + cooling-off gated. Natural
  extension of the existing OKX adapter, **but** the perp universe must be
  verified vs the 10 assets, and NL-specific rollout not confirmed.
- **One Trading** (Amsterdam, AFM, MiFID OTF + MiCAR) [HIGH, 3-0]: genuine
  NL-retail crypto perps, but only **BTC/ETH/XRP EUR** (3 of 10) → insufficient
  coverage for cross-sectional momentum.
- **Regulatory baseline** [HIGH, 3-0]: ESMA (24 Feb 2026) holds crypto
  "perpetual futures" **likely fall under EU CFD product-intervention rules**
  (substance over form) → ~2:1 leverage limits, suitability, margin close-out for
  firms offering to EU retail. This is why CEX perps are gated/blocked — and why
  offshore/DEX (Hyperliquid, Deribit) remain reachable. Regulators are
  **constraining**, not opening — compliant EU perp offerings carry ongoing
  tightening/leverage-cap risk.

## Path C — non-EU entity
Unsubstantiated by surviving claims and unnecessary given A/B. No recommendation.

## VRP / options
**Deribit** [HIGH on mechanics 3-0 / 2-1 on the end-to-end NL-retail framing]:
restricted-jurisdictions list (Mar 2026) excludes NL/EU; EEA "broadly served";
retail serviced by **DRB Panama Inc.** (offshore, EU-unregulated) after mandatory
KYC (suitability + Jumio + proof of residence). The only deep liquid options
venue reachable by NL retail. **Caveat:** no 2026 source documents a *confirmed*
NL-retail options trade end-to-end, and Deribit applies retail spot-only
carve-outs elsewhere (UAE, Panama) — so strongly supported, not 100% confirmed.

## Ranked recommendation
1. **Momentum + carry, compliant route:** Kraken Pro EU or OKX X-Perps (MiFID,
   NL-eligible, gated). Lower legal risk; build close to the existing OKX adapter;
   verify the 10-asset universe + NL enablement first.
2. **Momentum + carry, grey-area route:** Hyperliquid (no gating, full API, all
   assets) — fastest unlock, but EU-unregulated grey area + custody/smart-contract
   risk; build = new DEX SDK adapter. The momentum runner is an adapter-swap away
   (strategy logic is exchange-agnostic).
3. **VRP:** Deribit only (offshore, KYC, grey area) — heaviest + most uncertain.

## Open questions (verify before committing capital)
- Can an NL retail user complete Deribit KYC **and** place an options trade
  end-to-end today, or does suitability route NL to spot-only (as for UAE/Panama)?
- Do Kraken Pro EU **and** OKX X-Perps actually list + provide liquidity for **all
  10** momentum assets for NL retail, and is X-Perps individually enabled in NL?
- Is Aevo (and Derive/ParadeX/Drift/Vertex/GMX) EU-geoblocked, and does any
  on-chain options venue have enough 30d liquidity for VRP?
- Net edge on Hyperliquid at $5k–$50k after gas/slippage/hourly-funding
  path-dependence — fees are competitive but a full Hyperliquid-data backtest was
  not part of this research.

## Hyperliquid validation — the edge SURVIVES on the executable venue (2026-06-04)

Before any wallet or capital, the gating question was: does the momentum lead
that cleared the null on OKX data also clear it on **Hyperliquid's own** prices +
funding? Built `backtest/hyperliquid_backfill.py` (public info API — all 10 assets,
1244 daily bars 2023-01→2026-05, plus **3 years of real hourly funding**) and
re-ran the lead (lb=120/rebal=5/m=3) on both panels (`scripts/validate_momentum_hyperliquid.py`):

| metric | **Hyperliquid** (executable) | OKX (original) |
|---|---|---|
| verdict | **ADVANCE** | ADVANCE |
| net return | **+252%** | +243% |
| null %ile | **99.9** | 100 |
| Sharpe | +1.03 | +1.02 |
| cross-sec IC | **0.069 (p=0.013)** | 0.069 (p=0.018) |
| real funding drag | **+6.0%/yr (1113 funded days)** | +4.0%/yr (94 days) |

**The edge is venue-independent** — near-identical metrics on two independent
price panels → it is a real cross-sectional momentum effect, not an OKX-data
artifact. Hyperliquid even gives **3 years of real funding** (vs OKX's 3-month
proxy): the realistic headwind is **~6%/yr (1.65 bps/day)**, comfortably inside
the breakeven (~4× margin; net only goes negative around ~6–7 bps/day flat drag).
The validation charged a conservative 15 bps/|Δweight|; Hyperliquid's actual fees
(taker 0.045%, maker 0.015%) are *lower*, so the real edge is if anything
understated. **Conclusion: Hyperliquid is technically + economically confirmed as
the executable venue for the momentum lane.** Remaining to go live: a Hyperliquid
order-execution adapter (wallet/EIP-712 signing via the `/exchange` endpoint) +
the user's legal/tax decision on the grey area. Data files are gitignored
(regenerate via `python -m backtest.hyperliquid_backfill`).

## Time-sensitivity
All facts are 2026-dated but the frontier moves fast (ESMA Feb-2026 CFD stance,
MiCA full implementation Jul 2026). Compliant EU perp offerings could be re-gated
/ leverage-capped; offshore/DEX access could be geoblocked or enforcement-targeted
at any time.
