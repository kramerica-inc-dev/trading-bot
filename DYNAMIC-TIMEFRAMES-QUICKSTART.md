# Dynamic Timeframes + Per-TF Calibration — Quickstart

*Versie 2.7 — april 2026*

Dit document beschrijft hoe je de regime-gebaseerde timeframe-selectie (stap 3) en
per-timeframe parameter-kalibratie (stap 5) activeert. Beide features zijn
**uitgeschakeld by default**; de bot gedraagt zich identiek aan v2.6 totdat je
ze expliciet aanzet.

## Waarom

De bot draaide op een vast 5m-timeframe, ongeacht marktconditie. Twee problemen:

1. **In trending markets is 5m te ruiserig** — de bot pakt kleine tegenbewegingen
   op als signalen, opent dan een positie en raakt geliquideerd op de volgende
   pullback.
2. **In chop is elke timeframe slecht**, maar 5m is het slechtst — te veel
   whipsaws, te veel commissie-drain.

De oplossing is tweeledig:

- **Stap 3 (dynamic timeframes)** kiest het timeframe op basis van het
  gedetecteerde marktregime: trading timeframes passen bij de volatiliteits- en
  trendstructuur die het regime representeert.
- **Stap 5 (per-TF calibration)** vindt via walk-forward backtesting welke
  parameter-waarden op elk timeframe het beste werken, zodat bijvoorbeeld
  `rsi_period=14` (geijkt op 5m) niet blind wordt doorgebruikt op 1h-candles.

De twee features werken onafhankelijk. Je kunt stap 3 aanzetten zonder stap 5,
en andersom.

## Architectuur in één blik

```
Marktdata → detect_regime() → RegimeTimeframeResolver
                                       │
                                       ├─ active_timeframe  ──┐
                                       └─ active_interval     │
                                                              ▼
                                                    fetch_candles(tf)
                                                              │
                                                              ▼
                                            strategy.set_active_timeframe(tf)
                                                              │
                                                              ▼
                                                    strategy.analyze()
                                                         │
                                                         ├─ _apply_timeframe_profile()  ← TF params
                                                         └─ _apply_live_profile()       ← regime overrides
```

Precedence bij parameter-conflicten: **regime > timeframe > base**.

## Stap 3 activeren — dynamische timeframes

### 1. Config aanpassen

In `config.json`:

```json
"regime_timeframes": {
  "enabled": true,
  "confirmation_bars": 3,
  "fallback_regime": "unclear",
  "timeframes": {
    "bull_trend": "15m",
    "bear_trend": "15m",
    "range": "5m",
    "chop": "1h",
    "unclear": "1h"
  },
  "check_intervals": {
    "bull_trend": 300,
    "bear_trend": 300,
    "range": 60,
    "chop": 900,
    "unclear": 900
  },
  "urgency": {
    "chop": 3,
    "bear_trend": 2,
    "bull_trend": 2,
    "range": 1,
    "unclear": 0
  }
}
```

### 2. Hoe de hysteresis werkt

Elk regime heeft een urgentie (0-3). Als het gedetecteerde regime een
**hogere of gelijke urgentie** heeft dan het actieve regime, wisselt de bot
direct. Als het een **lagere urgentie** heeft, wordt gewacht tot
`confirmation_bars` opeenvolgende detecties hetzelfde regime aangeven.

Dit voorkomt whipsaw richting agressievere timeframes (bull_trend → range) en
houdt tegelijk de defensieve switch naar chop razendsnel.

### 3. Positiegedrag bij regime-switch

Open posities worden **niet aangeraakt**. Elke positie heeft een
`opened_on_timeframe` veld, en `_check_time_based_exits` rekent bars op basis
daarvan. Nieuwe signalen gebruiken het huidig actieve timeframe.

### 4. Verifiëren

Bij het opstarten zie je in de log:

```
🚀 Bot started! Timeframe=5m, interval=60s (dynamic)
```

Bij een regime-switch:

```
🔄 Timeframe switch: -> 15m (regime=bull_trend, interval=300s, reason=urgency_up (unclear->bull_trend, 0->2))
```

## Stap 5 activeren — per-TF parameter-kalibratie

### 1. Kalibratie draaien

Dit is een eenmalige (of periodieke, bv. wekelijkse) offline-stap.

```bash
cd /path/to/blofin-trader
python -m backtest.calibrate_per_timeframe --days 90
```

Het script:

1. Fetcht 90 dagen 5m-candles via BloFin.
2. Resamplet die naar 15m en 1h.
3. Draait walk-forward optimization (3 splits, 70/30 train/test) op elk
   timeframe over vijf TF-gevoelige parameters.
4. Schrijft het winnende profiel per TF naar `memory/timeframe_profiles.json`.

Tijd: ongeveer 25-40 minuten per timeframe op een standaard laptop. Met drie
timeframes dus ~1-2 uur in totaal.

CLI-opties:

```bash
# Minder data, sneller (ruwere kalibratie):
python -m backtest.calibrate_per_timeframe --days 30

# Alleen 15m kalibreren:
python -m backtest.calibrate_per_timeframe --timeframes 15m

# Dry run (laat output zien, schrijf niks weg):
python -m backtest.calibrate_per_timeframe --dry-run

# Forceer verse data (ignore cache):
python -m backtest.calibrate_per_timeframe --force-refresh

# Volledige sample in plaats van walk-forward (niet aanbevolen - overfitting):
python -m backtest.calibrate_per_timeframe --full-sample
```

### 2. Welke parameters worden gekalibreerd

Vijf parameters, gekozen op TF-gevoeligheid. De rest blijft op de
base-strategy config staan:

| Parameter                       | Waarom TF-gevoelig                                 |
|---------------------------------|----------------------------------------------------|
| `rsi_period`                    | RSI 14 = 70 min op 5m vs 14 uur op 1h             |
| `trend_strength_threshold`      | Relatieve price moves per bar zijn groter op HTF  |
| `efficiency_trend_threshold`    | Kaufman ER meet lineariteit, schaalt met bar-tijd |
| `min_confidence`                | Signaal-kwaliteit varieert per TF                 |
| `anchor_slope_threshold`        | EMA-slope afhankelijk van bar-duur                |

Macd, Bollinger Bands en volume-thresholds zijn **bewust** niet in de grid —
ze zijn relatief TF-invariant (BB 20 = 20 bars op elk TF, dat is intentioneel).

### 3. Activeer in `config.json`

```json
"timeframe_profiles": {
  "enabled": true,
  "path": "memory/timeframe_profiles.json"
}
```

### 4. Verifiëren

Bij het opstarten zie je:

```
📏 Loaded 3 calibrated timeframe profile(s) from timeframe_profiles.json
```

Als de bot wisselt van timeframe, wordt automatisch het bijbehorende profiel
toegepast. Dit gebeurt stil — geen extra log-regel.

## Aanbevolen rollout

1. **Dry-run eerst.** Zet `"dry_run": true` in `config.json`, activeer stap 3,
   en draai een week. Kijk naar de timeframe-switches in het log. Als de
   frequentie te hoog is, verhoog `confirmation_bars`. Als hij te traag
   reageert op trends, verlaag het.
2. **Voeg stap 5 toe in dry-run.** Draai de kalibratie, zet
   `timeframe_profiles.enabled=true`, herstart, draai nog een week. Vergelijk
   de dry-run-performance voor en na kalibratie.
3. **Live met verlaagd risico.** Zet `"dry_run": false` en halveer
   `risk.risk_per_trade_pct` voor de eerste 2-4 weken. Zo beperk je schade als
   een van de features zich anders gedraagt dan backtest voorspelde.
4. **Periodieke herijking.** Crypto-regimes verschuiven. Ik zou elke 4-8 weken
   `calibrate_per_timeframe.py` opnieuw draaien en het nieuwe profiel
   inladen. Er is geen auto-refresh — dit is bewust een handmatige stap zodat
   je controle houdt over wat er live draait.

## Features uitschakelen

Beide features zijn individueel uit te zetten zonder code te wijzigen:

```json
"regime_timeframes":  { "enabled": false },
"timeframe_profiles": { "enabled": false }
```

De bot valt dan terug op de statische `"timeframe": "5m"` uit de top-level
config en de hand-getunede strategy-parameters.

## Troubleshooting

**"No profiles loaded" warning bij startup**

Het JSON-bestand bestaat niet of is leeg. Draai
`calibrate_per_timeframe.py` eerst.

**Timeframe switches stoppen volledig na een paar cycles**

Check de log op "awaiting_confirmation"-messages. Als die lang blijven
hangen zonder te switchen, is `confirmation_bars` mogelijk te hoog voor de
huidige markt. Verlaag naar 2.

**Kalibratie rapporteert "no valid splits"**

Dat betekent dat op alle training-windows minder dan `min_trades` trades
worden gegenereerd. Opties: meer data (`--days 180`), minder strikte
trade-filter (`--min-trades 3`), of breder parameter-grid.

**Parameters veranderen ondanks `enabled: false`**

Dat kan niet in de bot zelf. Check of er een `regime_live_profile.json` in
`memory/` ligt die ook actief is (parameter_selector feature). Die werkt
onafhankelijk.

## Testen

```bash
python tests/test_regime_timeframe.py    # 16 tests — step 3
python tests/test_timeframe_profiles.py  # 13 tests — step 5
```

Beide suites moeten groen zijn voordat je live gaat.

## Verder lezen

- `scripts/regime_timeframe.py` — implementatie van de resolver + hysteresis
- `scripts/advanced_strategy.py` — zoek op `_timeframe_patched_base_profile`
  voor het precedence-model
- `backtest/calibrate_per_timeframe.py` — het kalibratie-script zelf,
  inclusief parameter-grids per TF
