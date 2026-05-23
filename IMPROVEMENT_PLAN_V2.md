# Blofin Trader — Verbeterplan V2: Drie Parallelle Structural-Premium Lanes

> Opvolger van `IMPROVEMENT_PLAN.md` (v1, 2026-05-12, COMPLETE met verdict NO-GO).
> V1 probeerde de bestaande prijs-directionele 'advanced' strategie te verbeteren
> via filter/sizing/scoring lagen — alle 6 fases bewezen geen edge. De 8-axis
> edge-diagnose (`docs/edge-diagnosis/A..I-*.md`) toonde aan dat de entry-edge
> structureel ontbreekt, niet door tuning te repareren.

> V2 pivot: stop met prijs voorspellen. Oogst structurele premies op
> EU-toegankelijke rails. Drie parallelle sondes naar Feasibility Gate.

**Project**: `blofin-trader` v2.7+
**Locatie**: `/Users/michiel/Downloads/openclaw/blofin-trader/`
**Datum**: 2026-05-23

---

## Waarom drie lanes parallel (en hoever)

DECISIONS.md (2026-05-12) bevatte de regel "one strategy at a time". Dit plan
zet die regel **selectief** opzij: parallel werk is alleen toegestaan in de
read-only feasibility-fase (geen runtime, geen orders, alleen data + null-tests).
Vanaf de Build-fase blijft "one at a time" gelden.

**Rationale**: parallelle feasibility multipliceert mijn aandacht maar niet het
multiple-testing-risico op productie — drie kandidaten brengen we tegelijk tot
de gate, daarna meritocratisch één naar paper. De kosten zijn voornamelijk
documentatie + data-fetch tijd, niet runtime-bandbreedte op de LXC.

**Patroon uit de 4 afgesloten lanes**:

| Lane | Faal-modus | Onderliggende oorzaak |
|------|-----------|----------------------|
| 1. Advanced (multi-indicator confluence) | 8-axis bewijs van geen entry-edge | Prijs-features hebben ~0 IC vs forward returns |
| 2. Plan-D (BTC 5m mean-reversion) | Fee-share >60%, base rate 27% verkeerde kant op | 5m churn op $115 onmogelijk; reversion-hypothese fout |
| 3. v1 trend-overlay (daily) | 3/3 rules onder random-entry-null OOS | Daily trend op 3.3y BTC niet onderscheidbaar van random-long |
| 4. Cash-and-carry | **Sharpe 6.95, Calmar 6.68 in backtest** maar 0 venues toegankelijk | EU MiCA / venue-API regulatory wall, *niet* strategie-falen |

De drie prijs-directionele lanes faalden op edge-niveau. De enige structurele
lane (4) had wél een echte edge — alleen door uitvoeringsbeperkingen onbruikbaar.
Plus `bh_overlay@btc` (geen entry-signaal, alleen vol-target + drawdown-stop)
verslaat BTC-BH momenteel met ~+5.9pp in 10 dagen paper. **Conclusie**: pivot
naar structural-premium harvesting op EU-toegankelijke rails.

---

## Doorlopende principes (gelden voor élke lane)

- Elke nieuwe feature defaultt op `enabled: false` / paper-only — bot-gedrag
  blijft identiek aan vorige versie tot expliciet aangezet
- Random-entry-null gate vóór backtest-optimalisatie (DECISIONS 2026-05-12)
- Edge-diagnose template (`docs/edge-diagnosis/`) toepassen indien lane naar
  paper gaat
- Holdout reserveren, eenmaal evalueren, deflated metrics rapporteren
- Logging — elke beslissing in trade record voor post-hoc analyse
- At-most-1 lane tegelijk LIVE; andere PASS-lanes blijven paper-draaien

---

## Fase 0 — DECISIONS update + per-lane charter (gedeeld, vóór code)

Vóór één regel code: één DECISIONS-entry die expliciet:

- (a) erkent dat dit afwijkt van "one strategy at a time"
- (b) parallel toestaat t/m Feasibility Gate alleen
- (c) kill-criteria per lane up-front
- (d) bevestigt dat at-most-1 lane tegelijk naar live

Per lane één `docs/STRATEGY-V2-<LANE>.md` met:

