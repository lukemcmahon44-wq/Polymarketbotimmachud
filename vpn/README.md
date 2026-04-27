# Belize-Region WireGuard VPN — Setup Guide

This VPN creates an isolated Linux network namespace called `arb_vpn`.
Only the arb bot process runs inside it. Everything else on your machine
uses your normal internet connection.

```
 Your machine
 ┌─────────────────────────────────────────────────────┐
 │  Normal processes  ──→  your ISP  ──→  internet     │
 │                                                       │
 │  arb_vpn namespace                                   │
 │  └─ arb_bot process ──→ WireGuard ──→ Belize VPS    │
 │                            (arb0)     10.200.0.1     │
 │                                           │           │
 │                                           ↓           │
 │                                   clob.polymarket.com │
 └─────────────────────────────────────────────────────┘
```

---

## Step 0: Get a VPS

Polymarket blocks US IP addresses. You need a VPS outside the US.

**Practically speaking, "Belize-based" means:**
- No major cloud provider has data centers physically in Belize.
- The nearest practical options are Miami, FL (Vultr, DigitalOcean, Linode).
- Miami works identically for this use case — Polymarket blocks US *residential/commercial*
  IPs, not geographic regions. A Miami datacenter IP is treated the same as a
  Belizean IP for access purposes.
- If you specifically need a Belize IP address:
  - **BTL (Belize Telecommunications Ltd)** — national carrier, may offer VPS/hosting
  - **Speednet Communications** — Belize ISP with business services
  - Search: "VPS hosting Belize" for local providers

**Recommended providers (Miami / Caribbean):**
| Provider | Region | Monthly cost | Notes |
|---|---|---|---|
| Vultr | Miami | ~$6/mo | Reliable, fast provisioning |
| DigitalOcean | New York / Toronto | ~$6/mo | Good uptime |
| Linode (Akamai) | Miami | ~$5/mo | Solid |
| Hetzner | Helsinki / Nuremberg | ~$5/mo | Europe, also non-US |

Minimum spec: **1 vCPU, 512 MB RAM, Ubuntu 22.04**.

The VPS only forwards your bot's traffic — it does not store data.

---

## Step 1: Set up the VPS

SSH into your new VPS and run:

```bash
curl -O https://raw.githubusercontent.com/.../vpn/setup_server.sh  # or scp the file
sudo bash setup_server.sh
```

It will print:
```
VPS public IP    : 123.45.67.89
Server public key: abc123...==
```

**Save both values** — you need them in Step 2.

---

## Step 2: Set up your local machine

On the machine where the bot will run:

```bash
sudo bash vpn/setup_client.sh  123.45.67.89  abc123...==
#                               ^VPS IP       ^server public key from Step 1
```

It will print a **client public key**.

---

## Step 3: Register the client on the VPS

Back on the VPS, edit `/etc/wireguard/wg0.conf` and replace the commented
`[Peer]` block with the client public key:

```ini
[Peer]
PublicKey  = <CLIENT_PUBLIC_KEY_FROM_STEP_2>
AllowedIPs = 10.200.0.2/32
```

Then restart WireGuard:
```bash
sudo systemctl restart wg-quick@wg0
```

---

## Step 4: Test the VPN

```bash
# Start the VPN namespace
sudo bash vpn/start_vpn.sh

# You should see:
# [vpn] Polymarket reachable. Egress IP: 123.45.67.89  <-- your VPS IP

# Quick sanity check — run a curl inside the namespace:
sudo ip netns exec arb_vpn curl https://ifconfig.me
# Should print your VPS IP, not your home IP
```

---

## Step 5: Run the bot

```bash
# Dry-run (default, safe to start here)
sudo bash run_bot.sh

# Live trading
sudo bash run_bot.sh --live
```

`run_bot.sh` handles everything:
- Starts the VPN namespace
- Runs the bot inside it
- Auto-restarts on crash (10s delay)
- Stops the VPN cleanly on Ctrl-C

---

## Running 24/7 with systemd

Once you've verified it works manually, set it up as a system service:

```bash
sudo cp arb_bot.service /etc/systemd/system/
# Edit /etc/systemd/system/arb_bot.service and set WorkingDirectory + User
sudo systemctl daemon-reload
sudo systemctl enable --now arb_bot
sudo systemctl status arb_bot

# View live logs
sudo journalctl -u arb_bot -f
```

---

## VPN FAQ

**Q: Does the VPN affect Kalshi trading?**
Yes — when the bot is running, its Kalshi calls also go through the Belize VPS.
Kalshi has no geo-restrictions so this works fine; it adds ~10-30ms latency
(Miami) vs. your direct connection.

**Q: What happens if the VPN drops?**
The WireGuard client attempts reconnection continuously (PersistentKeepalive=25s).
During a drop, all the bot's network calls will fail with connection errors.
The bot's retry logic and WebSocket reconnect loops handle this — it will
resume automatically once the VPN comes back. Kalshi calls also pause during
a VPN drop since the bot runs entirely inside the namespace.

**Q: Does this affect anything else on my machine?**
No. The `arb_vpn` namespace is completely isolated. Your browser, Kalshi
API calls outside the bot, and all other processes use your normal connection.

**Q: How do I check the VPN is working?**
```bash
# Should print your VPS IP
sudo ip netns exec arb_vpn curl https://ifconfig.me

# Show WireGuard status
sudo ip netns exec arb_vpn wg show arb0
```

**Q: How do I stop the VPN manually?**
```bash
sudo bash vpn/stop_vpn.sh
```

---

## Security notes

- The VPS only NATs your traffic — it does not decrypt HTTPS (TLS is end-to-end).
- Keep `/etc/wireguard/arb0-raw.conf` (contains your private key) permissions at 600.
- Do not expose port 51820 beyond what WireGuard needs (UDP only).
- The VPS itself should have SSH key-only auth and a firewall allowing only ports
  22 (SSH) and 51820/UDP (WireGuard).
