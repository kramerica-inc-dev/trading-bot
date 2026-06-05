# Crypto Trader — Verbeterplan V2.1: Drie Parallelle Structural-Premium Lanes (+ Sham-control)

> **Opvolger van `IMPROVEMENT_PLAN.md`** (v1, 2026-05-12, COMPLETE met verdict NO-GO).
> V1 probeerde de bestaande prijs-directionele 'advanced' strategie te verbeteren
> via filter/sizing/scoring lagen — alle 6 fases bewezen geen edge. De 8-axis
> edge-diagnose (`docs/edge-diagnosis/A..I-*.md`) toonde aan dat de entry-edge
> structureel ontbreekt, niet door tuning te repareren.

> **V2 pivot** (2026-05-23): stop met prijs voorspellen. Oogst structurele premies
> op EU-toegankelijke rails. Drie parallelle sondes naar Feasibility Gate.

> **V2.1 revisie** (2026-05-23, na externe review): gates per lane substantieel
> verstrengd (bid/ask + executable cost + tail-hedge + depeg + pre-funded
> inventory). Fase 0.5 Data & Execution Reality Check toegevoegd vóór Fase 1.
> Sham-control discipline toegevoegd om gate-thresholds te valideren.
> Doorlooptijd realistisch herzien.

**Project**: `blofin-trader` v2.7+
**Locatie**: `/Users/michiel/Downloads/openclaw/blofin-trader/`
**Datum**: 2026-05-23

---

## Waarom drie lanes parallel (en hoever)

DECISIONS.md (2026-05-12) bevatte de regel "one strategy at a time". Dit plan
zet die regel **selectief** opzij: parallel werk is alleen toegestaan in de
read-only feasibility-fase (geen runtime, geen orders, alleen data + null-tests).
Vanaf de Build-fase blijft "one at a time" gelden, en at-most-1 lane mag tegelijk
live.

**Rationale**: parallelle feasibility multipliceert mijn aandacht maar niet het
multiple-testing-risico op productie — drie kandidaten brengen we tegelijk tot
de gate, daarna meritocratisch één naar paper. De kosten zijn voornamelijk
documentatie + data-fetch tijd, niet runtime-bandbreedte op de LXC.

**Rationale at-most-1-live**: in crypto correleren "onafhankelijke" premies
typisch op exact de verkeerde momenten (exchange-stress, stablecoin-stress,
liquidity-stress, collateral-stress — LUNA mei-2022 en FTX nov-2022 zijn
voorbeelden). Bij beperkt kapitaal en zonder formeel risk-budget framework is
concentratie veiliger dan naïeve diversificatie van slecht-begrepen
tail-correlated premies. Bij groei naar $50k of bewezen track-record kan dit
herzien worden — wordt expliciet vastgelegd in DECISIONS.

**Patroon uit de 4 afgesloten lanes**:

| Lane | Faal-modus | Onderliggende oorzaak |
|------|-----------|----------------------|
| 1. Advanced (multi-indicator confluence) | 8-axis bewijs van geen entry-edge | Prijs-features hebben ~0 IC vs forward returns |
| 2. Plan-D (BTC 5m mean-reversion) | Fee-share >60%, base rate 27% verkeerde kant op | 5m churn op $115 onmogelijk; reversion-hypothese fout |
| 3. v1 trend-overlay (daily) | 3/3 rules onder random-entry-null OOS | Daily trend op 3.3y BTC niet onderscheidbaar van random-long |
| 4. Cash-and-carry | Sharpe 6.95, Calmar 6.68 in backtest maar 0 venues toegankelijk | EU MiCA / venue-API regulatory wall, *niet* strategie-falen |

De drie prijs-directionele lanes faalden op edge-niveau. De enige structurele
lane (4) had wél een echte edge — alleen door uitvoeringsbeperkingen onbruikbaar.
**Conclusie**: pivot naar structural-premium harvesting op EU-toegankelijke rails.

---

## Doorlopende principes (gelden voor élke lane)

