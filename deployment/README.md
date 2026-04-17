# Proxmox LXC Deployment Guide

Deploy the Blofin Trading Bot to a Proxmox LXC container for 24/7 operation.

## Prerequisites

- Proxmox VE server (SSH access)
- Storage for LXC containers (usually `local-lvm`)
- Network bridge configured (usually `vmbr0`)

## Quick Start

### Step 1: On Your MacBook

Prepare the deployment files:

```bash
cd ~/.openclaw/workspace/blofin-trader
chmod +x deployment/*.sh
```

Copy deployment folder to Proxmox:

```bash
scp -r deployment root@YOUR_PROXMOX_IP:/tmp/bot-deploy
```

### Step 2: On Proxmox (via SSH)

```bash
ssh root@YOUR_PROXMOX_IP
cd /tmp/bot-deploy
```

**Create container:**
```bash
./01-create-container.sh
```

This creates an LXC container (ID 300 by default) with:
- Ubuntu 22.04
- 512MB RAM
- 4GB disk
- Auto-start on boot

**Bootstrap container:**
```bash
# Push bootstrap script into container
pct push 300 02-bootstrap-container.sh /root/bootstrap.sh

# Run it
pct exec 300 -- bash /root/bootstrap.sh
```

This installs:
- Python 3 + pip
- Required packages (requests, numpy, pandas)
- Bot user account

### Step 3: Deploy Bot Code

**From your MacBook**, copy config.json to Proxmox first:

```bash
# On MacBook
scp config.json root@YOUR_PROXMOX_IP:/tmp/bot-deploy/config.json
```

**On Proxmox**, deploy everything:

```bash
./03-deploy-bot.sh
```

This copies:
- Bot scripts to `/opt/trading-bot/`
- Config file (with credentials)
- Systemd service

### Step 4: Test & Start

**Test manually first:**

```bash
pct enter 300
cd /opt/trading-bot
python3 scripts/trading_bot.py --once
```

Check output:
- ✅ Balance fetched?
- ✅ Prices shown?
- ✅ Strategy signals?
- ✅ "DRY RUN" message?

**If all good, enable service:**

```bash
systemctl enable trading-bot
systemctl start trading-bot
```

**Monitor logs:**

```bash
# Real-time systemd logs
journalctl -u trading-bot -f

# Bot's own logs
tail -f /opt/trading-bot/memory/trading-log.jsonl
```

## Configuration

### Container Settings

Edit `01-create-container.sh` before running:

```bash
CT_ID=300              # Change if ID taken
CT_PASSWORD="..."      # Set strong password
CT_STORAGE="local-lvm" # Your storage name
CT_MEMORY=512          # RAM in MB
CT_DISK=4              # Disk in GB
```

### Bot Settings

The bot runs with these systemd settings:
- Auto-restart on crash
- 60 second check interval
- Runs as `botuser` (not root)
- Logs to journald + JSONL file

To change interval, edit `deployment/trading-bot.service`:
```ini
ExecStart=/usr/bin/python3 /opt/trading-bot/scripts/trading_bot.py --interval 120
```

Then redeploy and restart.

## Management Commands

### Container Management

```bash
# Start/stop container
pct start 300
pct stop 300
pct restart 300

# Enter container
pct enter 300

# Check status
pct status 300

# View resource usage
pct exec 300 -- htop
```

### Bot Service Management

```bash
# Inside container or via pct exec 300 --
systemctl status trading-bot
systemctl start trading-bot
systemctl stop trading-bot
systemctl restart trading-bot

# View logs
journalctl -u trading-bot -n 100        # Last 100 lines
journalctl -u trading-bot --since today # Today's logs
journalctl -u trading-bot -f            # Follow
```

### Update Bot Code

When you update the bot scripts:

```bash
# On MacBook: copy new code to Proxmox
scp -r scripts root@YOUR_PROXMOX_IP:/tmp/bot-deploy/

# On Proxmox: redeploy
cd /tmp/bot-deploy
./03-deploy-bot.sh

# Restart service
pct exec 300 -- systemctl restart trading-bot
```

### Update Config

```bash
# On MacBook
scp config.json root@YOUR_PROXMOX_IP:/tmp/config.json

# On Proxmox
pct push 300 /tmp/config.json /opt/trading-bot/config.json
pct exec 300 -- chown botuser:botuser /opt/trading-bot/config.json
pct exec 300 -- chmod 600 /opt/trading-bot/config.json
pct exec 300 -- systemctl restart trading-bot
```

## Monitoring

### Check Bot Health

```bash
# Quick status
pct exec 300 -- systemctl is-active trading-bot

# Recent activity
pct exec 300 -- tail -20 /opt/trading-bot/memory/trading-log.jsonl | jq
```

### Automated Monitoring

Add to OpenClaw HEARTBEAT.md:

```markdown
## Trading Bot Health Check

Every 4 hours, check:
- Container running: `pct status 300`
- Service active: `pct exec 300 -- systemctl is-active trading-bot`
- Recent errors: `pct exec 300 -- journalctl -u trading-bot --since "5 min ago" | grep ERROR`
- Last trade: Check trading-log.jsonl timestamp
```

### Alerts

Set up alerts for:
- Service crashes (restart count)
- API errors
- No trades for >24h (if unexpected)
- Balance drops significantly

## Backups

### Manual Backup

```bash
# Backup config (contains credentials!)
pct exec 300 -- cat /opt/trading-bot/config.json > config.backup.json

# Backup logs
pct exec 300 -- tar -czf /tmp/bot-logs.tar.gz /opt/trading-bot/memory/
pct pull 300 /tmp/bot-logs.tar.gz ./bot-logs-backup.tar.gz
```

### Proxmox Backup

Use Proxmox's built-in backup:

```bash
# Create backup
vzdump 300 --compress zstd --mode snapshot

# Restore from backup
pct restore 300 /var/lib/vz/dump/vzdump-lxc-300-*.tar.zst
```

## Security

- ✅ Unprivileged container
- ✅ Config file mode 600 (owner-only)
- ✅ Runs as non-root user (`botuser`)
- ✅ No TRANSFER API permission
- ✅ Isolated from Proxmox host

**Additional hardening:**

```bash
# IP whitelist on Blofin (optional)
# Get container IP:
pct exec 300 -- hostname -I

# Add to Blofin API key whitelist
```

## Troubleshooting

### Container won't start

```bash
pct start 300
# Check logs
journalctl -xe
```

### Bot crashes on start

```bash
# Check Python errors
pct exec 300 -- python3 /opt/trading-bot/scripts/trading_bot.py --once

# Check dependencies
pct exec 300 -- python3 -c "import requests, numpy, pandas"
```

### "Signature verification failed"

- Check API credentials in config.json
- Verify system time: `pct exec 300 -- date`
- Compare with: `date`
- If different, sync time in container

### Can't connect to Blofin

- Check internet connectivity: `pct exec 300 -- ping -c 3 blofin.com`
- Check DNS: `pct exec 300 -- nslookup blofin.com`
- Firewall rules blocking?

## Removal

To completely remove the trading bot:

```bash
# Stop and disable service
pct exec 300 -- systemctl stop trading-bot
pct exec 300 -- systemctl disable trading-bot

# Stop and destroy container
pct stop 300
pct destroy 300

# Clean up
rm -rf /tmp/bot-deploy
```

---

**Questions?** Check the main SKILL.md or ask Claw! 🦀
