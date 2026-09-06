#!/usr/bin/env python3
"""Build a private, pinned router release from airport subscription and custom nodes."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import tarfile
import urllib.request
import yaml

from node_manager import (
    fetch_airport_subscription,
    inject_custom_nodes,
    load_custom_nodes,
)

ROOT = Path(__file__).resolve().parent
os.umask(0o077)

def load_env() -> dict:
    env_file = ROOT / 'config.env'
    config = {
        'TARGET': 'RSI@192.168.50.1',
        'AIRPORT_URL': '',
        'CUSTOM_NODES_FILE': 'private/custom_nodes.yaml',
        'LAN_IP': '192.168.50.1',
        'LAN_SUBNET': '192.168.50.0/24',
    }
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            config[k.strip()] = v.strip().strip('"').strip("'")
    return config

def fetch_artifacts_if_needed(manifest: dict):
    artifacts_dir = ROOT / 'artifacts'
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    for name, filename in [('mihomo', 'mihomo.gz'), ('ui', 'ui.tgz')]:
        dest = artifacts_dir / filename
        expected_sha = manifest[name]['sha256']
        if dest.exists():
            if hashlib.sha256(dest.read_bytes()).hexdigest() == expected_sha:
                continue
        print(f"Downloading {name} artifact from {manifest[name]['url']}...")
        req = urllib.request.Request(manifest[name]['url'], headers={'User-Agent': 'router-mihomo'})
        with urllib.request.urlopen(req, timeout=60) as resp:
            content = resp.read()
        assert hashlib.sha256(content).hexdigest() == expected_sha, f"{name}: downloaded sha256 mismatch"
        dest.write_bytes(content)

def main():
    parser = argparse.ArgumentParser(description="Build Mihomo router configuration")
    parser.add_argument('--sub', type=str, help="Airport subscription URL to fetch")
    parser.add_argument('--update-sub', action='store_true', help="Force re-download subscription from AIRPORT_URL in config.env")
    parser.add_argument('--custom-nodes', type=str, help="Path to custom nodes YAML file")
    parser.add_argument('--source-file', type=str, help="Path to local source config.yaml")
    parser.add_argument('--fetch-artifacts', action='store_true', help="Download missing binaries from GitHub")
    args = parser.parse_args()

    env = load_env()
    private = ROOT / 'private'
    private.mkdir(exist_ok=True)

    # 1. Credentials
    credentials = private / 'credentials.json'
    if not credentials.exists():
        credentials.write_text(json.dumps({'api': secrets.token_urlsafe(32), 'proxy': secrets.token_urlsafe(24)}))
    keys = json.loads(credentials.read_text())

    # 2. Manifest & Artifacts
    manifest = json.loads((ROOT / 'manifest.json').read_text())
    if args.fetch_artifacts:
        fetch_artifacts_if_needed(manifest)
    for name, filename in [('mihomo', 'mihomo.gz'), ('ui', 'ui.tgz')]:
        target = ROOT / 'artifacts' / filename
        if not target.exists():
            print(f"Missing {target}, downloading automatically...")
            fetch_artifacts_if_needed(manifest)
            break
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        assert digest == manifest[name]['sha256'], f'{name}: checksum mismatch'

    # 3. Determine Base Config (Airport subscription / cached file / local file)
    airport_cache = private / 'airport.yaml'
    sub_url = args.sub or (env['AIRPORT_URL'] if (args.update_sub or not airport_cache.exists()) else '')

    if sub_url:
        print(f"Updating subscription from: {sub_url}")
        original = fetch_airport_subscription(sub_url, airport_cache)
    elif airport_cache.exists():
        print(f"Using cached airport subscription from {airport_cache}")
        original = yaml.safe_load(airport_cache.read_text())
    elif args.source_file and Path(args.source_file).exists():
        print(f"Using local source config from {args.source_file}")
        original = yaml.safe_load(Path(args.source_file).read_text())
    elif Path('/Users/ryou/.config/mihomo/config.yaml').exists():
        default_source = Path('/Users/ryou/.config/mihomo/config.yaml')
        print(f"Using local fallback config from {default_source}")
        original = yaml.safe_load(default_source.read_text())
    else:
        raise RuntimeError("No configuration source found! Specify --sub <URL> or provide private/airport.yaml")

    config = dict(original)

    # 4. Load & Inject Custom Nodes (e.g. Hysteria 2)
    custom_nodes_path = Path(args.custom_nodes or env['CUSTOM_NODES_FILE'])
    if not custom_nodes_path.is_absolute():
        custom_nodes_path = ROOT / custom_nodes_path
        
    custom_nodes = load_custom_nodes(custom_nodes_path)
    if custom_nodes:
        print(f"Loaded {len(custom_nodes)} custom node(s) from {custom_nodes_path}")
        config, injected_count = inject_custom_nodes(config, custom_nodes)
        print(f"Injected custom nodes into {injected_count} proxy groups")
    else:
        print(f"No custom nodes found in {custom_nodes_path} (can create it to add HY2 nodes)")

    # 5. Router-specific adaptation
    fingerprint = config.pop('global-client-fingerprint', None)
    if fingerprint:
        config['proxies'] = [dict(proxy) for proxy in config.get('proxies', [])]
        for proxy in config['proxies']:
            if proxy.get('type') in ['vless', 'vmess', 'trojan', 'anytls']:
                proxy.setdefault('client-fingerprint', fingerprint)
    for name in ['cfw-bypass', 'clash-for-android', 'tun', 'external-controller-unix', 'external-controller-pipe', 'external-controller-tls', 'external-ui-url']:
        config.pop(name, None)

    lan_ip = env['LAN_IP']
    lan_subnet = env['LAN_SUBNET']

    config.update({
        'mixed-port': 7890, 'tproxy-port': 7893, 'allow-lan': True,
        'bind-address': '*', 'lan-allowed-ips': ['127.0.0.0/8', lan_subnet],
        'external-controller': f'{lan_ip}:9090', 'secret': keys['api'],
        'external-ui': '/tmp/mihomo/ui', 'authentication': ['router:' + keys['proxy']],
        'skip-auth-prefixes': ['127.0.0.1/32'], 'ipv6': False, 'find-process-mode': 'off',
        'interface-name': 'eth0', 'log-level': 'warning', 'geodata-mode': False,
        'geo-auto-update': False,
        'profile': {'store-selected': True, 'store-fake-ip': True}
    })
    config['dns'] = dict(config.get('dns', {}))
    config['dns'].update({'listen': '0.0.0.0:1053', 'ipv6': False})
    filters = list(config['dns'].get('fake-ip-filter', []))
    for pattern in ['*.lan', '*.local', 'localhost', '*.ts.net', 'router.asus.com', 'www.asusrouter.com']:
        if pattern not in filters:
            filters.append(pattern)
    config['dns']['fake-ip-filter'] = filters

    # 6. Seed select-group defaults from the running router or desktop core if reachable
    selection = {}
    controllers = [f"{lan_ip}:9090", original.get('external-controller', '')]
    for ctrl in controllers:
        if not ctrl:
            continue
        try:
            target_url = ctrl if ctrl.startswith(('http://', 'https://')) else f'http://{ctrl}'
            req = urllib.request.Request(f'{target_url}/proxies')
            req.add_header('Authorization', f"Bearer {keys['api']}")
            with urllib.request.urlopen(req, timeout=2) as response:
                states = json.load(response).get('proxies', {})
            for group in config.get('proxy-groups', []):
                current = states.get(group['name'], {}).get('now')
                if group.get('type') == 'select' and current in group.get('proxies', []):
                    group['proxies'].remove(current)
                    group['proxies'].insert(0, current)
                    selection[group['name']] = current
            if selection:
                break
        except Exception:
            continue

    # 7. Package build release
    release = ROOT / 'build' / 'mihomo'
    (release / 'artifacts').mkdir(parents=True, exist_ok=True)
    for filename in ['mihomo.gz', 'ui.tgz']:
        shutil.copy2(ROOT / 'artifacts' / filename, release / 'artifacts' / filename)

    # country.mmdb handling
    mmdb_src = None
    if (ROOT / 'artifacts' / 'country.mmdb.gz').exists():
        shutil.copy2(ROOT / 'artifacts' / 'country.mmdb.gz', release / 'artifacts' / 'country.mmdb.gz')
    elif (release / 'artifacts' / 'country.mmdb.gz').exists():
        pass
    elif Path('/Users/ryou/.config/mihomo/country.mmdb').exists():
        with (release / 'artifacts' / 'country.mmdb.gz').open('wb') as dest:
            with gzip.GzipFile(fileobj=dest, mode='wb', mtime=0) as compressor:
                compressor.write(Path('/Users/ryou/.config/mihomo/country.mmdb').read_bytes())
    else:
        print("Warning: country.mmdb not found, using existing artifacts if present")

    for name in ['service.sh', 'firewall.sh', 'event.sh', 'stop-event.sh']:
        shutil.copy2(ROOT / 'router' / name, release / name)
        (release / name).chmod(0o700)

    (release / 'config.yaml').write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
    shutil.copy2(ROOT / 'router' / 'clients.txt', release / 'clients.txt')

    manifest['generated_config_sha256'] = hashlib.sha256((release / 'config.yaml').read_bytes()).hexdigest()
    manifest['imported_selectors'] = len(selection)
    manifest['custom_nodes_count'] = len(custom_nodes)
    (release / 'manifest.json').write_text(json.dumps(manifest, indent=2))

    (private / 'ACCESS.md').write_text(
        f'# 路由器 Mihomo 管理\n\n'
        f'面板：http://{lan_ip}:9090/ui/\n\n'
        f'后端地址：http://{lan_ip}:9090\n\n'
        f'API 密钥：`{keys["api"]}`\n\n'
        f'手动 HTTP / SOCKS5 代理：{lan_ip}:7890\n\n'
        f'用户名：`router`；密码：`{keys["proxy"]}`\n\n'
        f'透明代理不需要客户端填写此密码。仅管理 LAN 可以访问上述端口。\n'
    )

    with tarfile.open(ROOT / 'build' / 'release.tgz', 'w:gz') as archive:
        archive.add(release, arcname='mihomo')

    print("\n[Build Success]")
    print(json.dumps({
        'proxies': len(config.get('proxies', [])),
        'custom_nodes': len(custom_nodes),
        'groups': len(config.get('proxy-groups', [])),
        'rules': len(config.get('rules', [])),
        'bundle_bytes': (ROOT / 'build' / 'release.tgz').stat().st_size
    }, indent=2))

if __name__ == '__main__':
    main()