- Elke nieuwe feature defaultt op `enabled: false` / paper-only
- Random-entry-null gate (of betere, lane-specifieke null) vóór backtest-optimalisatie
- Edge-diagnose template (`docs/edge-diagnosis/`) toepassen indien lane naar paper gaat
- Holdout reserveren, eenmaal evalueren, **deflated metrics rapporteren**
- Logging — elke beslissing in trade record voor post-hoc analyse
- At-most-1 lane tegelijk LIVE
- **NIEUW v2.1**: geen Fase 2 Build zonder executable-cost model (bid/ask,
  fees, slippage, latency, hedge-costs, margin, inventory)
- **NIEUW v2.1**: Multiple-testing penalty is cumulatief over alle lane-historie,
  niet alleen over de 3 V2-lanes. Deflated Sharpe rekent met N = totaal aantal
  geteste varianten in dit project sinds 2026-04-18 (advanced, Plan-D, v1, carry,
  + V2-lanes)
- **NIEUW v2.1**: Sham-control discipline (zie aparte sectie hieronder)

---

## Sham-control discipline (NIEUW v2.1)

Elke gate wordt blootgesteld aan **twee soorten sham-tests** vóór een lane
PASS mag krijgen. Doel: bewijzen dat de gate discriminerend vermogen heeft —
dat hij niet per ongeluk ook nep-edges door laat.

### 1. Per-lane shuffle-test (in elke lane's analyse-script)

Voor elke lane wordt dezelfde data + dezelfde gate uitgevoerd op een versie
waarin de temporele structuur is vernietigd:

- **Lane A**: shuffle dagelijkse IV-waarden over het historische venster,
  herbereken VRP, run zelfde gate. Echte VRP-edge vereist temporele alignment
  IV(t) → RV(t..t+h); shuffled IV destroys this.
- **Lane B**: vervang overlay-signaal door random rebalance-schedule met
  zelfde gemiddelde frequentie; OF bootstrap yield-vs-price returns door
  onafhankelijk te resamplen. De "overlay voegt waarde toe boven yield-alone"
  claim moet alleen overleven als prijs- en overlay-dynamics genuinely
  correleren.
- **Lane C**: synthetische spread = N(real_mean, real_std), zonder
  cross-venue causale structuur. Check of gate zou passen op puur-ruis
  spreads met dezelfde marginale verdeling.

**Discipline**: shuffle-test moet FAIL halen op elke lane. Als shuffled-versie
ook door de gate komt → gate meet niet wat hij denkt te meten, terug naar
tekentafel.

### 2. Globale synthetische sham-lane "Sham-D" (parallel met A/B/C)

Een vierde "lane" die per constructie geen edge heeft, wordt door dezelfde
feasibility-pipeline geleid als A/B/C:

- **Strategie**: rotate BTC-exposure op basis van UTC-uur (bv. long 00-12,
  flat 12-24). Geen plausibele economische reden voor edge.
- **Data**: bestaande BTC daily/hourly bars (geen extra fetch nodig).
- **Pipeline**: zelfde `daily_backtester`, zelfde Calmar/alpha-vs-BH metrics,
  zelfde `random_entry_null.py`, zelfde gate-threshold-formule die A/B/C
  gebruiken.
- **Effort**: 0.5 dag, geen runtime, geen LXC-impact.

**Discipline**: Sham-D MAG NIET door de gate komen. Als hij dat wel doet:
gate is broken, alle drie de V2-gates moeten worden herijkt vóór één lane
echt PASS mag halen.

---

## Fase 0 — DECISIONS update + per-lane charter (gedeeld, vóór code)

Vóór één regel code: één DECISIONS-entry die expliciet:

- (a) erkent dat dit afwijkt van "one strategy at a time"
- (b) parallel toestaat t/m Feasibility Gate alleen
- (c) kill-criteria per lane up-front
- (d) bevestigt dat at-most-1 lane tegelijk naar live, met **rationale**
  (tail-correlation in crypto bij stress-events)
- (e) introduceert sham-control discipline als bindend onderdeel van gate

Per lane één `docs/STRATEGY-V2-<LANE>.md` met:

- Hypothese in één zin (testbaar)
- Data-bron + dekking + **bid/ask of executable proxy**
- Lane-specifieke null-benchmark (NIET overal random-entry — zie Lane B)
- Edge-grootte threshold om door te gaan (vooraf vastgelegd, niet post-hoc)
- Risico-modus + tail-scenario (depeg / liquidation / venue failure)
- Kill-criterium
- Sham-test plan

