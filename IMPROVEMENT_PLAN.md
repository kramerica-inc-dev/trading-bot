# Crypto Trader — Verbeterplan: Alpha vs. Buy-and-Hold

> Gebaseerd op de v2-analyse (na correctie van overstatement in v1).
> Doel: meetbaar betere risk-adjusted performance dan BTC buy-and-hold,
> via empirisch gevalideerde stappen — geen architectuur-claims zonder data.

**Project**: `blofin-trader` v2.7
**Locatie**: `/Users/michiel/Downloads/openclaw/blofin-trader/`
**Deployment**: Proxmox LXC 25020 → `192.168.2.140:/opt/trading-bot/`

---

## Fase 0 — Voorbereiding en scope

Twee items uit de v2-analyse (`signal_sign` flip, ML classifier diagnose) zijn
specifiek voor de kramerica trading-bot — niet van toepassing op Crypto Trader.
Laten vallen. Zes items blijven over en mappen op de bestaande roadmap.

**Doorlopende principes** (gelden voor élke fase):

- Elke nieuwe feature defaultt op `enabled: false` — bot-gedrag blijft identiek
  aan vorige versie tot expliciet aangezet
- Geen claim zonder meting — elke fase eindigt met cijfers (Sharpe / Calmar /
  alpha-vs-BH) op holdout, niet alleen train
- Multiple-testing discipline — holdout reserveren, eenmaal evalueren, deflated
  metrics rapporteren bij elke optimalisatie
- Logging — elke beslissing (risk score componenten, bear-check uitkomst)
  geserialiseerd in trade record voor post-hoc analyse

---

## Fase 1 — Benchmark + risk-adjusted metrics in backtester

**Waarom eerst**: zonder deze meting is elk volgend experiment betekenisloos.
Je weet niet of een verandering verbetering of cherry-picked ruis is.

### Scope

- BTC buy-and-hold benchmark over dezelfde periode (return, max DD, DD-duur)
- Strategie-metrics uitbreiden: Calmar ratio, DD-duur, tijd onder water
- Alpha-vs-benchmark als expliciete metric in output
- **Conditional metrics**: prestatie tijdens bull-segmenten vs. bear-segmenten
  apart. De échte vraag is niet "verslaan we BTC overall" maar "beschermen we
  kapitaal als BTC daalt"

### Concrete plekken

- `backtest/backtester.py` → `BacktestResult._compute_metrics()` uitbreiden
- `BacktestResult.summary()` en `to_dict()` mee uitbreiden
- `backtest/run_backtest.py` en `run_baseline.py` → output tonen
- Dashboard (`scripts/dashboard_api.py`, `scripts/dashboard.html`) → benchmark
  tonen naast strategie-equity-curve

### Bull/bear segment-detectie

Voor conditional metrics: classificeer elke dag in de backtest-periode als
bull / bear / sideways op basis van een eenvoudige regel (bv. BTC 30d return
> +5% = bull, < −5% = bear, anders sideways). Berekend over rolling window,
géén lookahead.

### Verificatie

- Bestaande 38 tests blijven groen
- Nieuwe unit tests voor benchmark-berekening (bull-period, bear-period,
  flat-period, edge cases: enkele candle, lege trades-lijst)
- Visuele sanity check: equity curve van bot vs. BTC buy-and-hold over
  recente run, met conditional metrics in legend

### Effort en default state

- **Effort**: 0.5 dag
- **Default state**: altijd actief — pure meting, geen gedragsverandering

---

## Fase 2 — Look-ahead audit op primary-TF feature engineering

**Waarom nu**: `HTFCandleSync` regelt MTF-lookahead al, maar feature-berekening
op de primary timeframe (`advanced_strategy.py`) is niet expliciet geauditeerd.
Eén lookahead daar maakt alle backtest-resultaten onbetrouwbaar — inclusief de
benchmark-meting uit Fase 1.

### Audit-checklist

Voor elke indicator-functie en feature in `advanced_strategy.py`:

- **Rolling indicators** (RSI, MACD, BB, ATR): wordt huidige bar meegenomen in
  het window, of alleen `[t-N : t-1]`?
- **Entry-triggering**: signal op bar `t` gebaseerd op data ≤ `t-1`, of
  inclusief `t`?
- **Regime-detectie features** (efficiency, trend strength, anchor slope):
  gebruiken alleen gesloten bars
