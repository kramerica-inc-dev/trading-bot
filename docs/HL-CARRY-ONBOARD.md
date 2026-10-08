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
# activeren — PAS NA de B2-checklist in ~/Projects/trading/hl-lanes/LANE-carry.md
python3 -m scripts.carry_hl_go go
```

`go` (sinds 2026-10-08): valideert de live-config eerst met de eigen mode-gate
van de runner (schrijft niets als die faalt), stopt de unit, **archiveert de
DRY-state** (`state.json.dry-<ts>` — het gesimuleerde boek mag nooit bij de
live runner komen; de runner weigert zo'n state ook zelf), schrijft config+env
atomisch (backups `*.bak-go-<ts>`), start en wacht op een **verse** P3_LIVE-
cycle zonder halt. Mislukt start of verificatie → rollback (config, env, state
terug, unit herstart in DRY). Uitzondering: toont health dan al een open boek,
dan zet `go` de halt-sentinel (runner unwindt) en meldt exit 2 i.p.v. terug
te flippen.

Terugparkeren: `python3 -m scripts.carry_hl_go park` — weigert zolang health
een open live boek toont (eerst `touch state/carry/btc-hl/halt` en wachten tot
flat; `--force` overrulet). Archiveert de live-state zodat de DRY-proef schoon
herstart.

## Units, alerts, rapport (2026-10-08)

- **Eén unit: `carry@hl-btc`.** `carry-hl@.service` is uitgefaseerd (wees naar
  dezelfde config/state/env). De runner neemt bovendien een exclusieve flock
  op `state/carry/btc-hl/runner.lock`; een tweede instantie stopt direct.
- **Telegram-alerts** uit de runner (open, close, halt, basis-blowout,
  legging-abort, reconcile-fout, margin-low; aanhoudende condities 1× per 6u)
  via `/etc/trading-bot/carry-alerts.env` met alléén `TELEGRAM_BOT_TOKEN` en
  `TELEGRAM_CHAT_ID` (0600). Nooit een env met HL-keys van een andere lane
  hergebruiken: de runner valt terug op `HL_PRIVATE_KEY`/`HL_ACCOUNT_ADDRESS`.
- **Dagrapport:** `scripts/carry_health_check.py` levert het carry-blok. Prod-stap
  in `/opt/trading-bot/health_report.py` (prod-only bestand):
  `from carry_health_check import carry_report_lines` en
  `lines += carry_report_lines()` waar de andere lane-blokken worden opgebouwd.
  Los te draaien: `python3 -m scripts.carry_health_check` (exit 1 bij issues).
- **Green-watch** (`deployment/carry_green_watch.sh`) gebruikt
  `carry_runner --gate-only`: geforceerd DRY, zonder creds, raakt geen state.

## DRY-proef (gesimuleerd boek)

In DRY_RUN opent/sluit de runner bij de green-button een **gesimuleerd** boek:
gesized als `go` bij een storting van $1.500 (55/45 → per-leg $808,82), fills op
de mids van de cycle, HL-takerfees (spot 0,07%, perp 0,045%), funding per
afgerekende uur-settlement uit HL `fundingHistory`, basis-P&L mark-to-market.
Resultaat in `health.json` (`pnl`, `simulated_equity`, `sim_started_ts`) en op
het dashboard. Proefcriteria: `hl-lanes/LANE-carry.md`.

## Wat er al klaarstaat (geen actie nodig)

- unit `carry@hl-btc` draait in **DRY_RUN** (gesimuleerd boek, geen creds,
  geen orders — dubbele gate in `hl_carry_adapter` + `mode_gate`);
- config `configs/carry-hl-btc.json`: L≤2-cap, basis-kill 1%, green-button
  +5%/yr trailing-90d (hourly, 2160 samples, ≥1080 vereist), per-leg cap $1.000;
- `scripts/hl_sub_setup.py` blijft beschikbaar voor als het volume ooit de
  $100k passeert en een sub-account alsnog netter is;
- dagrapport: zie "Units, alerts, rapport" — de prod-hook moet nog gezet.

## Verwachting bij activatie

Green-button staat AAN (5,49%/jr trailing). De runner opent dan binnen één
cycle (60s) de delta-neutrale positie: spot UBTC long + perp BTC short, per-leg
≈ min(vrije spot/1,02; perp-AV/0,8; $1.000). Rendement op ingezet kapitaal bij
de huidige funding ≈ 3–4%/jr; het historische ON-gemiddelde uit HL-CARRY-STUDY
is ~10,6%/jr bij L=2. OFF-flip van de knop → runner unwindt zelf.
