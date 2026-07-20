# HL carry (carry@hl-btc) — onboarding & activatie

**Ontwerp (vastgesteld 2026-07-20):** dedicated wallet, géén sub-account — HL
vereist $100k cumulatief volume voor `createSubAccount` (master zat op $6,3k).
De LXC krijgt alléén de **agent-key** van de carry-wallet; class-transfers
(perp↔spot) kan een agent niet signen (testnet-geverifieerd), dus de
spot/perp-splitsing van een storting doe je eenmalig in de HL-UI.

## Stappen voor de operator (Michiel, ~10 min)

1. **Maak een verse wallet** (nieuwe key in je eigen wallet-app; deze wallet
   gaat alléén carry draaien — nooit een andere lane erop).
2. **Stort USDC** naar die wallet op Hyperliquid (zoals eerdere stortingen;
   richtbedrag ≈ $1.500 bij de huidige $1.000 per-leg cap).
3. **Splits in de HL-UI** (Portfolio → Transfer): **~55% naar Spot**, ~45%
   blijft in Perp. (Spot koopt de UBTC-leg; Perp is margin voor de short.)
4. **Approve een agent/API-wallet** in de HL-UI terwijl je met de carry-wallet
   verbonden bent (More → API). Bewaar de agent-private-key even lokaal.
5. Geef Claude (of draai zelf) op de LXC:

```bash
# eenmalig — agent-key via stdin, komt alleen in /etc/trading-bot/carry-hl-btc.env (0600)
python3 -m scripts.carry_hl_go onboard --master 0x<carry-wallet>
# controle: saldi + sizing die `go` zou toepassen
python3 -m scripts.carry_hl_go check
# activeren (sized van echte saldi; flipt dry_run/allow_live/HL_CONFIRM_LIVE; herstart unit)
python3 -m scripts.carry_hl_go go
```

Terugparkeren: `python3 -m scripts.carry_hl_go park` (bij open positie eerst
de halt-sentinel `state/carry/btc-hl/halt` zetten zodat de runner unwindt).

## Wat er al klaarstaat (geen actie nodig)

- unit `carry@hl-btc` draait in **DRY_RUN** (gate-observability, geen creds,
  geen orders — dubbele gate in `hl_carry_adapter` + `mode_gate`);
- config `configs/carry-hl-btc.json`: L≤2-cap, basis-kill 1%, green-button
  +5%/yr trailing-90d (hourly, 2160 samples), per-leg cap $1.000;
- `scripts/hl_sub_setup.py` blijft beschikbaar voor als het volume ooit de
  $100k passeert en een sub-account alsnog netter is;
- dagrapport (health_report.py) toont de lane-status.

## Verwachting bij activatie

Green-button staat AAN (5,49%/jr trailing). De runner opent dan binnen één
cycle (60s) de delta-neutrale positie: spot UBTC long + perp BTC short, per-leg
≈ min(vrije spot/1,02; perp-AV/0,8; $1.000). Rendement op ingezet kapitaal bij
de huidige funding ≈ 3–4%/jr; het historische ON-gemiddelde uit HL-CARRY-STUDY
is ~10,6%/jr bij L=2. OFF-flip van de knop → runner unwindt zelf.