- Hypothese in één zin (testbaar)
- Data-bron + dekking
- Random-entry / null-benchmark formulering
- Edge-grootte threshold om door te gaan (vooraf vastgelegd, niet post-hoc)
- Risico-modus + tail-scenario
- Kill-criterium

**Effort**: 0.5 dag totaal voor alle drie.
**Output**: 1 commit, 4 files (1 DECISIONS append + 3 specs).

---

## Fase 1 — Feasibility (parallel, read-only, geen runtime)

Voor elke lane: data ophalen + statistical edge-test + go/no-go rapport. Geen
adapter, geen orders, geen runner-werk. Uitvoeren via parallel subagents (één
per lane) — onafhankelijk werk, geen shared state.

### Lane A — Volatility Risk Premium (Deribit options)

**Hypothese**: BTC/ETH implied volatility ligt structureel boven realized
volatility, met genoeg materialiteit (≥5%/yr na fees) om als premie te oogsten
via delta-gehedgede short-vol posities.

| Stap | Output |
|------|--------|
| A1 Data | `backtest/data/deribit_iv_btc.csv`, `deribit_iv_eth.csv`. ATM IV + 25Δ skew daily, 3+ jaar. Deribit public API, geen creds. |
| A2 Realized vol | Rolling 7/30/90d realized vol uit bestaande spot-bars. |
| A3 VRP-tijdreeks | `backtest/analyze_vrp.py` — IV(t) vs realized_vol(t, t+horizon) per horizon (1w/2w/1m). Spearman + materialiteit (gemiddelde + p25/p75). |
| A4 Regime-cuts | VRP in bull / bear / sideways segmenten (zelfde classifier als Fase-1 v1-plan). Spike-events: LUNA mei-2022, FTX nov-2022, ETF jan-2024, carry-unwind aug-2024. |
| A5 Verkoop-simulatie | Naïeve short ATM straddle, delta-flat init, weekly roll, vergelijken met B&H. Géén optimalisatie — eerst kale meting. |

**Effort**: 2 dagen (1d data + skew-fetch pagineren, 1d analyse).
**Gate**: VRP gemiddeld ≥0 OOS over 2 splits, holdout-stabiel binnen 1σ, geen
catastrofale tail in spike-events groter dan totale 12m premie. Anders: NO-GO
+ document, sluit lane.

### Lane B — Staking-yield + bh_overlay (ETH/SOL spot)

**Hypothese**: het toevoegen van staking-yield (~3-7%/yr) aan de bestaande
bh_overlay strategie creëert een composeerbare edge die het random-entry-null
band klaart op risk-adjusted basis.

| Stap | Output |
|------|--------|
| B1 Yield-data | Lido stETH historische APR (vrij beschikbaar), SOL staking yield (Solana Beach API). 3+ jaar voor ETH, ~2y bruikbaar voor SOL. |
| B2 ETH/SOL daily bars | Reeds beschikbaar via bestaande `data_collector.py`. |
| B3 Overlay-backtest | Composeer `bh_overlay_strategy.py` met yield-accrual: dagelijks `+= yield_apr/365 * spot_qty`. Geen nieuwe strategie-logica. |
| B4 Vergelijking | Per asset: pure-BH vs BH+stake vs bh_overlay+stake. Op Calmar, max DD, alpha vs BTC-BH. |
| B5 Random-entry-null | Hergebruik `backtest/random_entry_null.py` op de overlay+stake combinatie. |

**Effort**: 1.5 dag (0.5d yield-fetch, 1d backtest + analyse).
**Gate**: BH+overlay+stake klaart Calmar ≥0.5σ boven het random-entry-null band
op holdout. Anders: NO-GO. Geen "yield maakt alles beter" zonder bewijs.

### Lane C — Cross-exchange spot basis (Bitvavo / Kraken / Coinbase EU)

**Hypothese**: prijsverschillen tussen drie grote EU spot-venues op BTC/ETH/USDT
zijn na realistische fees + transfer-kosten + latency materieel genoeg
(≥0.10%/trade op ≥10% van observaties) om als risk-free spread te oogsten.

