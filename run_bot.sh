#!/usr/bin/env bash
# =============================================================================
# run_bot.sh  —  Single entry point for 24/7 operation
# =============================================================================
# Starts the Belize VPN namespace, runs the arb bot inside it, and
# auto-restarts on crash. Ctrl-C or SIGTERM cleanly stops the VPN.
#
# Usage:
#   sudo bash run_bot.sh           # dry-run (default)
#   sudo bash run_bot.sh --live    # live trading
#
# Requirements:
#   - vpn/setup_server.sh run on the Belize VPS
#   - vpn/setup_client.sh run on this machine
#   - .venv/ created: python -m venv .venv && .venv/bin/pip install -r arb_bot/requirements.txt
#   - arb_bot/.env filled in
# =============================================================================

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: run_bot.sh must run as root to manage the VPN namespace."
    echo "Usage: sudo bash run_bot.sh [--live] [--no-dashboard]"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NS="arb_vpn"
VENV="${SCRIPT_DIR}/.venv"
LOG_DIR="${SCRIPT_DIR}/arb_bot/logs"
RESTART_DELAY=10
BOT_USER="${SUDO_USER:-$(logname 2>/dev/null || echo root)}"

mkdir -p "${LOG_DIR}"

# ---- Cleanup on exit -------------------------------------------------------
cleanup() {
    echo ""
    echo "[run_bot] Shutting down — stopping VPN namespace..."
    bash "${SCRIPT_DIR}/vpn/stop_vpn.sh" || true
    exit 0
}
trap cleanup INT TERM

# ---- Start VPN -------------------------------------------------------------
echo "[run_bot] Starting Belize VPN namespace..."
bash "${SCRIPT_DIR}/vpn/start_vpn.sh"

echo "[run_bot] VPN ready. Starting bot (user: ${BOT_USER})"
echo "[run_bot] Logs: ${LOG_DIR}/arb_bot_$(date +%Y%m%d).jsonl"
echo ""

# ---- Auto-restart loop -----------------------------------------------------
while true; do
    START_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    echo "[${START_TIME}] Launching arb bot..."

    # Run the bot as the original non-root user, inside the VPN namespace.
    # The namespace has no internet outside the VPN — only Polymarket (via Belize)
    # and any other traffic routed through the WireGuard tunnel.
    set +e
    ip netns exec "${NS}" \
        sudo -u "${BOT_USER}" -H \
        env HOME="$(eval echo ~"${BOT_USER}")" \
        "${VENV}/bin/python" -m arb_bot.main "$@" \
        2>&1 | tee -a "${LOG_DIR}/arb_bot_$(date +%Y%m%d).log"
    EXIT_CODE=${PIPESTATUS[0]}
    set -e

    STOP_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)

    if [[ $EXIT_CODE -eq 0 ]]; then
        echo "[${STOP_TIME}] Bot exited cleanly (code 0)."
    else
        echo "[${STOP_TIME}] Bot crashed (code ${EXIT_CODE}). Restarting in ${RESTART_DELAY}s..."
    fi

    sleep "${RESTART_DELAY}"
done
