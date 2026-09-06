#!/bin/sh
case "${1:-start}" in
  stop|kill) /jffs/mihomo/service.sh stop ;;
  start_nat|firewall) /jffs/mihomo/service.sh firewall ;;
  *) /jffs/mihomo/service.sh start ;;
esac
