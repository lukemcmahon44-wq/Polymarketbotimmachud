#!/usr/bin/env bash
# =============================================================================
# setup_client.sh  —  Run this on your local bot machine (Ubuntu 22.04 / Debian 12)
# =============================================================================
# Usage:
#   sudo bash vpn/setup_client.sh <VPS_IP> <SERVER_PUBLIC_KEY>
#
# Example:
#   sudo bash vpn/setup_client.sh 123.45.67.89 abc123...pubkey==
#
# This writes two files:
#   /etc/wireguard/arb0.conf      — wg-quick format (for reference)
#   /etc/wireguard/arb0-raw.conf  — raw wg format used by start_vpn.sh
#
# Raw format omits Address/DNS/PostUp (wg-quick extensions) so it works
# with 'wg setconf' inside a network namespace.
# =============================================================================

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: run as root  (sudo bash vpn/setup_client.sh ...)"
    exit 1
fi

VPS_IP="${1:?Usage: $0 <VPS_IP> <SERVER_PUBLIC_KEY>}"
SERVER_PUB="${2:?Usage: $0 <VPS_IP> <SERVER_PUBLIC_KEY>}"

echo "=== [1/3] Installing WireGuard ==="
apt-get update -y -q
apt-get install -y -q wireguard iproute2 curl

echo "=== [2/3] Generating client keypair ==="
CLIENT_PRIV=$(wg genkey)
CLIENT_PUB=$(echo "$CLIENT_PRIV" | wg pubkey)

echo "=== [3/3] Writing config files ==="

# wg-quick format (human-readable reference, not used directly by namespace setup)
cat > /etc/wireguard/arb0.conf << WGEOF
[Interface]
Address    = 10.200.0.2/24
PrivateKey = ${CLIENT_PRIV}
# Uncomment to use wg-quick instead of namespace approach:
# DNS = 1.1.1.1

[Peer]
PublicKey           = ${SERVER_PUB}
Endpoint            = ${VPS_IP}:51820
AllowedIPs          = 0.0.0.0/0, ::/0
PersistentKeepalive = 25
WGEOF

# Raw format for 'wg setconf' (no wg-quick extensions)
cat > /etc/wireguard/arb0-raw.conf << WGEOF
[Interface]
PrivateKey = ${CLIENT_PRIV}

[Peer]
PublicKey           = ${SERVER_PUB}
Endpoint            = ${VPS_IP}:51820
AllowedIPs          = 0.0.0.0/0, ::/0
PersistentKeepalive = 25
WGEOF

# DNS config for the arb_vpn namespace (Linux auto-reads this)
mkdir -p /etc/netns/arb_vpn
echo 'nameserver 1.1.1.1' > /etc/netns/arb_vpn/resolv.conf
echo 'nameserver 8.8.8.8' >> /etc/netns/arb_vpn/resolv.conf

chmod 600 /etc/wireguard/arb0.conf
chmod 600 /etc/wireguard/arb0-raw.conf

echo ""
echo "============================================================"
echo "  CLIENT SETUP COMPLETE"
echo "============================================================"
echo "  Client public key: ${CLIENT_PUB}"
echo ""
echo "  NEXT STEPS:"
echo "  Paste this into /etc/wireguard/wg0.conf on your VPS:"
echo ""
echo "  [Peer]"
echo "  PublicKey  = ${CLIENT_PUB}"
echo "  AllowedIPs = 10.200.0.2/32"
echo ""
echo "  Then on the VPS run:"
echo "    sudo systemctl restart wg-quick@wg0"
echo ""
echo "  Then on this machine run:"
echo "    sudo bash vpn/start_vpn.sh"
echo "============================================================"
