#!/bin/bash
# Deploy Dashboard to Trading Bot Container
# Run from Proxmox host or locally via SSH

set -e

CT_ID=${1:-25020}
BOT_DIR="/opt/trading-bot"
TARGET_IP="${DEPLOY_TARGET_IP:?Set DEPLOY_TARGET_IP to your container IP}"

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
BOT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "🖥️  Deploying Dashboard to container $CT_ID..."
echo ""

# Check if we're on the Proxmox host (pct available)
if command -v pct &> /dev/null; then
    EXEC="pct exec $CT_ID --"
    PUSH="pct push $CT_ID"
else
    echo "Not on Proxmox host, using SSH..."
    EXEC="ssh root@$TARGET_IP"
    PUSH="scp"
fi

# Copy dashboard files
echo "📤 Copying dashboard files..."
if command -v pct &> /dev/null; then
    pct push $CT_ID "$BOT_ROOT/scripts/dashboard_api.py" "$BOT_DIR/scripts/dashboard_api.py"
    pct push $CT_ID "$BOT_ROOT/scripts/dashboard.html" "$BOT_DIR/scripts/dashboard.html"
    pct push $CT_ID "$SCRIPT_DIR/trading-dashboard.service" "/etc/systemd/system/trading-dashboard.service"
    pct exec $CT_ID -- chown -R botuser:botuser "$BOT_DIR/scripts/dashboard_api.py" "$BOT_DIR/scripts/dashboard.html"
else
    scp "$BOT_ROOT/scripts/dashboard_api.py" "root@$TARGET_IP:$BOT_DIR/scripts/dashboard_api.py"
    scp "$BOT_ROOT/scripts/dashboard.html" "root@$TARGET_IP:$BOT_DIR/scripts/dashboard.html"
    scp "$SCRIPT_DIR/trading-dashboard.service" "root@$TARGET_IP:/etc/systemd/system/trading-dashboard.service"
    ssh root@$TARGET_IP "chown -R botuser:botuser $BOT_DIR/scripts/dashboard_api.py $BOT_DIR/scripts/dashboard.html"
fi

echo "⚙️  Enabling dashboard service..."
if command -v pct &> /dev/null; then
    pct exec $CT_ID -- systemctl daemon-reload
    pct exec $CT_ID -- systemctl enable trading-dashboard
    pct exec $CT_ID -- systemctl restart trading-dashboard
else
    ssh root@$TARGET_IP "systemctl daemon-reload && systemctl enable trading-dashboard && systemctl restart trading-dashboard"
fi

echo ""
echo "✅ Dashboard deployed!"
echo ""
echo "Access the dashboard at:"
echo "   http://$TARGET_IP:8080/"
echo ""
echo "Check status:"
echo "   systemctl status trading-dashboard"
echo "   journalctl -u trading-dashboard -f"
echo ""
echo "API endpoints:"
echo "   http://$TARGET_IP:8080/api/status   - Full bot state"
echo "   http://$TARGET_IP:8080/api/trades   - Trade history"
echo "   http://$TARGET_IP:8080/api/logs     - Log entries"
echo "   http://$TARGET_IP:8080/api/health   - Health check"
