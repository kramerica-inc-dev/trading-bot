#!/usr/bin/env bash
# Plan E multi-instance deploy to the Proxmox LXC.
#
# Run from the project root on the laptop (NOT on Proxmox).
#   ./deploy/deploy_multi.sh              # deploy code + enable base + c
#   ./deploy/deploy_multi.sh --full       # also enable g, cg, i, 12h, 48h, trail
#   ./deploy/deploy_multi.sh --no-enable  # deploy only, don't touch services
#
# Assumes SSH: root@trading-bot (Tailscale host)
# Installs to:  /opt/trading-bot/
# User:         botuser (owns /opt/trading-bot)
#
# What it does:
#   1. Backs up scripts/plan_e_runner.py and scripts/dashboard_api.py on LXC.
#   2. rsyncs scripts/ , configs/ , deploy/plan-e@.service.
#   3. Installs systemd templated unit at /etc/systemd/system/plan-e@.service.
#   4. Stops legacy plan-e-runner.service (if active) — data was only 1 day
#      old with no rebalance, so this is a safe migration.
#   5. Enables + starts plan-e@base and plan-e@c. Other instances only with --full.

set -euo pipefail

HOST="${HOST:-root@trading-bot}"
REMOTE_DIR="${REMOTE_DIR:-/opt/trading-bot}"
STAMP="$(date +%Y%m%d-%H%M%S)"

MODE="default"
if [[ "${1:-}" == "--full" ]]; then MODE="full"; fi
if [[ "${1:-}" == "--no-enable" ]]; then MODE="no-enable"; fi

echo "→ Target: $HOST:$REMOTE_DIR  (mode: $MODE)"

# 1. Backup critical files on LXC
echo "→ Backing up remote files on LXC…"
ssh "$HOST" bash <<EOF
set -euo pipefail
cd "$REMOTE_DIR"
for f in scripts/plan_e_runner.py scripts/dashboard_api.py scripts/dashboard.html; do
  if [ -f "\$f" ]; then cp "\$f" "\$f.backup-$STAMP"; fi
done
EOF

# 2. rsync code + configs + systemd unit
echo "→ Syncing scripts/ and configs/…"
rsync -avz --delete-excluded \
  --exclude='__pycache__/' --exclude='*.pyc' \
  scripts/plan_e_runner.py \
  scripts/dashboard_api.py \
  scripts/dashboard.html \
  "$HOST:$REMOTE_DIR/scripts/"

ssh "$HOST" "mkdir -p $REMOTE_DIR/configs"
rsync -avz configs/ "$HOST:$REMOTE_DIR/configs/"

rsync -avz deploy/plan-e@.service "$HOST:/etc/systemd/system/plan-e@.service"

# Make sure botuser owns everything it writes
ssh "$HOST" "chown -R botuser:botuser $REMOTE_DIR/scripts $REMOTE_DIR/configs"

# 3. Install + reload systemd
echo "→ Reloading systemd…"
ssh "$HOST" "systemctl daemon-reload"

# 3b. Dashboard service needs a restart to pick up new dashboard_api.py code.
# (The served dashboard.html is re-read per request, but the HTTP handler
# is only loaded once at process start.)
echo "→ Restarting trading-dashboard to load new API code…"
ssh "$HOST" bash <<'EOF'
set -e
if systemctl list-unit-files | grep -q '^trading-dashboard.service'; then
  systemctl restart trading-dashboard
fi
EOF

if [[ "$MODE" == "no-enable" ]]; then
  echo "✓ Code deployed. Services NOT modified (--no-enable)."
  exit 0
fi

# 4. Stop legacy unit if present
echo "→ Stopping legacy plan-e-runner.service if active…"
ssh "$HOST" bash <<'EOF'
set -e
if systemctl list-unit-files | grep -q '^plan-e-runner.service'; then
  systemctl disable --now plan-e-runner.service || true
fi
EOF

# 5. Enable + start instances
BASE_INSTANCES=(base c)
FULL_INSTANCES=(g cg i 12h 48h trail size17 maker50)

enable_instance() {
  local inst="$1"
  local cfg="$REMOTE_DIR/configs/plan-e-${inst}.json"
  echo "  ↳ plan-e@${inst}"
  ssh "$HOST" "test -f $cfg" || { echo "     missing config: $cfg" >&2; return 1; }
  ssh "$HOST" "systemctl enable --now plan-e@${inst}.service"
}

echo "→ Enabling base instances…"
for inst in "${BASE_INSTANCES[@]}"; do enable_instance "$inst"; done

if [[ "$MODE" == "full" ]]; then
  echo "→ Enabling full variant set (--full)…"
  for inst in "${FULL_INSTANCES[@]}"; do enable_instance "$inst"; done
fi

echo
echo "✓ Deploy complete. Check status:"
echo "  ssh $HOST 'systemctl status plan-e@base plan-e@c'"
echo "  ssh $HOST 'journalctl -u plan-e@base -f'"
