---
name: blofin-trader
description: Automated cryptocurrency trading bot for Blofin exchange. Use when Michiel wants to set up, monitor, configure, or manage automated trading on Blofin. Supports multiple strategies (RSI mean reversion, EMA trend following, grid trading), risk management, position monitoring, and trade logging. Handles API authentication, order execution, and market analysis.
---

# Blofin Trading Bot

Automated trading bot for Blofin cryptocurrency exchange with multiple strategies and risk management.

## Features

- **Multiple Trading Strategies**
  - RSI Mean Reversion (buy oversold, sell overbought)
  - EMA Trend Following (crossover signals)
  - Grid Trading (profit from volatility)

- **Risk Management**
  - Position sizing based on account balance
  - Stop-loss and take-profit levels
  - Configurable risk per trade

- **Monitoring & Logging**
  - Real-time trade logging to JSONL
  - Console output with color coding
  - Position tracking

## Quick Start

### 1. Setup Configuration

Copy the example config and fill in your Blofin API credentials:

```bash
cd blofin-trader
cp config.example.json config.json
```

Edit `config.json` with your credentials:
```json
{
  "api_key": "YOUR_API_KEY",
  "api_secret": "YOUR_SECRET",
  "passphrase": "YOUR_PASSPHRASE",
  "dry_run": true,
  "strategy_name": "rsi"
}
```

⚠️ **Security**: Add `config.json` to `.gitignore`! Never commit credentials.

### 2. Install Dependencies

```bash
pip install requests numpy pandas
```

### 3. Test Connection

```bash
cd scripts
python3 blofin_api.py
```

### 4. Run Bot

**Dry run mode (recommended first!):**
```bash
python3 trading_bot.py --config ../config.json
```

**Single iteration test:**
```bash
python3 trading_bot.py --once
```

**Live trading** (after testing):
Edit `config.json` and set `"dry_run": false`, then:
```bash
python3 trading_bot.py --interval 60
```

## Configuration

### API Credentials

Get your Blofin API keys from: https://blofin.com/account/apis

**Required permissions:**
- ✅ READ (view balances)
- ✅ TRADE (place/cancel orders)
- ❌ TRANSFER (NOT needed, leave disabled for security)

### Strategy Selection

Set `strategy_name` in config.json:

- **`"rsi"`** - RSI Mean Reversion
  - Best for: Ranging/sideways markets
  - Parameters: `rsi_period`, `rsi_oversold`, `rsi_overbought`
  
- **`"trend"`** - EMA Trend Following
  - Best for: Trending markets
  - Parameters: `fast_ema`, `slow_ema`
  
- **`"grid"`** - Grid Trading
  - Best for: Volatile markets
  - Parameters: `grid_levels`, `grid_spacing_pct`

### Risk Profiles

Pre-configured risk levels in `config.example.json`:

- **Conservative**: 5% per trade, 2% stop-loss
- **Moderate**: 10% per trade, 3% stop-loss  
- **Aggressive**: 25% per trade, 5% stop-loss

### Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| `trading_pair` | Asset pair to trade | `"BTC-USDT"` |
| `timeframe` | Candle timeframe | `"5m"` |
| `risk_per_trade_pct` | % of balance per trade | `10.0` |
| `min_confidence` | Minimum signal confidence | `0.6` |
| `dry_run` | Test mode without real orders | `true` |
| `demo_mode` | Use Blofin demo API | `false` |

## Monitoring

### Logs

Trade logs are written to `memory/trading-log.jsonl`:

```bash
tail -f memory/trading-log.jsonl | jq
```

### Heartbeat Integration

Add to `HEARTBEAT.md` for periodic checks:

```markdown
## Trading Bot Check

Check trading bot status every 2-4 hours:
- Read last 10 lines from `blofin-trader/memory/trading-log.jsonl`
- Look for errors or unusual activity
- Check balance if concerned
- Alert Michiel if issues found
```

### Manual Status Check

```python
from scripts.blofin_api import BlofinAPI

api = BlofinAPI(api_key, api_secret, passphrase)
balance = api.get_balance("futures", "USDT")
print(balance)
```

## Safety Features

1. **Dry Run Mode** - Test without real trades (set `"dry_run": true`)
2. **Demo Trading** - Use Blofin's demo environment (set `"demo_mode": true`)
3. **Position Limits** - Minimum order size enforced (0.1 contracts)
4. **Stop-Loss** - Automatic exit on losses
5. **Rate Limiting** - Respects Blofin API limits (30 req/10s)

## Troubleshooting

### "Signature verification failed"

- Check API credentials in config.json
- Ensure passphrase matches API key
- Verify system time is synchronized

### "Insufficient balance"

- Check balance: `api.get_balance("futures", "USDT")`
- Transfer funds to futures account if needed
- Reduce `risk_per_trade_pct`

### "Order size too small"

- Minimum: 0.1 contracts (0.0001 BTC for BTC-USDT)
- Increase balance or reduce risk percentage

### Bot not trading

- Check `min_confidence` threshold
- Review strategy parameters (RSI levels, EMA periods)
- Market might be in neutral zone

## File Structure

```
blofin-trader/
├── SKILL.md                     # This file
├── SETUP-INSTRUCTIES.md         # Dutch setup guide
├── config.example.json          # Config template
├── config.json                  # Your config (gitignored)
├── scripts/
│   ├── blofin_api.py           # API client
│   ├── trading_strategy.py     # Trading strategies
│   └── trading_bot.py          # Main bot orchestrator
├── references/
│   └── blofin-api-docs.md      # API reference
└── memory/
    └── trading-log.jsonl       # Trade logs
```

## API Reference

See `references/blofin-api-docs.md` for full Blofin API documentation.

Key endpoints used:
- `/api/v1/market/tickers` - Get current price
- `/api/v1/market/candles` - Get historical candles
- `/api/v1/asset/balances` - Check balance
- `/api/v1/trade/order` - Place orders

## Development

### Adding New Strategies

1. Create strategy class in `scripts/trading_strategy.py`:
```python
class MyStrategy(TradingStrategy):
    def analyze(self, candles, current_price) -> Signal:
        # Your logic here
        return Signal(action="buy", confidence=0.8, reason="...")
```

2. Register in factory:
```python
strategies = {
    "mystrat": MyStrategy
}
```

3. Update config with strategy parameters

### Testing Strategies

```python
from scripts.trading_strategy import create_strategy

strategy = create_strategy("rsi", {"rsi_period": 14})
signal = strategy.analyze(candles, current_price)
print(signal)
```

## Disclaimers

⚠️ **Trading involves risk**
- Start with small amounts
- Use dry run mode extensively
- Monitor the first 24-48 hours closely
- Crypto markets are volatile 24/7
- Past performance doesn't guarantee future results

⚠️ **Security**
- Never share API keys
- Use API keys with minimal permissions (no withdrawal)
- Store config.json securely
- Consider IP whitelisting on Blofin

## Support

- Blofin Docs: https://docs.blofin.com
- Check logs: `memory/trading-log.jsonl`
- Test in dry run mode first
- Start with conservative risk settings
