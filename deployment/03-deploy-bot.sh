#!/bin/bash
# Deploy Bot Code to Container
# Run this from your Proxmox host

set -e

CT_ID=300  # Must match container ID from step 1
BOT_DIR="/opt/trading-bot"

echo "🚀 Deploying Trading Bot to container $CT_ID..."
echo ""

# Check if container exists and is running
if ! pct status $CT_ID | grep -q "running"; then
    echo "❌ Container $CT_ID is not running!"
    echo "   Start it: pct start $CT_ID"
    exit 1
fi

# Get script directory (where deployment folder is)
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
BOT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "📦 Bot source: $BOT_ROOT"
echo "📍 Target: container $CT_ID at $BOT_DIR"
echo ""

# Create bot directories
echo "📁 Creating bot directories in container..."
pct exec $CT_ID -- mkdir -p $BOT_DIR/scripts
pct exec $CT_ID -- mkdir -p $BOT_DIR/memory
pct exec $CT_ID -- mkdir -p $BOT_DIR/reconciliation
pct exec $CT_ID -- chown -R botuser:botuser $BOT_DIR

# Copy all bot scripts (v2 requires all modules)
echo "📤 Copying bot scripts..."
BOT_SCRIPTS=(
    trading_bot.py
    trading_strategy.py
    advanced_strategy.py
    blofin_api.py
    blofin_adapter.py
    exchange_adapter.py
    coinbase_adapter.py
    config_utils.py
    risk_utils.py
    market_data_stream.py
    private_order_stream.py
    live_profile_manager.py
)
for script in "${BOT_SCRIPTS[@]}"; do
    if [ -f "$BOT_ROOT/scripts/$script" ]; then
        pct push $CT_ID "$BOT_ROOT/scripts/$script" "$BOT_DIR/scripts/$script"
    else
        echo "⚠️  Missing: scripts/$script"
    fi
done

# Make scripts executable
pct exec $CT_ID -- chmod +x "$BOT_DIR/scripts/"*.py

# Copy config if exists (WARNING: contains credentials!)
if [ -f "$BOT_ROOT/config.json" ]; then
    echo "🔐 Copying config.json..."
    pct push $CT_ID "$BOT_ROOT/config.json" "$BOT_DIR/config.json"
    pct exec $CT_ID -- chown botuser:botuser "$BOT_DIR/config.json"
    pct exec $CT_ID -- chmod 600 "$BOT_DIR/config.json"
else
    echo "⚠️  config.json not found. You'll need to create it manually."
    echo "   Copy config.example.json and fill in your API credentials."
fi

# Copy systemd service
echo "⚙️  Installing systemd service..."
pct push $CT_ID "$SCRIPT_DIR/trading-bot.service" "/etc/systemd/system/trading-bot.service"
pct exec $CT_ID -- systemctl daemon-reload

echo ""
echo "✅ Deployment complete!"
echo ""
echo "Next steps:"
echo "1. Run preflight check:"
echo "   pct enter $CT_ID"
echo "   cd $BOT_DIR"
echo "   python3 scripts/trading_bot.py --config config.json --preflight"
echo ""
echo "2. Enable and start service:"
echo "   systemctl enable trading-bot"
echo "   systemctl start trading-bot"
echo ""
echo "3. Monitor logs:"
echo "   journalctl -u trading-bot -f"
echo "   tail -f $BOT_DIR/memory/trading-log.jsonl"
