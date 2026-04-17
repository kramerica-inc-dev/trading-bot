# Blofin Trading Bot

**Version:** 2.7
**Entrypoint:** `scripts/trading_bot.py`
**Exchange:** BloFin perpetual futures
**Python:** 3.10+

---

## Overview

Regime-aware multi-indicator confluence trading bot for BloFin perpetual futures. Detects market regimes (bull trend, bear trend, range, chop), dynamically selects timeframes, and generates trade signals using RSI, MACD, Bollinger Bands, and volume confirmation. Position sizing is based on stop-loss distance, not fixed percentages.

### Key features

- **Multi-indicator confluence** — requires 3+ indicators agreeing before entering a trade (RSI, MACD, Bollinger Bands, volume)
- **Regime detection** — 4 market regimes (bull trend, bear trend, range, chop) with per-regime risk multipliers
- **Dynamic timeframe selection** — automatically switches between 5m/15m/1h based on detected regime (optional, disabled by default)
- **Per-timeframe calibration** — walk-forward optimized parameters for each timeframe (optional, disabled by default)
- **SL-based position sizing** — size determined by stop-loss distance and risk budget, not fixed percentage
- **Server-side TP/SL** — mandatory exchange-side stop-loss and take-profit via BloFin order-tpsl API
- **Fail-closed reconciliation** — mismatches between exchange and local state abort the bot on startup
- **Circuit breaker** — daily loss limit, consecutive loss/error limits, cooldown period
- **Trailing stop** — optional breakeven and trailing stop with per-regime overrides
- **Atomic state persistence** — tmp+rename pattern for crash-safe state writes
- **WebSocket support** — optional market data and private order streams
- **Backtesting** — with SL-based sizing aligned to live, per-regime metrics, walk-forward optimization

---

## Quick start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure credentials

Copy the example config and add your BloFin API credentials:

```bash
cp config.example.json config.json
```

Edit `config.json` and set the `blofin` section:

```json
"blofin": {
  "api_key": "your_api_key",
  "api_secret": "your_api_secret",
  "passphrase": "your_passphrase"
}
```

Alternatively, use environment variables (these override `config.json`):

```bash
export BLOFIN_API_KEY="your_api_key"
export BLOFIN_API_SECRET="your_api_secret"
export BLOFIN_PASSPHRASE="your_passphrase"
```

> **Security:** `config.json` and `.env` are in `.gitignore` and must never be committed. See `.env.example` for the full list of supported environment variables.

### 3. Preflight check

```bash
python3 scripts/trading_bot.py --config config.json --preflight
```

Validates: config schema, credentials, exchange connectivity, balance access, state directory, protection config, exchange capabilities.

### 4. Dry run

Start with `"dry_run": true` (the default):

```bash
python3 scripts/trading_bot.py --config config.json
```

### 5. Go live

Set `"dry_run": false` in `config.json`, then restart.

---

## Project structure

```
scripts/
  trading_bot.py            # Main bot — the only production entrypoint
  advanced_strategy.py      # Regime-aware multi-indicator confluence strategy
  config_utils.py           # Config loading, normalization, validation
  risk_utils.py             # SL-based position sizing
  blofin_api.py             # BloFin REST API client
  blofin_adapter.py         # BloFin exchange adapter
  exchange_adapter.py       # Abstract exchange interface
  coinbase_adapter.py       # Coinbase adapter (unused)
  market_data_stream.py     # WebSocket market data
  private_order_stream.py   # WebSocket private order updates
  live_profile_manager.py   # Live parameter profile management
  regime_timeframe.py       # Dynamic timeframe resolver with hysteresis
  dashboard_api.py          # Dashboard REST API (Flask)
  dashboard.html            # Dashboard UI

backtest/
  backtester.py             # Backtesting engine with SL/TP per-candle checks
  optimizer.py              # Parameter grid search
  run_backtest.py           # CLI entry point for backtests
  run_baseline.py           # Reproducible baseline run
  analyze_regimes.py        # Regime analysis tool
  calibrate_per_timeframe.py  # Walk-forward per-TF parameter calibration
  data_collector.py         # BloFin API candle fetching with pagination
  data/                     # Historical candle CSVs (gitignored)
  results/                  # Backtest output

deployment/
  trading-bot.service       # systemd service file
  trading-dashboard.service # Dashboard service file
  01-create-container.sh    # Proxmox LXC container creation
  02-bootstrap-container.sh # Container bootstrap (Python, deps)
  03-deploy-bot.sh          # Deploy bot scripts to container
  04-deploy-dashboard.sh    # Deploy dashboard to container

tests/
  test_config_and_risk.py         # Config validation + risk sizing
  test_bot_integration.py         # Bot lifecycle integration
  test_reconciliation.py          # Startup reconciliation
  test_execution_protection.py    # TP/SL enforcement
  test_strategy_diagnostics.py    # Rejection counters + sizing alignment
  test_regime_timeframe.py        # Dynamic timeframe resolver
  test_timeframe_profiles.py      # Per-TF calibration profiles
  test_dynamic_tf_integration.py  # End-to-end dynamic TF integration
```