- **Volume normalisatie**: gemiddelde over historie, niet inclusief huidige
- **Quality score / confidence**: zelfde discipline — geen huidige bar in de
  componenten

### Methodologie

1. Code-review per indicator-functie, met expliciete notatie wat de
   "current bar" status is (in code als comment toevoegen)
2. **Shift-by-one test**: shift alle features met 1 bar en herhaal backtest
    - Als resultaten ~identiek zijn → geen materiële lookahead
    - Als ze significant verschillen → lookahead gevonden, fixen
3. Documenteren in `docs/lookahead-audit.md` (per indicator: status + bewijs)

### Tooling

Nieuwe test `tests/test_lookahead_discipline.py`:
- Voor elke indicator: feed de strategy hetzelfde window twee keer (met en
  zonder de "huidige" bar), check dat de indicator-output identiek is

### Verificatie

- Shift-by-one test als CI-test toegevoegd
- Voor elke indicator: expliciete comment in code over bar-handling
- `docs/lookahead-audit.md` met audit-bevindingen

### Effort en default state

- **Effort**: 0.5–1 dag, afhankelijk van wat de audit oplevert
- **Default state**: fixes worden direct toegepast (correctheidsfix, geen
  feature met config-flag)

---

## Fase 3 — Funding rate: empirische analyse vóór integratie

**Belangrijk**: eerst meten of het signaal stand houdt op jouw data, dan pas
in de bot stoppen. Niet andersom — anders bouw je een feature die niet werkt.

### Stap 3a — Historische data ophalen

- BloFin funding rate endpoint: historische funding rates BTC-USDT over de
  volledige backtest-periode
- Opslaan in `backtest/data/funding_btc_usdt.csv`
- Nieuwe functie `fetch_funding_history()` in `backtest/data_collector.py`
- Pagineren analoog aan bestaande candle-fetching

**Effort**: 0.5 dag

### Stap 3b — Correlatie-analyse

Notebook of script `backtest/analyze_funding.py`:

- Funding rate vs. subsequent BTC return op horizons: 4h, 24h, 7d
- Percentielen plotten: wat is "extreme" funding? (p5, p25, p50, p75, p95)
- Sub-analyses per regime (bull / bear / range / chop)
- Spearman rank correlatie + visuele scatter plots

**Beslismoment** (gate naar 3c):

- Correlatie statistisch significant in-sample én out-of-sample (latere helft
  van de data)
- Effect-grootte materieel (niet alleen significant — bv. top-decile funding
  → subsequent return verschil van minimaal 0.5% over 24h horizon)
- Robuust over regimes (niet alleen werkend in één regime)

Als één van bovenstaande faalt → niet integreren, document waarom in
`docs/funding-analysis.md`. Tijd niet verspild — je weet nu dat het hier
geen edge is.

**Effort**: 1 dag

### Stap 3c — Integratie als input voor risk score

Komt in Fase 5 (continue risk score). Niet als binaire flip, maar als
continue input. Gewicht data-gedreven bepaald in Fase 5, niet vooraf gekozen.

### Verificatie

- Correlatie-analyse documenteert: signaal bestaat of niet, op welke horizon,
  in welk regime
- Plots en tabellen in `docs/funding-analysis.md`

### Effort en default state

- **Effort**: 1.5 dag totaal voor 3a + 3b
- **Default state**: alleen analyse — geen bot-gedragsverandering in deze fase

---

## Fase 4 — Walk-forward calibratie van regime-multipliers

**Waarom hier**: Fase 1 levert de benchmark-metric die nodig is om "betere"
multipliers te identificeren. Fase 2 zorgt dat de calibratie niet op
lookahead-besmette resultaten gebaseerd wordt.

### Aanpak

- Bestaande infrastructuur in `backtest/calibrate_per_timeframe.py` als template
- Nieuw script `backtest/calibrate_regime_multipliers.py`
- Parameter-grid: `bull_trend`, `bear_trend`, `range` multipliers
- `chop` en `unclear` blijven op `0.0` in deze fase — komen pas later in beeld
- 3 splits, 70/30 train/test, walk-forward (zelfde patroon als per-TF calibratie)
- Optimalisatiedoel: **Calmar ratio**, niet raw return — consistent met
  risk-adjusted philosophy

### Grid-design (cruciaal voor multiple-testing)