**Effort**: 0.5 dag totaal voor alle drie + Sham-D charter.
**Output**: 1 commit, 5 files (1 DECISIONS append + 3 lane-specs + Sham-D spec).

---

## Fase 0.5 — Data & Execution Reality Check (NIEUW v2.1)

Voor een lane statistisch getest mag worden, moet eerst worden vastgesteld dat
de benodigde data en execution-assumpties realistisch genoeg zijn om een
tradeable edge-test te doen. Een lane die alleen op mid/last/mark-data positief
lijkt mag niet door naar Fase 1.

**Effort**: 0.5–1 dag per lane (parallel uitvoerbaar via subagents).

### Lane A — VRP gate

PASS alleen als alle volgende onderbouwd zijn:

- Historische option surface 2019+ voldoende reconstrueerbaar (niet alleen
  index-vol of mark-IV, maar bid/ask of realistische proxy per strike/expiry)
- Greeks (Δ, Γ, Vega) per instrument/dag beschikbaar of zelf afleidbaar
- Hedge-instrument kosten simuleerbaar (perp funding voor de delta-hedge)
- Margin requirement model bestaat (cross/portfolio/SPAN voor Deribit)
- Tail-protection (OTM long puts) historisch prijsbaar
- Stress-events 2020-03, 2021-05, 2021-12, 2022-LUNA, 2022-FTX, 2024-ETF,
  2024-carry-unwind zonder lookahead reconstrueerbaar
- **Regulatory check**: kan een EU-resident in 2026 nog steeds Deribit gebruiken
  (KYC + API)? Dit is precies waar carry struikelde — check eerst.

### Lane B — Staking-overlay gate

PASS alleen als:

- Lido stETH APR historisch beschikbaar zonder lookahead (3+ jaar)
- SOL staking yield betrouwbaar beschikbaar (~2 jaar bruikbaar)
- Yield in token-units correct naar portfolio-PnL te vertalen (rebase vs
  reward-token)
- stETH/ETH depeg historie en withdrawal-queue events modelleerbaar
- SOL unstaking/bonding delay (~2 dagen) modelleerbaar
- Custody/platform-risk per yield-source (Lido vs Kraken vs Coinbase) als
  scenario meeneembaar
- **Regulatory check**: kan een EU-resident in 2026 ETH staken via Kraken
  EU / Coinbase EU? Lido on-chain stETH is altijd beschikbaar, maar
  custody/staking-services zijn EU-gereguleerd onveranderlijk geweest.

### Lane C — Basis gate

PASS alleen als:

- Executable bid/ask + orderbook depth (tot minimaal €1k, €5k, €10k) per venue
  haalbaar via WebSocket — niet alleen REST ticker
- Timestamps tussen venues NTP-synced of expliciet corrigeerbaar
- Actuele fee-tier per account/venue documenteerbaar
- Pre-funded inventory model simuleerbaar (spot-balances op alle 3 venues
  vooraf, geen per-trade transfers)
- Latency-meting per venue mogelijk (200–500ms realistisch)
- Partial-fill en one-leg-fill scenario's te modelleren
- Rebalance-cost (intermittente transfers) te schatten
- **Regulatory check**: alle 3 venues (Bitvavo / Kraken EU / Coinbase EU)
  accepteren NL-residenten met spot trading API + voldoende withdraw-limieten.

**Als een lane Fase 0.5 niet haalt: NO-GO vóór Fase 1, documenteren en sluiten.**
Dit is de gate die in V1 voor carry te laat kwam.

---

## Fase 1 — Feasibility (parallel, read-only, geen runtime)

Voor elke lane: data ophalen + statistical edge-test + sham-tests + go/no-go
rapport. Geen adapter, geen orders, geen runner-werk. Uitvoeren via parallel
subagents — onafhankelijk werk, geen shared state.

### Lane A — Volatility Risk Premium (Deribit options)

**Hypothese**: BTC/ETH implied volatility ligt structureel boven realized
volatility, met voldoende materialiteit (≥5%/yr net na bid/ask + hedge + tail-
protection) om als premie te oogsten via dagelijks delta-gehedgede short-vol
posities.