---

## Configuration

See `config.example.json` for the full schema with comments. All sections have safe defaults — only `blofin` credentials are required.

| Section | Purpose |
|---------|---------|
| `blofin` | API credentials and demo mode toggle |
| `risk` | Position sizing, leverage, margin mode, contract specs |
| `trading` | Allow long/short, max positions, position side mode |
| `strategy` | Indicator parameters, confidence thresholds, ATR multipliers |
| `protection` | Server-side TP/SL settings |
| `circuit_breaker` | Daily loss limit, consecutive loss/error limits, cooldown |
| `trailing_stop` | Breakeven and trailing stop with per-regime overrides |
| `execution` | Order reconciliation, pending order age, WebSocket |
| `market_data` | WebSocket toggle, candle cache size, staleness thresholds |
| `regime_timeframes` | Dynamic timeframe selection per regime (disabled by default) |
| `timeframe_profiles` | Per-TF calibrated parameters (disabled by default) |

### Environment variable overrides

The following environment variables override their `config.json` equivalents:

| Variable | Overrides |
|----------|-----------|
| `BLOFIN_API_KEY` | `blofin.api_key` |
| `BLOFIN_API_SECRET` | `blofin.api_secret` |
| `BLOFIN_PASSPHRASE` | `blofin.passphrase` |
| `BLOFIN_DEMO_MODE` | `blofin.demo_mode` |

This is handled by `config_utils._apply_exchange_env_overrides()`.

---

## Strategy

The `advanced` strategy uses a multi-indicator confluence approach with regime awareness.

### Indicators

| Indicator | Role | Key parameters |
|-----------|------|----------------|
| **RSI** | Momentum — overbought/oversold detection | `rsi_period`, `rsi_oversold`, `rsi_overbought` |
| **MACD** | Trend — strength and direction via histogram | `macd_fast`, `macd_slow`, `macd_signal` |
| **Bollinger Bands** | Volatility — price extremes relative to band | `bb_period`, `bb_std` |
| **Volume** | Confirmation — validates directional moves | Compared to 20-period average |

A trade signal requires `min_votes` indicators (default: 3 of 4) to agree on direction, plus `min_confidence` threshold.

### Regime detection

The strategy classifies market conditions into 4 regimes:

| Regime | Description | Risk multiplier |
|--------|-------------|-----------------|
| `bull_trend` | Strong uptrend with momentum | Normal |
| `bear_trend` | Strong downtrend with momentum | Normal |
| `range` | Sideways, mean-reverting | Reduced |
| `chop` | No clear direction, high noise | Heavily reduced or skip |

Each regime can have different risk multipliers, and the bot adjusts position sizing accordingly.

### Position sizing

Position size is calculated by `risk_utils.calculate_risk_position_size()`:

```
position_size = (balance * risk_pct) / stop_loss_distance
```

This ensures consistent risk per trade regardless of volatility. ATR-based stops widen in volatile markets (smaller position) and tighten in calm markets (larger position).

### Stop-loss and take-profit

- **Stop-loss:** `stop_loss_atr_mult` x ATR below/above entry (default: 2x)
- **Take-profit:** `take_profit_atr_mult` x ATR above/below entry (default: 3x)
- **Floors:** Minimum `stop_loss_floor_pct` (1%) and `take_profit_floor_pct` (1.5%) prevent stops that are too tight

---

## Dynamic timeframes

> Disabled by default. See [DYNAMIC-TIMEFRAMES-QUICKSTART.md](DYNAMIC-TIMEFRAMES-QUICKSTART.md) for the full guide.

Instead of a fixed 5m timeframe, the bot can automatically switch timeframes based on the detected market regime:

| Regime | Timeframe | Check interval |
|--------|-----------|----------------|
| `bull_trend` | 15m | 300s |
| `bear_trend` | 15m | 300s |
| `range` | 5m | 60s |
| `chop` | 1h | 900s |

Switches use a hysteresis mechanism based on urgency levels to prevent whipsawing:
- **Higher urgency** regime detected: switch immediately
- **Lower urgency** regime detected: wait for `confirmation_bars` consecutive detections

Enable in `config.json`:

```json
"regime_timeframes": { "enabled": true }
```

### Per-timeframe calibration

Indicator parameters tuned for 5m candles may not work on 15m or 1h. The calibration tool runs walk-forward optimization per timeframe:

```bash
python3 -m backtest.calibrate_per_timeframe --days 90
```

This generates `memory/timeframe_profiles.json` with optimized parameters for each timeframe. Enable in config:

```json
"timeframe_profiles": { "enabled": true, "path": "memory/timeframe_profiles.json" }
```

---

## Safety features

### Server-side TP/SL

Every position has exchange-side stop-loss and take-profit orders. If TP/SL placement fails and `require_server_side_tpsl` is `true` (default), the position is emergency-closed.

### Startup reconciliation

On startup, the bot compares exchange positions with local state. In live mode, it aborts on:

