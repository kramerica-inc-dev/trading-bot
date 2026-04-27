#!/bin/bash
set -euo pipefail
IFACE="eth0"
LOG="/var/log/dhcp-watchdog.log"
EVENTS="/var/log/dhcp-watchdog.events.jsonl"
touch "$LOG" "$EVENTS"
ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
has_ipv4() { ip -4 -o addr show dev "$IFACE" 2>/dev/null | grep -q 'inet '; }
if has_ipv4; then exit 0; fi
WHEN="$(ts)"
logger -t dhcp-watchdog "no IPv4 on $IFACE; running dhclient"
echo "[$WHEN] no IPv4 on $IFACE; running dhclient" >>"$LOG"
if dhclient -v "$IFACE" >>"$LOG" 2>&1 && has_ipv4; then
  IP="$(ip -4 -o addr show dev "$IFACE" | awk '{print $4}')"
  logger -t dhcp-watchdog "recovered: $IP"
  echo "[$(ts)] recovered: $IP" >>"$LOG"
  printf '{"ts":"%s","event":"recovered","ip":"%s"}\n' "$WHEN" "$IP" >>"$EVENTS"
  exit 0
fi
logger -t dhcp-watchdog "FAILED to recover lease"
echo "[$(ts)] FAILED to recover lease" >>"$LOG"
printf '{"ts":"%s","event":"failed"}\n' "$WHEN" >>"$EVENTS"
exit 1
