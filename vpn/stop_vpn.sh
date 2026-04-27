#!/usr/bin/env bash
# =============================================================================
# stop_vpn.sh  —  Tear down the arb_vpn namespace
# =============================================================================
# Usage:  sudo bash vpn/stop_vpn.sh
# =============================================================================

NS="arb_vpn"
WG="arb0"

if ip netns list 2>/dev/null | grep -q "^${NS}"; then
    ip netns exec "${NS}" ip link set "${WG}" down 2>/dev/null || true
    ip netns exec "${NS}" ip link delete "${WG}"  2>/dev/null || true
    ip netns delete "${NS}" 2>/dev/null || true
    echo "[vpn] Namespace '${NS}' stopped."
else
    echo "[vpn] Namespace '${NS}' not running — nothing to do."
fi
