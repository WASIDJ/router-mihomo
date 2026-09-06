"""Node management: Airport subscription fetching and custom node (HY2) parsing/injection."""
import gzip
from pathlib import Path
from typing import Any, Dict, List, Tuple
import urllib.parse
import urllib.request
import yaml


def parse_hy2_url(url: str) -> Dict[str, Any]:
    """Parse a hysteria2:// or hy2:// URL into a Mihomo proxy dictionary."""
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("hysteria2", "hy2"):
        raise ValueError(f"Unsupported scheme '{parsed.scheme}', expected hysteria2:// or hy2://")
    
    name = urllib.parse.unquote(parsed.fragment) if parsed.fragment else f"HY2-{parsed.hostname}"
    query = urllib.parse.parse_qs(parsed.query)
    
    password = urllib.parse.unquote(parsed.username or parsed.password or "")
    if not password and parsed.netloc and "@" in parsed.netloc:
        auth_part = parsed.netloc.split("@")[0]
        password = urllib.parse.unquote(auth_part)
        
    node: Dict[str, Any] = {
        "name": name,
        "type": "hysteria2",
        "server": parsed.hostname,
        "port": parsed.port or 443,
        "password": password,
    }
    
    if "sni" in query:
        node["sni"] = query["sni"][0]
    if "insecure" in query:
        node["skip-cert-verify"] = query["insecure"][0] in ("1", "true", "True")
    if "obfs" in query:
        node["obfs"] = query["obfs"][0]
    if "obfs-password" in query:
        node["obfs-password"] = query["obfs-password"][0]
    if "alpn" in query:
        node["alpn"] = query["alpn"]
    if "up" in query:
        node["up"] = query["up"][0]
    if "down" in query:
        node["down"] = query["down"][0]
    if "ports" in query:
        node["ports"] = query["ports"][0]
        
    return node


def load_custom_nodes(file_path: Path) -> List[Dict[str, Any]]:
    """Load custom nodes from a YAML file (supports both YAML objects and URI strings)."""
    if not file_path.exists():
        return []
    
    raw = yaml.safe_load(file_path.read_text())
    if not raw:
        return []
    
    if isinstance(raw, dict):
        raw = raw.get("proxies") or raw.get("custom_nodes") or []
        
    if not isinstance(raw, list):
        raise ValueError(f"Invalid custom nodes format in {file_path}; expected a list")
        
    nodes = []
    for item in raw:
        if isinstance(item, str):
            item = item.strip()
            if item.startswith(("hysteria2://", "hy2://")):
                nodes.append(parse_hy2_url(item))
            elif item.startswith("#") or not item:
                continue
            else:
                raise ValueError(f"Unsupported node URI format: {item}")
        elif isinstance(item, dict):
            if "name" not in item or "type" not in item:
                raise ValueError(f"Custom node missing required 'name' or 'type': {item}")
            nodes.append(dict(item))
        else:
            continue
            
    return nodes


def fetch_airport_subscription(url: str, cache_file: Path) -> Dict[str, Any]:
    """Fetch airport subscription from URL and cache to file. Returns parsed YAML dict."""
    print(f"Fetching airport subscription from: {url}")
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ClashMeta/1.19.30 mihomo/1.19.30 clash.meta",
            "Accept": "*/*",
        }
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        content = resp.read()
        
    if content.startswith(b"\x1f\x8b"):
        content = gzip.decompress(content)
        
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("latin1")
        
    data = yaml.safe_load(text)
    if not isinstance(data, dict) or "proxies" not in data:
        raise ValueError("Subscription did not return a valid Clash/Mihomo YAML config with 'proxies'")
        
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(text)
    print(f"Saved airport subscription to {cache_file} ({len(data.get('proxies', []))} proxies)")
    return data


def inject_custom_nodes(config: Dict[str, Any], custom_nodes: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], int]:
    """
    Inject custom nodes into config:
    1. Append custom nodes to config['proxies'].
    2. Create a dedicated '⚡ 自建节点' selector group.
    3. Prepend custom nodes to all relevant selector/fallback/url-test groups.
    """
    if not custom_nodes:
        return config, 0
        
    config = dict(config)
    proxies = list(config.get("proxies", []))
    
    custom_names = [n["name"] for n in custom_nodes]
    proxies = [p for p in proxies if p.get("name") not in custom_names]
    proxies.extend(custom_nodes)
    config["proxies"] = proxies
    
    proxy_groups = list(config.get("proxy-groups", []))
    
    custom_group_name = "⚡ 自建节点"
    proxy_groups = [g for g in proxy_groups if g.get("name") != custom_group_name]
    custom_group = {
        "name": custom_group_name,
        "type": "select",
        "proxies": list(custom_names)
    }
    proxy_groups.insert(0, custom_group)
    
    injected_groups_count = 0
    for group in proxy_groups:
        if group["name"] == custom_group_name:
            continue
        gtype = group.get("type")
        if gtype in ("select", "fallback", "url-test", "load-balance"):
            g_proxies = group.get("proxies")
            if isinstance(g_proxies, list):
                if set(g_proxies) <= {"DIRECT", "REJECT", "no-resolve"}:
                    continue
                for name in reversed(custom_names):
                    if name not in g_proxies:
                        g_proxies.insert(0, name)
                injected_groups_count += 1
                
    config["proxy-groups"] = proxy_groups
    return config, injected_groups_count
