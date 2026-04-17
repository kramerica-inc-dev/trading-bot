# Production Setup Guide

**Version:** 2.4
**Target:** Proxmox LXC container (configure your container IP/hostname)

---

## Setup procedure

### 1. Create config

```bash
cp config.example.json config.json
```

Edit `config.json`:
- Set `blofin.api_key`, `blofin.api_secret`, `blofin.passphrase` to real credentials
- Or use environment variables: `BLOFIN_API_KEY`, `BLOFIN_API_SECRET`, `BLOFIN_PASSPHRASE`
- Keep `dry_run: true` initially

### 2. Run preflight

```bash
python3 scripts/trading_bot.py --config config.json --preflight
```

Checks: config valid, credentials present, exchange reachable, balance accessible, state dir writable, state files coherent, protection config, exchange capabilities.

### 3. Verify dry run

```bash
python3 scripts/trading_bot.py --config config.json
```

Let it run for a few cycles. Check logs for signal analysis and hold reasons.

### 4. Deploy to container

```bash
bash deployment/03-deploy-bot.sh
```

This copies all 12 script modules, creates required directories (`memory/`, `reconciliation/`), and installs the systemd service.

### 5. Preflight on container

```bash
ssh root@YOUR_CONTAINER_HOST
cd /opt/trading-bot
python3 scripts/trading_bot.py --config config.json --preflight
```

### 6. Start service

```bash
systemctl daemon-reload
systemctl enable trading-bot
systemctl start trading-bot
journalctl -u trading-bot -f
```

### 7. Go live

Change `dry_run` to `false` in `/opt/trading-bot/config.json`, then restart:

```bash
systemctl restart trading-bot
```

---

## Safety features

### Server-side TP/SL

Enabled and required by default. When a position is opened:
1. Entry order placed
2. On fill, TP/SL order placed via BloFin `order-tpsl` API
3. If TP/SL placement fails and `require_server_side_tpsl=true`, the position is emergency-closed

Config:
```json
"protection": {
  "use_server_side_tpsl": true,
  "require_server_side_tpsl": true
}
```

### Startup reconciliation

On startup, the bot compares exchange positions with local state. In live mode, it aborts on:
- Exchange position with no local metadata (orphan)
- Local position not found on exchange (stale state)
- Missing TP/SL when `require_server_side_tpsl=true`

Use `--force-reconcile` only in emergencies to bypass.

### Circuit breaker

Optional (disabled by default). When enabled, trips on:
- Daily loss exceeding `daily_loss_limit_pct`
- Consecutive losses exceeding `max_consecutive_losses`
- Consecutive API errors exceeding `max_consecutive_errors`

Config:
```json
"circuit_breaker": {
  "enabled": true,
  "daily_loss_limit_pct": 5.0,
  "max_consecutive_losses": 3,
  "max_consecutive_errors": 5,
  "cooldown_minutes": 30
}
```

---

## Service management

```bash
systemctl status trading-bot
systemctl start trading-bot
systemctl stop trading-bot
systemctl restart trading-bot
journalctl -u trading-bot -f
journalctl -u trading-bot -n 100
```

---

## File locations on container

| Path | Content |
|------|---------|
| `/opt/trading-bot/scripts/` | All bot modules |
| `/opt/trading-bot/config.json` | Production config (chmod 600) |
| `/opt/trading-bot/memory/` | State files, logs |
| `/opt/trading-bot/reconciliation/` | Reconciliation snapshots |
| `/etc/systemd/system/trading-bot.service` | Service file |

---

## Updating

```bash
# Stop bot
systemctl stop trading-bot

# Backup
cp -r /opt/trading-bot/scripts /opt/trading-bot/scripts.bak-$(date +%Y%m%d%H%M)

# Upload new scripts (from dev machine)
scp scripts/*.py root@YOUR_CONTAINER_HOST:/opt/trading-bot/scripts/

# Preflight
python3 /opt/trading-bot/scripts/trading_bot.py --config /opt/trading-bot/config.json --preflight

# Start
systemctl start trading-bot
```

---

## Emergency stop

```bash
systemctl stop trading-bot
```

Then close any open positions via the BloFin web interface or API.
