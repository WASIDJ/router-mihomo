# AGENTS.md - router-mihomo Architecture & Agent Operation Guide

Welcome to `router-mihomo`! This document provides AI coding agents and human developers with immediate architectural clarity, operational procedures, strict safety invariants, and troubleshooting protocols.

---

## 1. System & Hardware Specifications

| Component | Specification |
| :--- | :--- |
| **Router Model** | ASUS RT-AX86U (Broadcom BCM4908, 4x Cortex-A53 @ 1.8GHz) |
| **OS / Firmware** | ASUSWRT-Merlin-KoolShare 388.8_2 (Linux kernel 4.1.52 aarch64) |
| **Storage Architecture** | NAND Flash partition `/jffs` (47MB total, ~20MB free). RAM disk `/tmp` (456MB tmpfs). |
| **Core Software** | Mihomo (Clash Meta) v1.19.30 (arm64), MetaCubeXD v1.273.0 Web UI |
| **LAN / Subnet** | `192.168.50.1/24` (Interface: `br0`) |
| **WAN Interface** | `eth0` (1000Mb/s Full Duplex) |

---

## 2. Core Architecture & Mental Model

### A. Flash Wear & Storage Protection
- **Problem**: Router NAND flash has strict write cycle limits and `/jffs` is only 47MB.
- **Solution**:
  - Binaries and assets are stored **compressed** in `/jffs/mihomo/artifacts/` (`mihomo.gz`, `ui.tgz`, `country.mmdb.gz`, total ~22MB).
  - On service boot (`service.sh prepare`), artifacts are unpacked into RAM (`/tmp/mihomo`, tmpfs). Decompression takes ~1.3s.
  - Runtime logs live in `/tmp/mihomo/core.log` (RAM) and are bounded to **1MB** (`wc -c > 1048576`), after which they are truncated.
  - **Invariant**: **NEVER write persistent logs or high-frequency telemetry directly to `/jffs`**.

### B. Transparent Proxy & Network Routing
- **DNS Redirection**:
  - `MH_DNS` in iptables `nat` PREROUTING redirects all UDP/TCP 53 from LAN clients (`mh_clients` ipset) to local port `1053` (Mihomo Fake-IP DNS).
- **TCP/UDP TPROXY Interception**:
  - `MH_ROUTE` in iptables `mangle` PREROUTING intercepts LAN TCP and UDP traffic (skipping port 53 and private subnets) and routes via `TPROXY --on-port 7893 --tproxy-mark 0x01000000/0x01000000`.
  - Kernel routing rule: `ip rule pref 11000 fwmark 0x1000000/0x1000000 lookup 110`.
  - Route table 110: `ip route add local 0.0.0.0/0 dev lo table 110`.
- **Anti-Leak Guard (`MH_GUARD`)**:
  - In iptables `filter` FORWARD chain, `MH_GUARD` rejects any unproxied public traffic from LAN clients if the core is stopped, preventing privacy leaks.

### C. Zero-Downtime Hot Reload
- Updating nodes or subscriptions does **not** restart the service.
- The system calls Mihomo's REST API `PUT /configs?force=true` pointing to the symlink `/tmp/mihomo/config.yaml`.
- Config reload takes `< 0.2s` with zero dropped TCP/UDP connections.

---

## 3. Directory & File Reference

```text
router-mihomo/
├── router/
│   ├── service.sh           # Core daemon: prepare (decompression), launch, supervise, reload, status
│   ├── firewall.sh          # Firewall rules: MH_ROUTE (TPROXY), MH_DNS (redir), MH_INPUT, MH_GUARD
│   ├── event.sh             # KoolShare event hook (WAN restart, NAT rebuild, service start)
│   ├── stop-event.sh        # KoolShare shutdown hook
│   ├── firewall-start       # Merlin firewall-start hook (re-applies rules after firewall restart)
│   ├── clients.txt          # CIDR list of LAN clients to intercept (default: 192.168.50.0/24)
│   └── config.template.yaml # Bootstrap configuration template
├── manifest.json            # Pinned binary & UI versions with official URLs and SHA256 hashes
├── build.py                 # Desktop builder: downloads sub, merges custom nodes, outputs build/mihomo/
├── deploy.py                # Fast-track deployer: uploads config.yaml & calls REST API hot-reload
├── node_manager.py          # Python engine for Hysteria 2 URI parsing and proxy group injection
├── verify.py                # Automated verifier: API auth, group selectors, static assets, proxy curl
├── verify_transparent.py    # Automated verifier: Fake-IP DNS, transparent HTTPS curl, UDP STUN
├── install.sh               # Standalone router installation script
├── config.env.example       # Example user configuration (TARGET, AIRPORT_URL, etc.)
└── custom_nodes.example.yaml# Example Hysteria 2 / custom node specifications
```

---

## 4. Agent Operation Playbook

### Workflow 1: Validating Router State
To verify the router is healthy and operational:
```sh
# Run functionality & transparent proxy tests
python3 verify.py
python3 verify_transparent.py

# Or check via SSH
ssh RSI@192.168.50.1 rsi status
```

### Workflow 2: Updating Configuration or Nodes
1. Edit `private/custom_nodes.yaml` or change `AIRPORT_URL`.
2. Build:
   ```sh
   python3 build.py
   ```
3. Deploy (fast API reload without dropping connections):
   ```sh
   python3 deploy.py
   ```
4. Verify:
   ```sh
   python3 verify.py
   ```

### Workflow 3: Emergency Bypass & Network Recovery
If traffic is blocked or Mihomo misbehaves:
```sh
# Step 1: Stop service and remove iptables rules
ssh RSI@192.168.50.1 "/jffs/mihomo/service.sh stop && /jffs/mihomo/firewall.sh remove"

# Step 2: Rollback to previous working configuration
ssh RSI@192.168.50.1 "/jffs/mihomo/service.sh rollback"
```

---

## 5. Strict Invariants for Agents

1. **Never Overwrite `/jffs/scripts/firewall-start` blindly**:
   - Other plugins (Entware, custom DDNS) share this file. Check with `grep` and append idempotently.
2. **Never commit sensitive files**:
   - `private/`, `config.env`, `*.key`, `credentials.json` are in `.gitignore` and must stay private.
3. **Cross-device linking in Go/Shell**:
   - Moving files between `/tmp` and `/jffs` with `os.Rename` or `mv` across mount boundaries will fail with `EXDEV`. Always stage on the target partition before atomic rename.
4. **Preserve `MH_GUARD` during normal operations**:
   - The FORWARD guard protects against silent unencrypted direct leaks. Only remove it during explicit recovery (`firewall.sh remove`).