- Exchange position with no local metadata (orphan)
- Local position not found on exchange (stale state)
- Missing TP/SL when `require_server_side_tpsl` is `true`

Use `--force-reconcile` only in emergencies to bypass.

### Circuit breaker

When enabled, the bot stops trading on:

- Daily loss exceeding `daily_loss_limit_pct`
- Consecutive losses exceeding `max_consecutive_losses`
- Consecutive API errors exceeding `max_consecutive_errors`

After tripping, the bot enters a cooldown period before resuming.

### Trailing stop

Optional breakeven and trailing stop mechanism with per-regime overrides. Moves the stop-loss up as the trade moves in your favor.

---

## Backtesting

### Run a baseline

```bash
python3 -m backtest.run_baseline
```

Results are saved to `backtest/results/baseline_latest.json`.

### Run with custom parameters

```bash
python3 -m backtest.run_backtest --config config.json
```

### Parameter optimization

```bash
python3 -m backtest.optimizer
```

Grid search over strategy parameters with walk-forward splits.

### Collect historical data

```bash
python3 -m backtest.data_collector --pair BTC-USDT --timeframe 5m --days 90
```

Downloads candle data from BloFin and saves to `backtest/data/`.

---

## Deployment

Target: Proxmox LXC container with systemd service. See [PRODUCTION-SETUP.md](PRODUCTION-SETUP.md) for the full walkthrough.

### Deploy

```bash
bash deployment/03-deploy-bot.sh
```

This copies all script modules, creates required directories (`memory/`, `reconciliation/`), and installs the systemd service.

### On the container

```bash
# Preflight
python3 /opt/trading-bot/scripts/trading_bot.py --config /opt/trading-bot/config.json --preflight

# Start
systemctl enable trading-bot
systemctl start trading-bot
journalctl -u trading-bot -f
```

### Credential management on the container

Create `/opt/trading-bot/.env` with your credentials:

```bash
BLOFIN_API_KEY=your_key
BLOFIN_API_SECRET=your_secret
BLOFIN_PASSPHRASE=your_passphrase
```

The systemd service can be configured with `EnvironmentFile=/opt/trading-bot/.env` to inject these at runtime.

---

## Tests

```bash
python3 -m unittest discover -s tests -v
```

| Test file | Coverage |
|-----------|----------|
| `test_config_and_risk.py` | Config validation, normalization, risk sizing |
| `test_bot_integration.py` | Bot lifecycle, dry run, signal handling |
| `test_reconciliation.py` | Startup reconciliation logic |
| `test_execution_protection.py` | TP/SL enforcement |
| `test_strategy_diagnostics.py` | Rejection counters, sizing alignment |
| `test_regime_timeframe.py` | Dynamic timeframe resolver, hysteresis |
| `test_timeframe_profiles.py` | Per-TF calibration profile loading |
| `test_dynamic_tf_integration.py` | End-to-end dynamic TF integration |

---

## CLI flags

| Flag | Purpose |
|------|---------|
| `--config PATH` | Config file path (required) |
| `--preflight` | Run validation checks and exit |
| `--force-reconcile` | Bypass reconciliation errors (emergency use only) |

---

## Monitoring

Trade logs are written to `memory/trading-log.jsonl`:

```bash
tail -f memory/trading-log.jsonl | python3 -m json.tool
```

Log output includes:
- Signal action, confidence, and reason
- Active regime and regime confidence
- Indicator values (RSI, MACD histogram, volume ratio, ATR)
- Risk multiplier and quality score
- Active timeframe (when dynamic timeframes enabled)

---

## Troubleshooting

### "Signature verification failed"

- Verify API credentials in `config.json` or environment variables
- Ensure passphrase matches the API key
- Check system clock synchronization

### "Insufficient balance"

- Check balance on BloFin
- Transfer funds to the futures account
- Reduce `risk.risk_per_trade_pct`

### Bot not trading

- Check `strategy.min_confidence` — lower it if no signals are generated
- Review regime detection in logs — the bot skips trades in `chop` regime
- Verify the circuit breaker hasn't tripped (check daily loss, consecutive losses)

### WebSocket disconnects

The bot automatically reconnects. Periodic `websocket error: fin=1 opcode=8` messages are normal — BloFin closes idle connections.

---

## Further reading

| Document | Content |
|----------|---------|
| [ADVANCED-STRATEGY.md](ADVANCED-STRATEGY.md) | Strategy parameters, tuning profiles, indicator details |
| [DYNAMIC-TIMEFRAMES-QUICKSTART.md](DYNAMIC-TIMEFRAMES-QUICKSTART.md) | Dynamic timeframe selection and per-TF calibration guide |
| [PRODUCTION-SETUP.md](PRODUCTION-SETUP.md) | Container deployment walkthrough |
| [DASHBOARD-PLAN.md](DASHBOARD-PLAN.md) | Dashboard feature plan and API endpoints |
| [deployment/README.md](deployment/README.md) | Deployment scripts documentation |
| `config.example.json` | Full configuration schema with comments |
| `.env.example` | Supported environment variables |
