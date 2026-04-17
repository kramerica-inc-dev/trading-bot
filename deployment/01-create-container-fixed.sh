#!/bin/bash
# Create LXC Container on Proxmox for Trading Bot
# Run this on your Proxmox host via SSH

set -e

# Configuration
CT_ID=25020  # Change if this ID is already used
CT_HOSTNAME="trading-bot"
CT_PASSWORD="${CT_PASSWORD:-changeme123}"  # Override via environment variable
CT_STORAGE="local-lvm"  # Change to your storage name if different
CT_MEMORY=512  # MB
CT_SWAP=512
CT_DISK=4  # GB
CT_CORES=1

# Network (DHCP - adjust if you use static)
CT_NETWORK="vmbr0"
CT_VLAN="25020"  # VLAN tag

# Template (Ubuntu 22.04)
TEMPLATE="local:vztmpl/ubuntu-22.04-standard_22.04-1_amd64.tar.zst"

echo "🔧 Creating LXC container for Blofin Trading Bot..."
echo "   ID: $CT_ID"
echo "   Hostname: $CT_HOSTNAME"
echo ""

# Check if container ID exists
if pct status $CT_ID &>/dev/null; then
    echo "❌ Container ID $CT_ID already exists!"
    echo "   Change CT_ID in this script or remove existing container:"
    echo "   pct destroy $CT_ID"
    exit 1
fi

# Check if template exists
if ! pveam list $CT_STORAGE | grep -q "ubuntu-22.04"; then
    echo "📥 Downloading Ubuntu 22.04 template..."
    pveam download local ubuntu-22.04-standard_22.04-1_amd64.tar.zst
fi

# Create container
echo "🏗️  Creating container..."
pct create $CT_ID $TEMPLATE \
    --hostname $CT_HOSTNAME \
    --password $CT_PASSWORD \
    --memory $CT_MEMORY \
    --swap $CT_SWAP \
    --storage $CT_STORAGE \
    --rootfs $CT_STORAGE:$CT_DISK \
    --cores $CT_CORES \
    --net0 name=eth0,bridge=$CT_NETWORK,ip=dhcp \
    --features nesting=1 \
    --unprivileged 1 \
    --onboot 1

echo "✅ Container created!"
echo ""
echo "🚀 Starting container..."
pct start $CT_ID

# Wait for container to boot
echo "⏳ Waiting for container to boot..."
sleep 5

# Get container IP
CT_IP=$(pct exec $CT_ID -- hostname -I | awk '{print $1}')
echo "📍 Container IP: $CT_IP"

echo ""
echo "✅ Container is ready!"
echo ""
echo "Next steps:"
echo "1. SSH into container: ssh root@$CT_IP (password: $CT_PASSWORD)"
echo "2. Run bootstrap script: ./02-bootstrap-container.sh"
echo ""
echo "Or run directly from Proxmox host:"
echo "   pct push $CT_ID ./02-bootstrap-container.sh /root/bootstrap.sh"
echo "   pct exec $CT_ID -- bash /root/bootstrap.sh"
