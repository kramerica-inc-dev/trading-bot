# Quick Start Guide - Blofin Trading Bot

## 1. Get API Credentials

1. Login to Blofin: https://blofin.com
2. Navigate to **Account → API Management**
3. Click **Create API Key**
4. Set permissions:
   - ✅ **READ** (required)
   - ✅ **TRADE** (required)
   - ❌ **TRANSFER** (leave disabled!)
5. Save these safely:
   - API Key
   - API Secret
   - Passphrase (you create this)

## 2. Configure Bot

```bash
cd blofin-trader
cp config.example.json config.json
nano config.json  # or your preferred editor
```

Minimal config:
```json
{
  "api_key": "paste_your_key_here",
  "api_secret": "paste_your_secret_here",
  "passphrase": "your_passphrase",
  
  "dry_run": true,
  "trading_pair": "BTC-USDT",
  "strategy_name": "rsi",
  
  "risk_per_trade_pct": 10.0
}
```

## 3. Install Dependencies

```bash
pip3 install requests numpy pandas
```

Or with conda:
```bash
conda install requests numpy pandas
```

## 4. Test Connection

```bash
cd scripts
python3 blofin_api.py
```

Expected output:
```
BTC-USDT Ticker: {...}
Futures Balance: {...}
```

If you see errors, check your credentials.

## 5. Dry Run Test

**Always start with dry run mode!**

```bash
python3 trading_bot.py --config ../config.json --once
```

This runs ONE iteration without placing real orders. Check output:
- ✅ Balance fetched
- ✅ Current price shown
- ✅ Strategy signal generated
- ✅ "DRY RUN MODE" message

## 6. Monitor for 1 Hour

Let it run in dry mode for ~1 hour:

```bash
python3 trading_bot.py --interval 60
```

Watch for:
- No authentication errors
- Reasonable signals (not trading every cycle)
- Proper logging

Stop with `Ctrl+C`.

## 7. Go Live (Optional)

⚠️ **Only after successful dry run testing!**

Edit `config.json`:
```json
{
  "dry_run": false
}
```

Start bot:
```bash
python3 trading_bot.py --interval 60
```

**Monitor closely for first 2-4 hours!**

## Common Issues

### "Signature verification failed"
- Double-check API credentials
- Ensure no extra spaces in config.json
- Verify passphrase matches

### "Insufficient balance"
- Check Blofin account has funds
- Funds must be in **Futures** account
- Use Transfer feature if in Funding account

### "Rate limit reached"
- Bot is making too many requests
- Increase `--interval` (default 60s is safe)

### Bot not trading
- Check `min_confidence` threshold (default 0.6)
- Strategy might not detect signals yet
- Market may be in neutral zone (expected!)

## Risk Management Tips

Start **conservative**:
- Risk per trade: **5-10%** of balance
- Stop-loss: **2-3%**
- Use **RSI strategy** first (more conservative)

After 1 week of stable operation, consider:
- Increasing risk per trade to 10-15%
- Testing other strategies (trend, grid)
- Trading multiple pairs

## Monitoring Strategy

**First 24 hours:** Check every 2-3 hours
**After 1 week:** Check daily
**After 1 month:** Weekly reviews OK

Always monitor logs:
```bash
tail -f ../memory/trading-log.jsonl | jq
```

## Questions?

- Check logs: `memory/trading-log.jsonl`
- Review SKILL.md for full docs
- Test with `--once` flag first
- Ask Claw for help! 🦀
