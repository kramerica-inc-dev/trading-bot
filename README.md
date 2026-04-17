# Blofin Trading Bot

**Version:** 2.4
**Entrypoint:** `scripts/trading_bot.py`
**Exchange:** BloFin (Coinbase adapter exists but untested)

---

## Overview

Regime-aware multi-timeframe trading bot for BloFin perpetual futures. Uses a multi-indicator confluence strategy with regime detection (bull trend, bear trend, range, chop) to generate trade signals with dynamic position sizing based on stop-loss distance.

### Key features

- **Regime-aware strategy** with 4 market regimes and per-regime risk multipliers
- **SL-based position sizing** via `risk_utils.calculate_risk_position_size()`
- **Server-side TP/SL** via BloFin order-tpsl API (mandatory by default)
- **Fail-closed startup reconciliation** — mismatches between exchange and local state abort the bot
- **Circuit breaker** — daily loss limit, consecutive error/loss limits, cooldown
- **Preflight validation** (`--preflight`) — checks config, credentials, connectivity, state
- **Atomic state persistence** — tmp+rename pattern for crash safety
- **WebSocket support** — optional market data and private order streams
- **Backtesting** — with SL-based sizing aligned to live, per-regime metrics, rejection counters

---

## Project structure

```
scripts/
  trading_bot.py          # Main bot (the only production entrypoint)
  advanced_strategy.py    # Regime-aware multi-indicator strategy
  config_utils.py         # Config loading, normalization, validation
  risk_utils.py           # SL-based position sizing
  blofin_api.py           # BloFin REST API client
  blofin_adapter.py       # BloFin exchange adapter
  exchange_adapter.py     # Abstract exchange interface
  coinbase_adapter.py     # Coinbase adapter (unused)
  market_data_stream.py   # WebSocket market data
  private_order_stream.py # WebSocket order updates
  live_profile_manager.py # Parameter profile management (disabled by default)

backtest/
  backtester.py           # Backtesting engine
  optimizer.py            # Parameter grid search
  run_backtest.py         # CLI entry point for backtests
  run_baseline.py         # Reproducible baseline run
  data/                   # Historical candle CSVs
  results/                # Backtest output

deployment/
  trading-bot.service     # systemd service file
  03-deploy-bot.sh        # Deploy script for Proxmox container

tests/
  test_config_and_risk.py       # Config validation + risk sizing tests
  test_bot_integration.py       # Bot lifecycle integration tests
  test_reconciliation.py        # Startup reconciliation tests
  test_execution_protection.py  # TP/SL enforcement tests
  test_strategy_diagnostics.py  # Rejection counter + sizing alignment tests
```

---

## Quick start

### 1. Config

Copy `config.example.json` to `config.json` and fill in credentials:

```bash
cp config.example.json config.json
```

### 2. Preflight

```bash
python3 scripts/trading_bot.py --config config.json --preflight
```

### 3. Dry run

Start with `"dry_run": true` in config:

```bash
python3 scripts/trading_bot.py --config config.json
```

### 4. Backtest

```bash
python3 -m backtest.run_baseline
```

---

## Config

See `config.example.json` for the full schema. Key sections:

| Section | Purpose |
|---------|---------|
| `blofin` | API credentials |
| `risk` | Position sizing, leverage, contract size |
| `trading` | Allow long/short, max positions |
| `strategy` | Strategy parameters |
| `protection` | Server-side TP/SL settings |
| `circuit_breaker` | Loss/error limits |
| `execution` | Order reconciliation, WebSocket |
| `market_data` | WebSocket, staleness thresholds |
| `parameter_selector` | Live parameter profile (disabled by default) |

Safe defaults are applied for all sections not explicitly set.

---

## Container deployment

Target: Proxmox LXC container (configure your container IP)

```bash
# Deploy all scripts + service
bash deployment/03-deploy-bot.sh

# On the container:
python3 /opt/trading-bot/scripts/trading_bot.py --config /opt/trading-bot/config.json --preflight
systemctl enable trading-bot
systemctl start trading-bot
journalctl -u trading-bot -f
```

---

## Tests

```bash
python3 -m unittest discover -s tests -v
```

43 tests covering config validation, risk sizing, strategy behavior, reconciliation, execution protection, and backtester alignment.

---

## CLI flags

| Flag | Purpose |
|------|---------|
| `--config PATH` | Config file path (required) |
| `--preflight` | Run validation checks and exit |
| `--force-reconcile` | Bypass reconciliation errors (emergency use only) |