| Stap | Output |
|------|--------|
| A1 Data | `backtest/data/deribit_options_btc.parquet`, `_eth.parquet`. Per dag: bid/ask/mark IV, Greeks, per strike/expiry. **Volledig venster 2019+** (niet alleen post-LUNA). |
| A2 Realized vol | Rolling 7/30/90d realized vol uit bestaande spot-bars. |
| A3 VRP-tijdreeks | `backtest/analyze_vrp.py` — bid_IV vs realized_vol(t, t+horizon) per horizon (1w/2w/1m). Bid_IV is wat je executable krijgt als verkoper, niet mid/mark. |
| A4 Regime-cuts | VRP in bull/bear/sideways segmenten. Spike-events: 2020-03 COVID, 2021-05, 2021-12, 2022-05 LUNA, 2022-11 FTX, 2024-01 ETF, 2024-08 carry-unwind. |
| A5 Verkoop-simulatie | Short 25Δ strangle + dagelijkse delta-hedge via perp + mandatory 5%-OTM long put als tail-hedge. ALL costs (bid/ask, hedge perp funding, tail premie, margin opportunity cost) in baseline. |
| A6 Shuffle-sham | Run A3+A5 met willekeurig geshuffelde IV-tijdreeks. MOET FAIL. |

**Effort**: **4 dagen** (was 2d in V2.0 — bid/ask data + Greeks + hedge-cost
model + tail-hedge pricing is substantieel meer werk dan alleen index-IV).

**Gate (V2.1, verstrengd)**: PASS alleen als de strategie na alle kosten:
- annualized return on margin ≥ +5%
- net PnL na bid/ask, hedge-costs, tail-premie, margin opportunity cost
  positief in ≥2 OOS-splits
- p25 rolling 3m PnL > 0
- `margin_breach_count == 0` over heel venster
- 99% CVaR binnen vooraf gedefinieerde limiet (specifiek in lane-charter)
- Stress-events veroorzaken geen liquidation of forced unwind
- Shuffle-sham komt NIET door dezelfde gate

Als IV gemiddeld boven RV ligt maar bovenstaande niet wordt gehaald: **NO-GO**.

### Lane B — Staking-yield + bh_overlay (ETH/SOL spot)

**Hypothese**: de bestaande `bh_overlay` (drawdown-stop + vol-target) voegt
risk-adjusted waarde toe boven `BH+staking-yield-alone` op ETH/SOL — d.w.z.
de overlay-component betaalt zichzelf terug bovenop de yield-floor.

| Stap | Output |
|------|--------|
| B1 Yield-data | Lido stETH APR (3+ jaar, vrij beschikbaar). SOL staking yield (Solana Beach API, ~2y). Validatie: geen lookahead (APR-as-of-date, niet smoothed). |
| B2 ETH/SOL daily bars | Reeds beschikbaar via `data_collector.py`. |
| B3 Overlay-backtest | Componeer `bh_overlay_strategy.py` met yield-accrual. Geen nieuwe strategie-logica. |
| B4 Vergelijkingsbatterij | ETH-BH, stETH-BH, ETH-bh_overlay, stETH-bh_overlay, ETH-bh_overlay+synth_yield, SOL-BH, SOL-bh_overlay, SOL-bh_overlay+yield, BTC-bh_overlay baseline, EUR-cash. |
| B5 Attribution | PnL decomposeren in: spot beta / overlay-timing / yield-accrual / interaction. ≥90% van PnL moet verklaarbaar zijn. |
| B6 Depeg/haircut scenarios | Apply 5%/10%/15% depeg op stETH op LUNA-, FTX-achtige dates; replay overlay+yield met haircut. |
| B7 Shuffle-sham | Vervang overlay-signaal door random rebalance met zelfde gemiddelde frequentie. MOET FAIL. |

**Effort**: **2 dagen** (was 1.5d — vergelijkingsbatterij + attribution + depeg
scenarios kost meer dan eerdere schatting).

**Gate (V2.1, verstrengd — nieuwe null!)**: PASS alleen als:
- `bh_overlay+stake` Calmar verbetert tov `BH+stake` (NIET tov random-entry-null;
  random-entry was het verkeerde nul-model in V2.0)
- Max drawdown niet verslechtert boven vooraf vastgelegde tolerantie
  (bv. ≤5pp slechter dan `BH+stake`)