| Stap | Output |
|------|--------|
| C1 Public-ticker poll | Script 3 dagen lopen op 1s polling, drie venues, BTC-EUR + ETH-EUR + BTC-USDT (waar beschikbaar). `backtest/data/basis_*.csv`. Geen creds. |
| C2 Fee-tier check | Maker/taker per venue (publiek), withdraw fee + tijd per asset, FX-spread EUR↔USD. Realistische round-trip kosten. |
| C3 Spread-analyse | Histogram van net spread na fees + withdraw-tijd-discount. Hoeveel % van tijd is netto >0? Welke capaciteit op die spread (orderbook depth)? |
| C4 Latency-realiteit | Simuleer execution: 200–500ms slippage, partial-fill, transfer-tijden 5–30min. Hoeveel "papier-edge" overleeft realistische executie? |

**Effort**: 1 dag (0.5d poll + 0.5d analyse — 3-dagen data verzamelt zichzelf
parallel met andere werk).
**Gate**: net spread na realistische fees + slippage + transfer-discount
≥0.10% per trade op ≥10% van observaties bij capaciteit ≥$1k. Anders: NO-GO —
efficiënte markten doen hun werk.

### Feasibility Gate (synchroon na alle drie)

Na elke lane Phase 1 afgerond → samen kijken naar 3 rapporten. Beslissing:

- **0 lanes PASS**: zoek lane #6 of accepteer fallback-pad (`bh_overlay@btc`
  paper voortzetten).
- **1 lane PASS**: ga naar Fase 2 voor die lane.
- **≥2 lanes PASS**: kies één voor build/paper op basis van (a) edge-grootte
  (b) executie-kosten (c) infra-hergebruik. Andere lane(s) blijven gespect maar
  wachten tot eerste paper-PASS of NO-GO.

**Waarom serieel vanaf hier**: drie nieuwe adapters tegelijk = drie keer zoveel
oppervlak voor bugs, drie tegelijk paper-evalueren multipliceert het
multiple-testing-probleem op holdout-selectie. Geen meerwaarde tov sequentieel
met de winnaar eerst.

---

## Fase 2 — Build (alleen voor lane(s) die Feasibility Gate haalden)

### Lane A (VRP) — als PASS

- `scripts/deribit_api.py` — public + auth (option chain, place/cancel,
  position, fills). KYC + API key user-actie vereist.
- `scripts/vrp_strategy.py` — delta-hedge logic, roll-schedule (weekly),
  tail-protection (5%-OTM long put kopen voor elke short straddle).
- `scripts/vrp_runner.py` — analoog aan `carry_runner.py`: cycle, gate
  (VRP-spread > threshold), reconcile, dry-run / demo / live three-state.
- `configs/vrp-btc.json`, `configs/vrp-eth.json`.
- `deployment/systemd/vrp@.service` op LXC.
- `docs/VRP-OPS.md`.

**Effort**: 4 dagen.
**Default state**: dry-run, paper-only.

### Lane B (Staking-overlay) — als PASS

- Uitbreiding van `bh_overlay_strategy.py` met `staking_apr` parameter
  (config-driven, geen logica).
- Yield-accrual ledger in `state/bh_overlay/<inst>/state.json` (extra veld
  `staked_yield_accrued`).
- `configs/bh_overlay-eth.json`, `configs/bh_overlay-sol.json`.
- `systemctl enable bh_overlay@eth bh_overlay@sol` op LXC — service-template
  is identiek.
- Dashboard-tab krijgt asset-dropdown.
- **Geen** echte staking-acties; runner blijft paper-only tot venue-adapter
  (Kraken EU spot + stake) bestaat — die hoort bij Fase 3 indien live.

**Effort**: 2 dagen.
**Default state**: paper, yield-accrual gesimuleerd.

### Lane C (Basis) — als PASS

- Drie EU-venue spot-adapters: `scripts/bitvavo_api.py`, `scripts/kraken_api.py`,
  `scripts/coinbase_eu_api.py` (publiek-data eerst, auth + place_order daarna).
- `scripts/basis_runner.py` — paper-arb: detecteer net spread, simuleer
  cross-venue legs met realistische latency + fees + transfer-tijden.
- Voorlopig géén echte orders — paper-simulatie tegen live tickers.

