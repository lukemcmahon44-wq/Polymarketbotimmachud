#!/usr/bin/env bash
# =============================================================================
# start_vpn.sh  —  Create the isolated 'arb_vpn' network namespace
# =============================================================================
# The namespace has its OWN network stack, completely separate from the host.
# Only processes explicitly run inside it (via 'ip netns exec arb_vpn ...')
# use the VPN. Everything else on your machine is unaffected.
#
# Usage:  sudo bash vpn/start_vpn.sh
# =============================================================================

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: run as root  (sudo bash vpn/start_vpn.sh)"
    exit 1
fi

NS="arb_vpn"
WG="arb0"
CLIENT_IP="10.200.0.2"
RAW_CONF="/etc/wireguard/arb0-raw.conf"

if [[ ! -f "$RAW_CONF" ]]; then
    echo "ERROR: $RAW_CONF not found. Run vpn/setup_client.sh first."
    exit 1
fi

# Clean up any previous state
bash "$(dirname "$0")/stop_vpn.sh" 2>/dev/null || true

echo "[vpn] Creating network namespace: ${NS}"
ip netns add "${NS}"

echo "[vpn] Loading WireGuard kernel module"
modprobe wireguard 2>/dev/null || true  # already loaded on most modern kernels

echo "[vpn] Creating WireGuard interface inside namespace"
ip link add "${WG}" type wireguard
ip link set "${WG}" netns "${NS}"

echo "[vpn] Applying WireGuard config"
ip netns exec "${NS}" wg setconf "${WG}" "${RAW_CONF}"

echo "[vpn] Assigning IP address"
ip netns exec "${NS}" ip addr add "${CLIENT_IP}/24" dev "${WG}"
ip netns exec "${NS}" ip link set "${WG}" up
ip netns exec "${NS}" ip link set lo up

echo "[vpn] Setting default route through VPN tunnel"
ip netns exec "${NS}" ip route add default dev "${WG}"

echo "[vpn] Waiting for WireGuard handshake..."
for i in {1..10}; do
    HANDSHAKE=$(ip netns exec "${NS}" wg show "${WG}" latest-handshakes 2>/dev/null | awk '{print $2}')
    if [[ -n "$HANDSHAKE" && "$HANDSHAKE" != "0" ]]; then
        echo "[vpn] Handshake established."
        break
    fi
    sleep 1
done

echo "[vpn] Verifying Polymarket connectivity..."
if ! ip netns exec "${NS}" curl -s --max-time 10 "https://clob.polymarket.com/" > /dev/null; then
    echo "ERROR: Polymarket unreachable through VPN."
    echo "  -> Check that the VPS is running: sudo systemctl status wg-quick@wg0"
    echo "  -> Check VPS firewall allows UDP port 51820"
    bash "$(dirname "$0")/stop_vpn.sh" || true
    exit 1
fi

EGRESS=$(ip netns exec "${NS}" curl -s --max-time 5 "https://ifconfig.me" || echo "unknown")
echo "[vpn] Polymarket reachable. Egress IP: ${EGRESS}"
echo "[vpn] Namespace '${NS}' is ready. Bot traffic will exit via this IP."