- Ulcer index verbetert
- Block-bootstrap CI op de verbetering sluit 0 uit met 95% confidence
- Depeg/liquidity haircut scenario (15% stETH-cliff) blijft acceptabel
- SOL unstaking delay (2d) is gemodelleerd
- Yield-data zonder lookahead
- Attribution verklaart ≥90% van PnL
- Shuffle-sham komt NIET door dezelfde gate

### Lane C — Cross-exchange spot basis (Bitvavo / Kraken / Coinbase EU)

**Hypothese**: prijsverschillen tussen drie EU spot-venues op BTC/ETH/USDT,
met **pre-funded inventory op alle drie**, leveren een netto-spread (na fees,
slippage, latency, partial-fills) van ≥0.10% per trade op ≥10% van observaties
bij capaciteit ≥€1k.

**N.B.** "Risk-free spread" framing uit V2.0 is geschrapt. Dit is **pre-funded
cross-venue inventory spread capture**, niet transfer-based arbitrage.
Transfers zijn alleen voor periodieke rebalance, NIET voor de trade zelf.

| Stap | Output |
|------|--------|
| C1 WebSocket data | Top-of-book + depth tot €10k op 3 venues, BTC-EUR + ETH-EUR + BTC-USDT (waar beschikbaar), **14–30 dagen** continu (was 3d in V2.0). NTP-synced timestamps. |
| C2 Fee-tier check | Maker/taker per venue + account-tier, withdraw fees + tijden, FX-spread EUR↔USDT realistisch. |
| C3 Spread-analyse | Net executable spread na fees + 200–500ms slippage + partial-fill scenarios. Histogram per capaciteit (€1k / €5k / €10k). |
| C4 Inventory-model | Simuleer pre-funded balances (bv. €5k spot + €5k stable op elke venue). Track inventory-drift, rebalance frequency, rebalance-cost. |
| C5 Execution scenarios | Apart simuleren: maker/maker, maker/taker, taker/taker, one-leg-fill, cancel/requote. |
| C6 Shuffle-sham | Synthetische spread = N(real_mean, real_std), geen cross-venue causaliteit. MOET FAIL. |

**Effort**: **2 dagen** mijn werk + **14–30 dagen wall-clock** voor de
WebSocket-data-verzameling parallel met andere lanes. (Was 1d totaal in V2.0
— compleet onrealistisch.)

**Gate (V2.1, verstrengd + geherframed)**: PASS alleen als:
- Spread gebaseerd op executable bid/ask, niet last/mid/ticker
- Orderbook depth ondersteunt capaciteit ≥€1k per trade
- Net spread ≥0.10% na fees+slippage op ≥10% van observaties
- Opportunities blijven bestaan onder 200–500ms latency
- One-leg/partial-fill stress acceptabel
- Inventory imbalance binnen vooraf vastgelegde limieten blijft over 30d
- Rebalance-cost (transfers) ≤30% van bruto basis-edge
- Shuffle-sham komt NIET door dezelfde gate

Als edge alleen zichtbaar is op ticker/last/mid of transfer-discount: **NO-GO**.

### Sham-D — synthetische control (parallel)

**Strategie**: rotate BTC-exposure op basis van UTC-uur. Long 00-12 UTC, flat
12-24 UTC. Geen plausibele economische reden voor edge.

| Stap | Output |
|------|--------|
| SD1 | Hergebruik `daily_backtester` (eigenlijk hourly hier) + BTC bars |
| SD2 | Run met dezelfde Calmar/alpha-vs-BH metrics als A/B/C |
| SD3 | Run `random_entry_null.py` op deze strategie |
| SD4 | Apply identieke gate-formule die A/B/C zou gebruiken |

**Effort**: 0.5 dag.

**Discipline**: Sham-D MAG NIET PASS halen. Als hij dat wel doet: gates van
A/B/C zijn te lax, alle drie herijken vóór één lane echt PASS mag krijgen.

### Feasibility Gate (synchroon na alle vier)

Na elke lane Phase 1 afgerond → samen kijken naar 4 rapporten (A, B, C, Sham-D).

Beslis-tabel:

