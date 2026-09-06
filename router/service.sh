#!/bin/sh
set -eu
BASE=/jffs/mihomo
RUN=/tmp/mihomo
mkdir -p "$RUN"
umask 077

alive() {
  [ -f "$RUN/core.pid" ] || return 1
  pid=$(cat "$RUN/core.pid")
  [ "$(readlink "/proc/$pid/exe" 2>/dev/null)" = "$RUN/mihomo" ]
}
prepare() {
  if [ ! -x "$RUN/mihomo" ]; then
    gzip -dc "$BASE/artifacts/mihomo.gz" > "$RUN/mihomo.new"
    chmod 700 "$RUN/mihomo.new"
    mv "$RUN/mihomo.new" "$RUN/mihomo"
  fi
  if [ ! -f "$RUN/ui/index.html" ]; then
    mkdir -p "$RUN/ui"
    tar -xzf "$BASE/artifacts/ui.tgz" -C "$RUN/ui"
  fi
  if [ ! -f "$RUN/country.mmdb" ]; then
    gzip -dc "$BASE/artifacts/country.mmdb.gz" > "$RUN/country.mmdb"
  fi
  mkdir -p "$BASE/state"
  ln -sf "$BASE/state/cache.db" "$RUN/cache.db"
  ln -sf "$BASE/config.yaml" "$RUN/config.yaml"
  if [ -x "$BASE/rsi" ]; then
    mkdir -p /tmp/opt/bin
    ln -sf "$BASE/rsi" /tmp/opt/bin/rsi 2>/dev/null || true
  fi
  "$RUN/mihomo" -t -d "$RUN" -f "$BASE/config.yaml" > "$RUN/check.log" 2>&1
}
launch() {
  if [ -f "$RUN/core.log" ]; then mv -f "$RUN/core.log" "$RUN/core.previous.log"; fi
  GOMEMLIMIT=192MiB GOGC=100 "$RUN/mihomo" -d "$RUN" -f "$BASE/config.yaml" > "$RUN/core.log" 2>&1 &
  echo "$!" > "$RUN/core.pid"
}
case "${1:-status}" in
  start)
    exec 9>"$RUN/start.lock"
    flock -x 9
    prepare
    "$BASE/firewall.sh" apply
    if [ -f "$RUN/supervisor.pid" ] && kill -0 "$(cat "$RUN/supervisor.pid")" 2>/dev/null; then exit 0; fi
    nohup "$BASE/service.sh" supervise </dev/null >"$RUN/supervisor.log" 2>&1 9>&- &
    echo "$!" > "$RUN/supervisor.pid"
    cru a MihomoWatchdog '* * * * * /jffs/mihomo/service.sh ensure'
    ;;
  supervise)
    trap 'if alive; then kill "$(cat "$RUN/core.pid")" 2>/dev/null || true; fi; exit 0' TERM INT
    while :; do
      if ! alive; then
        if prepare; then launch; else logger -t mihomo 'Configuration check failed; clients remain guarded'; fi
      fi
      # Logs live only in RAM and are bounded.
      if [ -f "$RUN/core.log" ] && [ "$(wc -c < "$RUN/core.log")" -gt 1048576 ]; then : > "$RUN/core.log"; fi
      sleep 5 & wait "$!" || true
    done
    ;;
  ensure)
    if ! alive; then "$BASE/service.sh" start; fi
    ;;
  stop)
    cru d MihomoWatchdog
    if [ -f "$RUN/supervisor.pid" ]; then
      kill "$(cat "$RUN/supervisor.pid")" 2>/dev/null || true
      rm -f "$RUN/supervisor.pid"
    fi
    if alive; then kill "$(cat "$RUN/core.pid")" 2>/dev/null || true; fi
    # Keep firewall guards: stopping must not change proxy policy to direct.
    ;;
  restart)
    prepare
    "$BASE/service.sh" stop
    sleep 1
    "$BASE/service.sh" start
    ;;
  firewall)
    exec 9>"$RUN/start.lock"
    flock -x 9
    "$BASE/firewall.sh" apply
    ;;
  rollback)
    test -f "$BASE/config.previous.yaml"
    "$RUN/mihomo" -t -d "$RUN" -f "$BASE/config.previous.yaml" > "$RUN/check.log" 2>&1
    cp "$BASE/config.yaml" "$BASE/config.failed.yaml"
    cp "$BASE/config.previous.yaml" "$BASE/config.yaml"
    if [ -f "$BASE/clients.previous.txt" ]; then cp "$BASE/clients.previous.txt" "$BASE/clients.txt"; fi
    "$BASE/service.sh" restart
    ;;
  status)
    if alive; then printf 'Mihomo running PID %s\n' "$(cat "$RUN/core.pid")"; else echo 'Mihomo stopped'; fi
    ipset list mh_clients 2>/dev/null | tail -8
    ;;
  *) echo 'Usage: service.sh start|stop|restart|ensure|firewall|rollback|status' >&2; exit 2;;
esac
