#!/bin/sh
set -eu
BASE=/jffs/mihomo
IPT=iptables
SET=mh_clients
CHN_SET=chnroute
MARK=0x01000000/0x01000000
TABLE=110
PREF=11000
PRIVATE='0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.168.0.0/16 224.0.0.0/4 240.0.0.0/4'

unlink_chain() {
  while $IPT -t "$1" -C "$2" -j "$3" 2>/dev/null; do
    $IPT -t "$1" -D "$2" -j "$3"
  done
}
chain() {
  $IPT -t "$1" -N "$2" 2>/dev/null || true
  $IPT -t "$1" -F "$2"
}
case "${1:-apply}" in
  remove)
    for entry in 'mangle PREROUTING MH_ROUTE' 'nat PREROUTING MH_DNS' 'filter INPUT MH_INPUT' 'filter FORWARD MH_GUARD'; do
      set -- $entry
      unlink_chain "$1" "$2" "$3"
      $IPT -t "$1" -F "$3" 2>/dev/null || true
      $IPT -t "$1" -X "$3" 2>/dev/null || true
    done
    while ip rule del pref "$PREF" fwmark "$MARK" table "$TABLE" 2>/dev/null; do :; done
    ip route del local 0.0.0.0/0 dev lo table "$TABLE" 2>/dev/null || true
    ipset destroy "$SET" 2>/dev/null || true
    ipset destroy "$CHN_SET" 2>/dev/null || true
    ipset destroy chn_next 2>/dev/null || true
    exit 0
    ;;
  update-chnroute)
    echo "Updating chnroute from https://ispip.clang.cn/all_cn.txt..."
    tmp_file="/tmp/chnroute_new.txt"
    if curl -sL --connect-timeout 10 https://ispip.clang.cn/all_cn.txt > "$tmp_file" && [ -s "$tmp_file" ]; then
      lines=$(wc -l < "$tmp_file")
      if [ "$lines" -gt 3000 ]; then
        cp -f "$tmp_file" "$BASE/chnroute.txt"
        rm -f "$tmp_file"
        ipset create chn_next hash:net family inet hashsize 8192 maxelem 65536 -exist
        ipset flush chn_next
        sed 's/^/add chn_next /' "$BASE/chnroute.txt" | ipset restore
        ipset swap chn_next "$CHN_SET"
        ipset destroy chn_next
        fc flush --if br0 >/dev/null 2>&1 || true
        echo "chnroute updated successfully ($lines subnets loaded)."
        exit 0
      fi
    fi
    rm -f "$tmp_file"
    echo "Failed to update chnroute: invalid download" >&2
    exit 1
    ;;
esac

# Kernel TCP / network buffer tuning for high-throughput & BDP scaling
echo 16777216 > /proc/sys/net/core/rmem_max 2>/dev/null || true
echo 16777216 > /proc/sys/net/core/wmem_max 2>/dev/null || true
echo "4096 87380 16777216" > /proc/sys/net/ipv4/tcp_rmem 2>/dev/null || true
echo "4096 65536 16777216" > /proc/sys/net/ipv4/tcp_wmem 2>/dev/null || true
echo 2048 > /proc/sys/net/core/somaxconn 2>/dev/null || true
echo 4096 > /proc/sys/net/core/netdev_max_backlog 2>/dev/null || true
echo 3 > /proc/sys/net/ipv4/tcp_fastopen 2>/dev/null || true

modprobe xt_TPROXY 2>/dev/null || true
modprobe xt_socket 2>/dev/null || true
modprobe xt_set 2>/dev/null || true
modprobe ip_set_hash_net 2>/dev/null || true

ipset create "$SET" hash:net family inet -exist
ipset create "$CHN_SET" hash:net family inet hashsize 8192 maxelem 65536 -exist

ipset create mh_next hash:net family inet -exist
ipset flush mh_next
while read -r client; do
  case "$client" in ''|'#'*) continue;; esac
  # Only LAN client addresses are accepted, never arbitrary shell text.
  case "$client" in 192.168.50.*) ipset add mh_next "$client" -exist;; *) exit 1;; esac
