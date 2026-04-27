#!/usr/bin/env bash
# =============================================================================
# setup_server.sh  —  Run this on your Belize-region VPS (Ubuntu 22.04 / Debian 12)
# =============================================================================
# Usage:
#   sudo bash setup_server.sh
#
# What it does:
#   1. Installs WireGuard
#   2. Generates server keypair
#   3. Writes /etc/wireguard/wg0.conf
#   4. Enables IP forwarding + NAT (so your bot can reach the internet)
#   5. Starts and enables the wg0 service
#
# After this script, copy SERVER_PUBLIC_KEY and your VPS IP into setup_client.sh
# on your local machine, then paste the client public key back here.
# =============================================================================

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: run as root  (sudo bash setup_server.sh)"
    exit 1
fi

echo "=== [1/5] Installing WireGuard ==="
apt-get update -y -q
apt-get install -y -q wireguard iptables curl

echo "=== [2/5] Generating server keypair ==="
SERVER_PRIV=$(wg genkey)
SERVER_PUB=$(echo "$SERVER_PRIV" | wg pubkey)

# Detect primary outbound interface (eth0, ens3, etc.)
PRIM_IFACE=$(ip route show default | awk '/default/ {print $5}' | head -1)
VPS_IP=$(curl -s --max-time 5 https://ifconfig.me || echo "unknown")

echo "=== [3/5] Writing /etc/wireguard/wg0.conf ==="
cat > /etc/wireguard/wg0.conf << WGEOF
[Interface]
Address    = 10.200.0.1/24
ListenPort = 51820
PrivateKey = ${SERVER_PRIV}

# NAT: forward VPN client traffic to the internet via ${PRIM_IFACE}
PostUp   = iptables -A FORWARD -i wg0 -j ACCEPT; \\
           iptables -t nat -A POSTROUTING -o ${PRIM_IFACE} -j MASQUERADE
PostDown = iptables -D FORWARD -i wg0 -j ACCEPT; \\
           iptables -t nat -D POSTROUTING -o ${PRIM_IFACE} -j MASQUERADE

# ----- Bot client (add after running setup_client.sh) -----
# [Peer]
# PublicKey  = <PASTE_CLIENT_PUBLIC_KEY_HERE>
# AllowedIPs = 10.200.0.2/32
WGEOF

chmod 600 /etc/wireguard/wg0.conf

echo "=== [4/5] Enabling IP forwarding ==="
if ! grep -q 'net.ipv4.ip_forward=1' /etc/sysctl.conf; then
    echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf
fi
sysctl -p -q

echo "=== [5/5] Starting WireGuard ==="
systemctl enable --now wg-quick@wg0

echo ""
echo "============================================================"
echo "  SERVER SETUP COMPLETE"
echo "============================================================"
echo "  VPS public IP    : ${VPS_IP}"
echo "  Server public key: ${SERVER_PUB}"
echo ""
echo "  NEXT STEPS:"
echo "  1. Run vpn/setup_client.sh on your bot machine."
echo "  2. Copy the CLIENT PUBLIC KEY it prints."
echo "  3. On this VPS, edit /etc/wireguard/wg0.conf and replace"
echo "     the commented-out [Peer] block with:"
echo ""
echo "     [Peer]"
echo "     PublicKey  = <CLIENT_PUBLIC_KEY>"
echo "     AllowedIPs = 10.200.0.2/32"
echo ""
echo "  4. Then run: sudo systemctl restart wg-quick@wg0"
echo "============================================================"