| Sham-D status | A/B/C PASS aantal | Beslissing |
|---------------|-------------------|-----------|
| Sham-D PASS | (irrelevant) | Gates herijken vóór één lane door mag |
| Sham-D FAIL | 0 lanes PASS | Zoek lane #6 of accepteer fallback `bh_overlay@btc` paper voortzetten |
| Sham-D FAIL | 1 lane PASS | Ga naar Fase 2 voor die lane |
| Sham-D FAIL | ≥2 lanes PASS | Kies één voor build/paper op basis van (a) edge-grootte (b) executie-kosten (c) infra-hergebruik. Default = B. Andere lane(s) gespect maar wachten. |

**Waarom serieel vanaf hier**: drie nieuwe adapters tegelijk = 3× zoveel
oppervlak voor bugs, drie tegelijk paper-evalueren multipliceert het
multiple-testing-probleem op holdout-selectie.

---

## Fase 2 — Build (alleen voor lane die Feasibility Gate haalde)

**Globale precondition (NIEUW v2.1)**: geen Fase 2 build start zonder
**executable-cost model** voor die lane. Dat is per lane:

- **A VRP**: bid/ask opties + perp hedge funding + tail-premie + margin
- **B Staking**: depeg/liquidity haircut + unstaking delay + custody scenario
- **C Basis**: bid/ask + fees + latency + partial-fill + inventory/rebalance

Zonder dat model: terug naar Fase 1, gate niet correct beoordeeld.

### Build-volgorde indien meerdere PASS

**Default: Lane B eerst**, tenzij Lane A uitzonderlijk sterke gate-pass laat
zien (≥10%/yr return on margin met conservatieve aannames). Reden: B hergebruikt
bestaande `bh_overlay`-infra, minste nieuwe execution surface, snelste
betrouwbare paper-test, kleinste verlies bij fout.

### Lane A (VRP) — als PASS

- `scripts/deribit_api.py` — public + auth (option chain, place/cancel,
  position, fills, exercise/expiry handling, Greeks-monitor). KYC + API
  key user-actie vereist.
- `scripts/vrp_strategy.py` — 25Δ strangle + dagelijkse delta-hedge logic,
  weekly roll-schedule, mandatory tail-hedge inkoop.
- `scripts/vrp_runner.py` — analoog aan `carry_runner.py`: cycle, gate
  (VRP-spread > threshold), reconcile, dry-run / demo / live three-state.
  **Extra**: continuous Greeks-monitor (niet alleen cycle-based).
- `configs/vrp-btc.json`, `configs/vrp-eth.json`.
- `deployment/systemd/vrp@.service` op LXC.
- `docs/VRP-OPS.md`.

**Effort**: **7–10 dagen** (was 4d in V2.0 — option-chain navigation +
exercise/expiry + Greeks-monitor + dagelijkse delta-hedge zijn substantieel
meer dan een platte REST-adapter).
**Default state**: dry-run, paper-only.

### Lane B (Staking-overlay) — als PASS

- Uitbreiding van `bh_overlay_strategy.py` met `staking_apr` parameter +
  `depeg_haircut_pct` risk parameter.
- Yield-accrual ledger in `state/bh_overlay/<inst>/state.json`.
- `configs/bh_overlay-eth.json`, `configs/bh_overlay-sol.json`.
- `systemctl enable bh_overlay@eth bh_overlay@sol` op LXC — service-template
  is identiek.
- Dashboard-tab krijgt asset-dropdown + depeg-scenario indicator.
- **Geen** echte staking-acties in Fase 2; runner blijft paper-only tot
  venue-adapter (Kraken EU spot + stake) bestaat — die hoort bij Fase 4 live.

**Effort**: 2 dagen.
**Default state**: paper, yield-accrual gesimuleerd.

### Lane C (Basis) — als PASS

- Drie EU-venue spot-adapters: `scripts/bitvavo_api.py`, `scripts/kraken_api.py`,
  `scripts/coinbase_eu_api.py` (WebSocket top-of-book + REST auth/place/cancel).
- `scripts/basis_runner.py` — paper-arb met pre-funded inventory model,
  simultane execution simulator, realistische latency + fees + partial-fill.
- Voorlopig géén echte orders — paper-simulatie tegen live tickers maar mét
  pre-funded balance accounting.