- Maximum 5×5×5 = 125 combinaties per split (grof grid, niet fijn-mazig)
- Range per multiplier: 0.5, 0.75, 1.0, 1.25, 1.5
- Géén iteratief fijner zoeken rond de winner — dat veroorzaakt overfit

### Multiple-testing discipline

- **Deflated Sharpe ratio** rapporteren naast Sharpe (Bailey & López de Prado,
  2014)
- **Holdout set**: laatste 20% van data reserveren, **één keer** evalueren aan
  het eind. Niet eerder, niet meerdere keren
- Spreiding van Sharpe over alle 125 combinaties plotten — als de winner een
  uitschieter is in een ruisveld → niet overnemen, multipliers ongewijzigd laten

### Output

Resultaten naar nieuwe optionele config-section `regime_multipliers`:

```json
"regime_multipliers": {
  "enabled": false,
  "bull_trend": 1.0,
  "bear_trend": 0.8,
  "range": 0.55
}
```

### Verificatie

- Holdout-performance ligt binnen 1σ van train-performance van de winner
- Visualisatie: equity curves train vs. holdout met oude én nieuwe multipliers
- Als holdout significant slechter dan train → niet overnemen

### Effort en default state

- **Effort**: 1 dag implementatie + 0.5 dag runtijd/analyse
- **Default state**: `enabled: false` — bot blijft bestaande multipliers
  gebruiken totdat je expliciet activeert

---

## Fase 5 — Continue weighted risk score

**Roadmap-match**: dit is exact de geplande "weighted risk scoring" — vervangt
`min_confidence` / `min_quality_score` / `min_regime_confidence` als binaire
gates.

### Architectuur

Nieuwe module `scripts/risk_scoring.py`:

```python
def compute_risk_score(signal, market_context) -> float:
    """Returns 0.0–1.0 — feeds position size, not gate decision."""
```

**Inputs**:

- Signaal-confidence (huidige `confidence`)
- Quality score
- Regime confidence
- Funding bias (uit Fase 3c, als die positief uitviel)
- MTF-alignment

**Score-naar-size mapping**: continue functie, niet drempel. Voorbeeld:

```python
position_size_multiplier = clip(2 * score - 0.5, 0.0, 1.0)
```

→ score 0.25 = 0× (geen trade), score 0.5 = 0.5×, score 0.75 = 1.0×

Dit vervangt het huidige binaire patroon waarbij sub-threshold signals
volledig worden geweerd. Lage confidence ≠ geen trade, maar kleinere trade.

### Gewichten bepalen — twee opties

**Optie A**: gelijke gewichten als startpunt, dan walk-forward calibratie
analoog aan Fase 4 (grof grid).

**Optie B**: Ridge / Lasso regressie van forward-returns op gewogen
componenten, met cross-validation om gewichten te leren.

**Aanbeveling**: A eerst (eenvoudiger, lager overfit-risico, makkelijker te
interpreteren). B alleen als A niet convergeert.

### A/B test (verplicht vóór activatie)

- Backtest met oude binaire gates vs. nieuwe continue score op holdout
- Metrics: aantal trades, gemiddelde grootte, Sharpe, Calmar, max DD,
  alpha-vs-BH
- Verwachting: meer trades met kleinere gemiddelde grootte, vergelijkbare
  of betere Calmar
- Als Calmar verslechtert → niet overnemen, analyse waarom

### Fallback discipline

Oude binaire gates blijven in code achter een config-flag voor minstens één
release. Toggle kan terug naar oude gedrag zonder code-rollback.

### Effort en default state

- **Effort**: 1.5 dag implementatie, 1 dag calibratie + A/B test
- **Default state**: `enabled: false` via `risk_scoring.enabled` config-flag

---

## Fase 6 — Bear-check / devil's advocate module

**Roadmap-match**: hoogste prio uit het verbeterplan in memory. Past nu in
een natuurlijke plek — als één van de inputs op de risk score uit Fase 5,
of als finale gate vóór order-submissie.

### Twee implementatie-opties

**Optie A — Deterministische checklist** (aanbevolen, eerst):

Vóór elke entry: evalueer bearish case (voor longs) of bullish case (voor
shorts). Concrete checks:

- MTF tegenstand (lagere timeframe bullish, hogere timeframe bearish?)
- Funding op extreme (uit Fase 3c)
- Recente lower highs / lower lows binnen window
- BB-extremen tegen positie in
- Hoge correlatie met recente verloren trades (zelfde regime, zelfde setup?)

