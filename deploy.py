#!/usr/bin/env python3
"""Deploy configuration or full release to router with instant zero-downtime hot-reload."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent

def load_env() -> dict:
    env_file = ROOT / 'config.env'
    config = {
        'TARGET': 'RSI@192.168.50.1',
        'LAN_IP': '192.168.50.1',
    }
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            config[k.strip()] = v.strip().strip('"').strip("'")
    return config

ENV = load_env()
TARGET = ENV['TARGET']
LAN_IP = ENV['LAN_IP']

def ssh(command, data=None):
    result = subprocess.run(
        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', TARGET, command],
        input=data,
        capture_output=True
    )
    if result.returncode:
        (ROOT / 'private/deploy-error.log').write_bytes(result.stdout + result.stderr)
        raise RuntimeError(f'Router deployment failed; see private/deploy-error.log\n{result.stderr.decode()}')
    return result.stdout.decode()

def hot_reload_api() -> bool:
    """Trigger hot-reload via Mihomo REST API without dropping connections."""
    creds_file = ROOT / 'private/credentials.json'
    if not creds_file.exists():
        return False
    keys = json.loads(creds_file.read_text())
    req = urllib.request.Request(
        f'http://{LAN_IP}:9090/configs?force=true',
        headers={'Authorization': f"Bearer {keys['api']}", 'Content-Type': 'application/json'},
        data=json.dumps({'path': '/tmp/mihomo/config.yaml'}).encode(),
        method='PUT'
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status in (200, 204)
    except Exception as e:
        print(f"Hot-reload API returned: {e}")
        return False

def ensure_firewall_hooks():
    firewall_start_content = (ROOT / 'router/firewall-start').read_text()
    ssh('cat > /tmp/firewall-start.candidate && cp /tmp/firewall-start.candidate /jffs/scripts/firewall-start && chmod 700 /jffs/scripts/firewall-start && rm -f /tmp/firewall-start.candidate', firewall_start_content.encode())

def fast_deploy():
    """Fast-track: update config.yaml, firewall scripts, chnroute and trigger API hot-reload."""
    print(f"[*] Fast deploying to {TARGET}...")
    
    # Sync firewall scripts & chnroute if updated
    if (ROOT / 'build/mihomo/chnroute.txt').exists():
        ssh('cat > /jffs/mihomo/chnroute.txt.new && mv /jffs/mihomo/chnroute.txt.new /jffs/mihomo/chnroute.txt && chmod 644 /jffs/mihomo/chnroute.txt', (ROOT / 'build/mihomo/chnroute.txt').read_bytes())
    if (ROOT / 'build/mihomo/firewall.sh').exists():
        ssh('cat > /jffs/mihomo/firewall.sh.new && mv /jffs/mihomo/firewall.sh.new /jffs/mihomo/firewall.sh && chmod 700 /jffs/mihomo/firewall.sh', (ROOT / 'build/mihomo/firewall.sh').read_bytes())

    ensure_firewall_hooks()
    ssh('/jffs/scripts/firewall-start')

    config_bytes = (ROOT / 'build/mihomo/config.yaml').read_bytes()
    ssh('cat > /tmp/mihomo/config.candidate.yaml', config_bytes)
    
    # Pre-flight syntax validation
    check = ssh('/tmp/mihomo/mihomo -t -d /tmp/mihomo -f /tmp/mihomo/config.candidate.yaml 2>&1 || true')
    if 'successful' not in check:
        raise RuntimeError(f"Config syntax validation failed on router:\n{check}")
        
    ssh('''
set -e
[ -f /jffs/mihomo/config.yaml ] && cp /jffs/mihomo/config.yaml /jffs/mihomo/config.previous.yaml
mv /tmp/mihomo/config.candidate.yaml /jffs/mihomo/config.yaml
chmod 600 /jffs/mihomo/config.yaml
ln -sf /jffs/mihomo/config.yaml /tmp/mihomo/config.yaml
''')

    if hot_reload_api():
        print("✅ Config updated and hot-reloaded successfully via API! (Zero Downtime)")
    else:
        print("API reload unavailable; restarting service gracefully...")
        print(ssh('/jffs/mihomo/service.sh restart; sleep 1; /jffs/mihomo/service.sh status'))

def full_deploy():
    """Full-track: sync binary artifacts, scripts, hooks, and restart service."""
    print(f"[*] Full deploying release to {TARGET}...")
    ssh('mkdir -p /tmp/mihomo-stage; tar -xzf - -C /tmp/mihomo-stage', (ROOT / 'build/release.tgz').read_bytes())
    for name in ['mihomo.gz', 'ui.tgz', 'country.mmdb.gz']:
        artifact_path = ROOT / 'build/mihomo/artifacts' / name
        if artifact_path.exists():
            expected = hashlib.md5(artifact_path.read_bytes()).hexdigest()
            actual = ssh('md5sum /tmp/mihomo-stage/mihomo/artifacts/' + name).split()[0]
            assert actual == expected, f'SSH transfer integrity failed for {name}'

    ssh('if [ -x /tmp/mihomo/mihomo ]; then /tmp/mihomo/mihomo -t -d /tmp/mihomo -f /tmp/mihomo-stage/mihomo/config.yaml >/tmp/mihomo/candidate-check.log 2>&1; fi')
    ssh('''set -e
if [ ! -d /jffs/mihomo ]; then
  cp -a /tmp/mihomo-stage/mihomo /jffs/mihomo
else
  cp /jffs/mihomo/config.yaml /jffs/mihomo/config.previous.yaml 2>/dev/null || true
  cp /jffs/mihomo/clients.txt /jffs/mihomo/clients.previous.txt 2>/dev/null || true
  for file in config.yaml clients.txt service.sh firewall.sh event.sh stop-event.sh manifest.json chnroute.txt; do
    if [ -f /tmp/mihomo-stage/mihomo/$file ]; then
      cp /tmp/mihomo-stage/mihomo/$file /jffs/mihomo/$file.new
      mv /jffs/mihomo/$file.new /jffs/mihomo/$file
    fi
  done
  cp -a /tmp/mihomo-stage/mihomo/artifacts/* /jffs/mihomo/artifacts/ 2>/dev/null || true
fi
chmod 700 /jffs/mihomo /jffs/mihomo/*.sh
chmod 600 /jffs/mihomo/config*.yaml
chown -R 0:0 /jffs/mihomo
for hook in S99mihomo.sh V99mihomo.sh N99mihomo.sh; do
  [ -d /koolshare/init.d ] && ln -sf /jffs/mihomo/event.sh /koolshare/init.d/$hook
done
[ -d /koolshare/init.d ] && ln -sf /jffs/mihomo/stop-event.sh /koolshare/init.d/T99mihomo.sh
''')

    ensure_firewall_hooks()

    try:
        print(ssh('/jffs/mihomo/service.sh restart; sleep 2; /jffs/mihomo/service.sh status'))
    except RuntimeError:
        ssh('/jffs/mihomo/service.sh rollback')
        raise

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Deploy to router")
    parser.add_argument('--full', action='store_true', help="Perform full binary artifacts sync and service restart")
    args = parser.parse_args()

    # Check if core is already installed on the router
    is_installed = ssh('[ -f /jffs/mihomo/artifacts/mihomo.gz ] && [ -x /tmp/mihomo/mihomo ] && echo "YES" || echo "NO"').strip()
    if args.full or is_installed != "YES":
        full_deploy()
    else:
        fast_deploy()
