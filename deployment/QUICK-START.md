# 🚀 Quick Start - Proxmox Deployment

## TL;DR - Complete Setup in 5 Minutes

### 1. On MacBook - Prepare Files

```bash
cd ~/.openclaw/workspace/blofin-trader

# Make scripts executable
chmod +x deployment/*.sh

# Copy to Proxmox
scp -r deployment config.json root@YOUR_PROXMOX_IP:/tmp/bot-deploy
```

### 2. On Proxmox - Create & Setup Container

```bash
ssh root@YOUR_PROXMOX_IP
cd /tmp/bot-deploy

# Create container (ID 300)
./01-create-container.sh

# Bootstrap it (install Python, packages)
pct push 300 02-bootstrap-container.sh /root/bootstrap.sh
pct exec 300 -- bash /root/bootstrap.sh

# Deploy bot code
./03-deploy-bot.sh
```

### 3. Test & Start

```bash
# Test manually (DRY RUN)
pct enter 300
cd /opt/trading-bot
python3 scripts/trading_bot.py --once

# If OK, enable service
systemctl enable trading-bot
systemctl start trading-bot

# Watch logs
journalctl -u trading-bot -f
```

### 4. Go Live (After Testing)

```bash
# On MacBook: Edit config.json
nano config.json
# Change: "dry_run": false

# Upload new config
scp config.json root@YOUR_PROXMOX_IP:/tmp/config.json

# On Proxmox: Update & restart
pct push 300 /tmp/config.json /opt/trading-bot/config.json
pct exec 300 -- systemctl restart trading-bot
```

---

## Container Details

- **ID**: 300 (customizable in script)
- **Hostname**: trading-bot
- **RAM**: 512MB
- **Disk**: 4GB
- **OS**: Ubuntu 22.04
- **Auto-start**: Yes

## Management Commands

```bash
# View logs
journalctl -u trading-bot -f                    # Systemd logs
tail -f /opt/trading-bot/memory/trading-log.jsonl  # Bot logs (in container)

# Service control
systemctl status trading-bot
systemctl restart trading-bot

# Container control
pct status 300
pct restart 300
pct enter 300
```

## Update Bot

```bash
# On MacBook
scp -r scripts root@YOUR_PROXMOX_IP:/tmp/bot-deploy/

# On Proxmox
cd /tmp/bot-deploy
./03-deploy-bot.sh
pct exec 300 -- systemctl restart trading-bot
```

## Troubleshooting

**Bot won't start?**
```bash
pct exec 300 -- python3 /opt/trading-bot/scripts/trading_bot.py --once
```

**Check Python packages:**
```bash
pct exec 300 -- python3 -c "import requests, numpy, pandas; print('OK')"
```

**View errors:**
```bash
journalctl -u trading-bot --since "10 min ago" | grep -i error
```

---

Full documentation: See `deployment/README.md` 📚