**Effort**: 3–4 dagen voor 3 adapters + runner.
**Default state**: paper, geen creds nodig.

---

## Fase 3 — Paper window (alleen winnaar)

- Paper-window per lane: minimaal **4 weken** uninterrupted, dashboard-zichtbaar.
- PASS-criteria: paper realiseert edge-grootte uit Fase 1 binnen 1σ; geen halts;
  reconcile-errors <1% van cycles.

## Fase 4 — Live (alleen winnaar van paper)

- At-most-1 lane mag tegelijk naar live.
- Andere PASS-lanes blijven paper-draaien tot de eerste een live-track-record
  heeft van ≥4 weken.

---

## Doorlooptijd

| Fase | Mijn werk | User-actie | Cumulatief |
|------|-----------|------------|-----------|
| 0 — DECISIONS + 3 specs | 0.5d | Lezen+OK | 0.5d |
| 1A/1B/1C parallel | max(2d, 1.5d, 1d) = 2d | — | 2.5d |
| Gate | 0 | Beslissen welke door | 2.5d |
| 2 — Build winnaar | 2–4d afh. lane | Bij A: Deribit KYC + API-key | 4.5–6.5d |
| 3 — Paper ≥4w | passive | passive | 4–6 weken na start |
| 4 — Live | 1d + paper-PASS | venue creds + go/no-go call | — |

---

## Risico's bij deze parallel-aanpak (eerlijk)

1. **Aandacht is de bottleneck.** Drie parallelle feasibility-studies via
   subagents werkt, maar als één lane onverwacht diep gaat (bijv. VRP tail-
   modeling) kan het de andere twee vertragen. Mitigatie: harde 2-dag timebox
   per lane in Fase 1, daarna rapporteren waar het staat.

2. **Multiple testing op de Gate.** Drie kandidaten betekent 3× zoveel kans
   dat één per toeval de null klaart. Mitigatie: gate-thresholds zijn vooraf
   strikt (niet "best of 3 wins") + per-lane holdout = niet gedeeld.

3. **Documentatie-overhead.** Drie specs + drie analyses + drie gate-rapporten
   = veel bestanden voor potentieel 0 winners. Realistisch: bij vorige lanes
   leverde de gestructureerde diagnose zelf hergebruikbare infra op
   (`random_entry_null.py`, `edge-diagnosis/`-template). Dat geldt hier ook.

4. **DECISIONS-coherentie.** De parallel-uitzondering moet expliciet in
   DECISIONS, anders glijdt het terug naar "everything goes". Daarom Fase 0
   voorop.

---

## Beslismomenten (gates)

Op deze momenten expliciet stoppen en beslissen of door te gaan:

1. **Na Fase 0**: DECISIONS-entry akkoord? Drie specs realistisch geformuleerd?
   Zo nee: aanpassen vóór Fase 1.
2. **Na Fase 1 (Feasibility Gate)**: hoeveel lanes klaren de vooraf
   vastgelegde edge-threshold? Welke gaat door naar Build?
3. **Na Fase 2 build van winnaar**: code-review + tests groen + smoke-test
   op dry-run? Zo nee: niet starten op LXC.
4. **Na Fase 3 paper-window**: realiseert paper de edge binnen 1σ van Fase-1
   schatting? Zo nee: NO-GO live, terug naar tekentafel of switch naar
   tweede-keus lane.
5. **Na Fase 4 live (≥4w)**: hard kill als realiseerde performance
   onderpresteert tov paper met ≥1σ. Geen "geef het meer tijd" zonder
   hypothese waarom paper en live verschilden.

---

## Wijzigingshistorie

- **v1** (`IMPROVEMENT_PLAN.md`, 2026-05-12): COMPLETE met verdict NO-GO over
  hele plan. Bewees dat sizing/multiplier/scoring/filter lagen geen verliezende
  entry-signaal kunnen redden. Pivot was nodig, niet meer plan-versies op
  dezelfde 'advanced' familie.
- **v2** (dit plan, 2026-05-23): pivot naar structural-premium harvesting,
  drie parallelle lanes naar Feasibility Gate, daarna meritocratisch serieel
  verder.
