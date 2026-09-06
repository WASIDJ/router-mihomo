#!/usr/bin/env python3
"""Non-secret API, static asset, DNS and egress checks against the router."""
import concurrent.futures
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent

def load_env() -> dict:
    env_file = ROOT / 'config.env'
    config = {'LAN_IP': '192.168.50.1'}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            config[k.strip()] = v.strip().strip('"').strip("'")
    return config

ENV = load_env()
LAN_IP = ENV.get('LAN_IP', '192.168.50.1')
BASE = f'http://{LAN_IP}:9090'
KEYS = json.loads((ROOT / 'private/credentials.json').read_text())
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def api(path, method='GET', data=None):
    req = urllib.request.Request(
        BASE + path,
        method=method,
        headers={'Authorization': 'Bearer ' + KEYS['api'], 'Content-Type': 'application/json'},
        data=None if data is None else json.dumps(data).encode()
    )
    with HTTP.open(req, timeout=20) as response:
        raw = response.read()
        return json.loads(raw) if raw else response.status

def proxy_curl(url):
    settings = f'proxy = "http://{LAN_IP}:7890"\nproxy-user = "router:' + KEYS['proxy'] + '"\n'
    result = subprocess.run(
        ['curl', '--config', '-', '--noproxy', '', '--silent', '--show-error', '--max-time', '25', url],
        input=settings,
        text=True,
        capture_output=True
    )
    if result.returncode:
        return {'ok': False, 'error': result.stderr.strip()}
    if '/cdn-cgi/trace' in url:
        fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        return {'ok': True, 'location': fields.get('loc'), 'tls': fields.get('tls')}
    return {'ok': True, 'bytes': len(result.stdout)}

class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        value = attrs.get('src') if tag == 'script' else attrs.get('href') if tag == 'link' else None
        if value and not value.startswith(('http:', 'https:', 'data:')):
            self.paths.append(urllib.parse.urljoin(BASE + '/ui/', value))

if __name__ == '__main__':
    print('version:', api('/version'))
    states = api('/proxies')['proxies']
    print('proxy/group entries:', len(states))
    print('rules:', len(api('/rules')['rules']))
    
    # Dynamically find select groups to verify
    test_groups = [
        name for name, info in states.items()
        if info.get('type') == 'Selector' and info.get('now')
    ][:3]
    for name in test_groups:
        state = states[name]
        status = api('/proxies/' + urllib.parse.quote(name, safe=''), 'PUT', {'name': state['now']})
        print('selector API:', name, '->', status)
        
    for authenticated in [False, True]:
        try:
            if authenticated:
                print('authenticated API:', bool(api('/version')))
            else:
                HTTP.open(BASE + '/version', timeout=5)
                raise AssertionError('unauthenticated API accepted')
        except urllib.error.HTTPError as exc:
            assert not authenticated and exc.code == 401
            print('unauthenticated API:', exc.code)
            
    with HTTP.open(BASE + '/ui/', timeout=5) as response:
        html = response.read().decode()
    parser = Assets()
    parser.feed(html)
    for asset in parser.paths:
        with HTTP.open(asset, timeout=10) as response:
            assert response.status == 200 and len(response.read()) > 0
    print('UI HTML/assets:', len(parser.paths), 'passed')
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        urls = ['https://www.cloudflare.com/cdn-cgi/trace', 'https://www.google.com/generate_204', 'https://www.baidu.com/']
        for url, result in zip(urls, pool.map(proxy_curl, urls)):
            print('authenticated mixed proxy:', url, result)