**Effort**: 4–5 dagen (was 3–4d — WebSocket per venue + inventory accounting
+ partial-fill simulator).
**Default state**: paper, geen creds nodig.

---

## Fase 3 — Paper window (alleen winnaar)

- Paper-window per lane: minimaal **8–12 weken** uninterrupted (was 4w in V2.0).
  Dat moet een sample bevatten dat een regime-overgang dekt, anders test je
  alleen of de strategie werkt in één regime.

**Paper PASS-criteria (V2.1, verstrengd)**:
- Gerealiseerde metric niet slechter dan Fase-1 **p25 verwachting** (niet mean —
  p25 is het scenario waarop je gerust moet kunnen zijn)
- Geen risk-rule breach
- Reconcile gaps <1% van cycles
- Geen unexplained PnL > 0.5% van equity in enige cycle
- Live-like fees/slippage gebruikt
- Alle beslissingen in trade records
- Performance attribution verklaart ≥90% van PnL
- Geen data gaps tijdens kritieke events (bv. tijdens fast-move)
- Geen manual intervention nodig om PnL te redden
- Dashboard, restart en state-recovery werken na restart-drill

## Fase 4 — Live (alleen winnaar van paper)

- At-most-1 lane mag tegelijk naar live.
- Andere PASS-lanes blijven paper-draaien tot de eerste een live-track-record
  heeft van ≥4 weken die binnen 1σ van paper presteert.

---

## Doorlooptijd (V2.1 herzien, realistisch)

| Fase | Mijn werk | User-actie | Cumulatief |
|------|-----------|------------|-----------|
| 0 — DECISIONS + 4 specs (incl. Sham-D) | 0.5d | Lezen+OK | 0.5d |
| 0.5 — Data/Execution Reality Check (parallel) | 1–1.5d max(per lane) | Mogelijk venue KYC-poging | 1.5–2d |
| 1A/1B/1C/Sham-D parallel | max(4d, 2d, 2d, 0.5d) = 4d | — | 5.5–6d |
| Gate review | 0.5d | Beslissen welke door | 6–6.5d |
| 2 — Build winnaar | 2d (B) / 7–10d (A) / 4–5d (C) | Bij A: Deribit KYC + API-key | 8.5–16.5d |
| 3 — Paper ≥8w | passive | passive | 8–11 weken na build-start |
| 4 — Live | 1d + paper-PASS | venue creds + go/no-go call | — |

**N.B.** C-lane heeft 14–30 dagen wall-clock voor WebSocket-data-collectie
parallel met andere werk — geen mijn-werk-tijd, wel realiteit voor wanneer
gate-besluit te nemen.

---

## Risico's bij deze parallel-aanpak (eerlijk)

1. **Aandacht is de bottleneck.** Drie parallelle feasibility + Sham-D via
   subagents werkt, maar als één lane onverwacht diep gaat kan het de andere
   vertragen. Mitigatie: harde 4-dag timebox voor Lane A in Fase 1, anders
   parkeren.

2. **Multiple testing op de Gate.** Vier kandidaten (incl. Sham-D) verlaagt
   per-lane false-positive maar verhoogt portfolio-level. Mitigatie:
   gate-thresholds zijn vooraf strikt + shuffle-test per lane + Sham-D als
   gate-recalibratie-trigger + cumulatieve deflated metrics over alle
   project-historie.

3. **Documentatie-overhead.** Vier specs + vier analyses + vier gate-rapporten
   = veel bestanden voor potentieel 0 winners. Realistisch: bij vorige lanes
   leverde de gestructureerde diagnose zelf hergebruikbare infra op. Geldt
   ook hier.

4. **DECISIONS-coherentie.** De parallel-uitzondering moet expliciet in
   DECISIONS, anders glijdt het terug naar "everything goes". Daarom Fase 0
   voorop.

