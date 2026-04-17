#!/bin/bash
# Bootstrap Trading Bot Container
# Run this INSIDE the LXC container (or via pct exec)

set -e

echo "🔧 Bootstrapping Trading Bot Container..."
echo ""

# Update system
echo "📦 Updating system packages..."
apt-get update
apt-get upgrade -y

# Install dependencies
echo "📦 Installing Python and dependencies..."
apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    git \
    curl \
    vim \
    htop \
    screen

# Create bot user (optional, can run as root for simplicity)
echo "👤 Creating bot user..."
if ! id -u botuser &>/dev/null; then
    useradd -m -s /bin/bash botuser
    echo "botuser ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers
fi

# Create bot directory
echo "📁 Creating bot directory..."
mkdir -p /opt/trading-bot
chown botuser:botuser /opt/trading-bot

# Install Python packages
echo "🐍 Installing Python packages..."
pip3 install --upgrade pip
pip3 install requests numpy pandas

echo ""
echo "✅ Bootstrap complete!"
echo ""
echo "Next steps:"
echo "1. Upload bot code to /opt/trading-bot/"
echo "2. Create config.json with your API credentials"
echo "3. Install systemd service"
echo ""
echo "From your Proxmox host, run:"
echo "   ./03-deploy-bot.sh"