Score 0.0–1.0 voor "tegenargument-sterkte". Hoog tegenargument → lagere risk
score (géén harde blokkade — past in continue-score architectuur uit Fase 5).

**Optie B — LLM periodic auditor** (later, los project):

- Niet real-time (te duur/traag voor elke trade)
- Event-triggered: bij grote drawdowns, regime-switches, of na N trades
- Geeft strategisch advies, geen real-time gate
- Resultaat in dashboard, niet in trade-pipeline

### Aanbeveling

A eerst implementeren — concreet, testbaar, deterministisch. B later als
losse audit-laag erbovenop. B is niet kritiek-pad voor alpha-vs-BH doel.

### Logging

Elk trade-record krijgt extra veld:

```python
"bear_check": {
    "score": 0.42,
    "components": {
        "mtf_opposition": 0.3,
        "funding_extreme": 0.1,
        "recent_lower_highs": 0.0,
        "bb_extreme_against": 0.2,
        "loser_correlation": 0.4
    }
}
```

Maakt post-hoc analyse mogelijk: zijn trades met hoge bear-check scores
inderdaad slechter? Zo niet → bear-check werkt niet, terug naar tekentafel.

### A/B test

- Trades met / zonder bear-check op holdout
- Metric: Calmar, win rate, max DD
- Verwachting: lager trade-volume, hogere win rate, lagere max DD

### Effort en default state

- **Effort**: 1 dag voor A
- **Default state**: `enabled: false`. B is los project, niet in dit plan.

---

## Doorlooptijd en volgorde

| Fase | Effort | Afhankelijk van | Status |
|------|--------|----------------|--------|
| 1 — Benchmark + metrics | 0.5d | — | TODO |
| 2 — Look-ahead audit | 0.5–1d | — (parallel met 1 kan) | TODO |
| 3a+3b — Funding analyse | 1.5d | 1, 2 | TODO |
| 4 — Regime-multiplier calibratie | 1.5d | 1, 2 | TODO |
| 5 — Continue risk score | 2.5d | 1, 2, 3 | TODO |
| 6 — Bear-check (optie A) | 1d | 5 | TODO |

**Totaal**: ~8 dagen werk, gefaseerd. Na elke fase tussentijds A/B-resultaat
beschikbaar.

### Kritiek pad

Fase 1 → Fase 2 → (Fase 3, Fase 4 parallel) → Fase 5 → Fase 6.

Fase 1 en 2 kunnen parallel als verschillende developer-sessies, mits
geen merge-conflicts op `backtester.py`.

---

## Beslismomenten (gates)

Op deze momenten expliciet stoppen en beslissen of door te gaan:

1. **Na Fase 2**: lookahead gevonden? Eerst fixen, dan pas Fase 3+. Anders
   bouw je op losse grond.
2. **Na Fase 3b**: funding correlatie significant + materieel + robuust? Zo nee,
   funding skippen in Fase 5.
3. **Na Fase 4 holdout**: regime-multiplier winner buiten 1σ van train?
   Niet activeren, multipliers ongewijzigd.
4. **Na Fase 5 A/B**: continue score slechtere Calmar dan binaire gates?
   Niet activeren, fallback behouden.
5. **Na Fase 6 A/B**: bear-check verlaagt Calmar of niet meetbaar effect?
   Niet activeren, terug naar tekentafel.

---

## Referenties

- Liu, Y., Tsyvinski, A., & Wu, X. (2021). *Common Risk Factors in
  Cryptocurrency*. Journal of Finance.
- Bailey, D. H., & López de Prado, M. (2014). *The Deflated Sharpe Ratio*.
  Journal of Portfolio Management.
- López de Prado, M. (2018). *Advances in Financial Machine Learning*. Wiley.
  Hoofdstukken over backtest overfitting en walk-forward.
- BloFin API docs: `GET /api/v1/market/funding-rate`

---

## Wijzigingshistorie

- **v1** (initiële analyse, kramerica trading-bot): te zelfverzekerde claims
  over `signal_sign`, LightGBM-upgrade, asymmetrische multipliers
- **v2** (correctie na review): hypothese-status erkend, methodologische
  punten toegevoegd (lookahead audit, multiple-testing, risk-adjusted metrics)
- **dit plan**: v2-prioriteiten geadapteerd voor Crypto Trader v2.7, met
  expliciete beslismomenten en fallback-discipline