5. **Realistische uitkomstverdeling (V2.1, eerlijk)**: meest waarschijnlijke
   scenario na Fase 1 met de verstrengde gates is:
   - P(Sham-D FAIL) ≈ 85% (als <85%: gate-architectuur is fundamenteel kapot)
   - P(C PASS) ≈ 10% (efficiënte markten doen hun werk)
   - P(A PASS) ≈ 30% (VRP is documented edge maar tail/hedge/regulatory
     kunnen alle drie kapot maken)
   - P(B PASS) ≈ 40% (overlay-vs-yield-alone is een echte testbare vraag)
   - P(≥1 lane PASS | Sham-D FAIL) ≈ 60%
   - P(zero passes) ≈ 35%
   
   Dat is een acceptabele feasibility-portfolio. Zelfs bij 0 PASS is de output
   waardevol: definitief uitsluiten van deze 3 lanes is informatie. Dwingt
   echte vraag: wordt dit account `bh_overlay@btc only` als productie-strategie?

---

## Beslismomenten (gates)

1. **Na Fase 0**: DECISIONS-entry akkoord? Vier specs realistisch geformuleerd?
2. **Na Fase 0.5**: welke lanes overleven de data/execution/regulatory reality
   check? Lanes die hier falen sluiten zonder Fase 1.
3. **Na Sham-D Fase 1**: komt Sham-D door de gate? Zo ja: herijken voordat
   A/B/C PASS mogen krijgen.
4. **Na Fase 1 (Feasibility Gate)**: hoeveel A/B/C-lanes klaren de vooraf
   vastgelegde edge-threshold? Welke gaat door naar Build?
5. **Na Fase 2 build van winnaar**: code-review + tests groen + smoke-test
   op dry-run? Executable-cost model expliciet gevalideerd?
6. **Na Fase 3 paper-window**: realiseert paper de edge binnen 1σ van Fase-1
   p25-schatting? Restart-drill geslaagd? Attribution ≥90%?
7. **Na Fase 4 live (≥4w)**: hard kill als realiseerde performance
   onderpresteert tov paper met ≥1σ. Geen "geef het meer tijd" zonder
   hypothese waarom paper en live verschilden.

---

## Wijzigingshistorie

- **v1** (`IMPROVEMENT_PLAN.md`, 2026-05-12): COMPLETE met verdict NO-GO over
  hele plan. Bewees dat sizing/multiplier/scoring/filter lagen geen verliezende
  entry-signaal kunnen redden.

- **v2** (`IMPROVEMENT_PLAN_V2.md`, 2026-05-23): pivot naar structural-premium
  harvesting, drie parallelle lanes naar Feasibility Gate, daarna meritocratisch
  serieel verder.

- **v2.1** (deze versie, 2026-05-23): na externe review:
  - Fase 0.5 Data & Execution Reality Check toegevoegd (regulatory + data-
    feasibility check vóór statistische edge-tests)
  - Globale regel: geen Fase 2 Build zonder executable-cost model
  - Lane A VRP gate verstrengd: return-on-margin ≥5%, CVaR, margin_breach_count=0,
    mandatory tail-hedge in baseline, dagelijkse delta-hedge ipv weekly,
    bid/ask ipv mid/mark, data-venster 2019+ ipv post-LUNA. Effort 4d ipv 2d
    Fase 1, 7–10d ipv 4d Fase 2.
  - Lane B Staking benchmark gecorrigeerd: `BH+yield` is de juiste null, niet
    random-entry. Vergelijkingsbatterij + attribution decompositie + depeg/
    haircut scenarios + SOL unstaking delay expliciet.
  - Lane C Basis herframed: "pre-funded cross-venue inventory spread capture",
    geen risk-free transfer-arb. Executable bid/ask + depth via WebSocket,
    14–30d window ipv 3d, FX-leg expliciet.
  - Paper-gate verstrengd: p25 ipv mean, attribution ≥90% van PnL,
    restart/state-recovery werken, geen manual intervention.
  - Doorlooptijd realistisch herzien (Fase 1 = 4d, paper = 8–12w).
  - Sham-control discipline toegevoegd: per-lane shuffle-test + globale
    Sham-D synthetische lane (UTC-uur rotatie) parallel met A/B/C.
  - Multiple-testing penalty uitgebreid naar cumulatieve project-historie
    (alle eerdere lanes meegenomen).
  - bh_overlay@btc 5.9pp claim uit motivatie geschrapt (10d = ruis).
  - At-most-1-live rationale (tail-correlation in crypto stress) expliciet
    in DECISIONS opgenomen.
  - Eerlijke P(uitkomst)-verdeling opgenomen onder Risico's.
