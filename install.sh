#!/bin/sh
# Standalone installer for ASUSWRT-Merlin / Linux ARM64
set -eu

BASE=/jffs/mihomo
RUN=/tmp/mihomo
TARGET_USER=${TARGET_USER:-RSI}

echo "=== RT-AX86U Mihomo Transparent Proxy Installer ==="

# 1. Directory creation
mkdir -p "$BASE/artifacts" "$BASE/state" "$RUN" /koolshare/init.d /jffs/scripts

# 2. Download and verify core artifacts if missing
if [ ! -f "$BASE/artifacts/mihomo.gz" ]; then
  echo "[*] Downloading Mihomo v1.19.30 arm64..."
  curl -fsSL "https://github.com/MetaCubeX/mihomo/releases/download/v1.19.30/mihomo-linux-arm64-v1.19.30.gz" -o "$BASE/artifacts/mihomo.gz"
fi

if [ ! -f "$BASE/artifacts/ui.tgz" ]; then
  echo "[*] Downloading MetaCubeXD v1.273.0 Web UI..."
  curl -fsSL "https://github.com/MetaCubeX/metacubexd/releases/download/v1.273.0/compressed-dist.tgz" -o "$BASE/artifacts/ui.tgz"
fi

# 3. Download rsi CLI if missing
if [ ! -f "$BASE/rsi" ]; then
  echo "[*] Downloading rsi CLI tool..."
  curl -fsSL "https://github.com/WASIDJ/rsi/releases/latest/download/rsi-linux-arm64" -o "$BASE/rsi" || true
  if [ -f "$BASE/rsi" ]; then
    chmod +x "$BASE/rsi"
    ln -sf "$BASE/rsi" /koolshare/bin/rsi 2>/dev/null || true
    mkdir -p /tmp/opt/bin
    ln -sf "$BASE/rsi" /tmp/opt/bin/rsi 2>/dev/null || true
  fi
fi

# 4. Permissions & KoolShare Hooks
chmod 700 "$BASE" "$BASE"/*.sh 2>/dev/null || true
for hook in S99mihomo.sh V99mihomo.sh N99mihomo.sh; do
  [ -d /koolshare/init.d ] && ln -sf "$BASE/event.sh" "/koolshare/init.d/$hook"
done
[ -d /koolshare/init.d ] && ln -sf "$BASE/stop-event.sh" "/koolshare/init.d/T99mihomo.sh"

# 5. Integrate firewall-start hook
if [ -f /jffs/scripts/firewall-start ]; then
  if ! grep -q '/jffs/mihomo/service.sh firewall' /jffs/scripts/firewall-start; then
    echo '/jffs/mihomo/service.sh firewall' >> /jffs/scripts/firewall-start
  fi
else
  echo '#!/bin/sh' > /jffs/scripts/firewall-start
  echo '/jffs/mihomo/service.sh firewall' >> /jffs/scripts/firewall-start
fi
chmod 700 /jffs/scripts/firewall-start

echo "✅ Base installation complete. Run 'rsi sub set <URL>' to activate proxy."
