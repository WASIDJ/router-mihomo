#!/usr/bin/env python3
"""Test LAN traffic without an explicit proxy, including UDP STUN."""
import concurrent.futures
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import time

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

def dns(name, tcp=False):
    command = ['dig', f'@{LAN_IP}', name, 'A', '+short', '+time=3', '+tries=1']
    if tcp:
        command.append('+tcp')
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    return next(line for line in result.stdout.splitlines() if line and line[0].isdigit())

def fetch(host, path):
    ip = dns(host)
    command = ['curl', '--noproxy', '*', '--resolve', f'{host}:443:{ip}', '--max-time', '25', '--silent', '--show-error', '-o', '/dev/null', '-w', '%{http_code} %{time_total} %{speed_download}', f'https://{host}{path}']
    result = subprocess.run(command, capture_output=True, text=True)
    return {'host': host, 'dns': ip, 'exit': result.returncode, 'http_seconds_bytes_per_second': result.stdout, 'error': result.stderr.strip()}

if __name__ == '__main__':
    print('DNS over UDP:', dns('www.google.com'), flush=True)
    print('DNS over TCP:', dns('www.google.com', True), flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        jobs = [('www.google.com', '/generate_204'), ('www.baidu.com', '/'), ('www.cloudflare.com', '/cdn-cgi/trace')]
        for result in pool.map(lambda args: fetch(*args), jobs):
            print(json.dumps(result), flush=True)
    transaction = os.urandom(12)
    packet = struct.pack('!HHI', 1, 0, 0x2112A442) + transaction
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(8)
        started = time.monotonic()
        sock.sendto(packet, (dns('stun.l.google.com'), 19302))
        response, _ = sock.recvfrom(2048)
        assert response[8:20] == transaction and response[:2] == b'\x01\x01', 'Invalid STUN response'
        print('UDP transparent STUN: success; milliseconds:', round((time.monotonic() - started) * 1000), flush=True)