done < "$BASE/clients.txt"
ipset swap mh_next "$SET"
ipset destroy mh_next

# Populate chnroute if file exists and set is currently unpopulated
if [ -f "$BASE/chnroute.txt" ]; then
  entries=$(ipset list "$CHN_SET" -t 2>/dev/null | grep 'Number of entries:' | awk '{print $NF}')
  if [ "${entries:-0}" -lt 1000 ]; then
    ipset create chn_next hash:net family inet hashsize 8192 maxelem 65536 -exist
    ipset flush chn_next
    sed 's/^/add chn_next /' "$BASE/chnroute.txt" | ipset restore
    ipset swap chn_next "$CHN_SET"
    ipset destroy chn_next
  fi
fi

# A persistent FORWARD guard prevents selected TCP/UDP clients silently
# falling back to direct when the proxy exits. Management/private LAN and domestic bypass it.
chain filter MH_GUARD
for net in $PRIVATE; do $IPT -t filter -A MH_GUARD -d "$net" -j RETURN; done
$IPT -t filter -A MH_GUARD -m set --match-set "$CHN_SET" dst -j RETURN
for proto in tcp udp; do
  $IPT -t filter -A MH_GUARD -i br0 -m set --match-set "$SET" src -p "$proto" -j REJECT
done
$IPT -t filter -C FORWARD -j MH_GUARD 2>/dev/null || $IPT -t filter -I FORWARD 1 -j MH_GUARD

chain filter MH_INPUT
$IPT -A MH_INPUT -i br0 -s 192.168.50.0/24 -m mark --mark "$MARK" -j ACCEPT
for proto in tcp udp; do
  $IPT -A MH_INPUT -i lo -p "$proto" -m multiport --dports 7890,7893,9090,1053 -j ACCEPT
  $IPT -A MH_INPUT -i br0 -s 192.168.50.0/24 -p "$proto" -m multiport --dports 7890,7893,9090,1053 -j ACCEPT
  $IPT -A MH_INPUT -p "$proto" -m multiport --dports 7890,7893,9090,1053 -j DROP
done
$IPT -C INPUT -j MH_INPUT 2>/dev/null || $IPT -I INPUT 1 -j MH_INPUT

ip route replace local 0.0.0.0/0 dev lo table "$TABLE"
if ! ip rule show | grep -q '11000:.*fwmark 0x1000000/0x1000000.*lookup 110'; then
  ip rule add pref "$PREF" fwmark "$MARK" table "$TABLE"
fi

chain mangle MH_ROUTE
$IPT -t mangle -A MH_ROUTE ! -i br0 -j RETURN
$IPT -t mangle -A MH_ROUTE -m set ! --match-set "$SET" src -j RETURN
for proto in tcp udp; do $IPT -t mangle -A MH_ROUTE -p "$proto" --dport 53 -j RETURN; done
for net in $PRIVATE; do $IPT -t mangle -A MH_ROUTE -d "$net" -j RETURN; done
# Direct hardware bypass: Domestic traffic directly RETURNs to utilize Broadcom Flow Cache wire-speed acceleration
$IPT -t mangle -A MH_ROUTE -m set --match-set "$CHN_SET" dst -j RETURN
for proto in tcp udp; do
  $IPT -t mangle -A MH_ROUTE -p "$proto" -j TPROXY --on-port 7893 --tproxy-mark "$MARK"
done
$IPT -t mangle -C PREROUTING -j MH_ROUTE 2>/dev/null || $IPT -t mangle -I PREROUTING 1 -j MH_ROUTE

chain nat MH_DNS
for proto in tcp udp; do
  $IPT -t nat -A MH_DNS -i br0 -m set --match-set "$SET" src -p "$proto" --dport 53 -j REDIRECT --to-ports 1053
done
$IPT -t nat -C PREROUTING -j MH_DNS 2>/dev/null || $IPT -t nat -I PREROUTING 1 -j MH_DNS

# Invalidate cached LAN flows so new policy applies to previously accelerated traffic.
fc flush --if br0 >/dev/null 2>&1 || true
