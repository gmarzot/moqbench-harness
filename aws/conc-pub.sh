#!/bin/bash
# Is the ~860 Mbps single-publisher ceiling per-connection or per-host?
# Run N publishers concurrently and sum achieved bitrate.
#   per-connection limit -> aggregate scales with N
#   host/link limit      -> aggregate stays flat, each gets ~ceiling/N
BENCH="$HOME/gmarzot-bench"
RELAY="${1:?usage: $0 <relay-host> [args]}"
OBJ="${2:-32768}"
SECS="${3:-20}"

printf "%-6s %-12s %-12s %-10s\n" "N" "aggregate" "per-pub avg" "host_cpu%"
for N in 1 2 4 8; do
  rm -f /tmp/cp-*.log
  for i in $(seq "$N"); do
    "$BENCH/venv/bin/python" -u -m aiomoqt.tools.pub_bench \
      "moqt://${RELAY}:4533/moq-relay" -k -s "$OBJ" -P 4 -r 0 \
      -t "$SECS" --forward 1 >/tmp/cp-$i.log 2>&1 &
  done
  sleep $((SECS / 2))
  IDLE=$(top -bn2 -d 2 2>/dev/null | grep -E "^%Cpu" | tail -1 | grep -oE "[0-9.]+ id" | grep -oE "[0-9.]+")
  wait
  TOTAL=0
  for i in $(seq "$N"); do
    MB=$(grep -E "^  Sent:" /tmp/cp-$i.log | grep -oE "[0-9.]+Mbps" | grep -oE "[0-9.]+")
    GB=$(grep -E "^  Sent:" /tmp/cp-$i.log | grep -oE "[0-9.]+Gbps" | grep -oE "[0-9.]+")
    [ -n "$GB" ] && MB=$(echo "$GB * 1000" | bc)
    TOTAL=$(echo "${TOTAL} + ${MB:-0}" | bc)
  done
  AVG=$(echo "scale=1; $TOTAL / $N" | bc)
  BUSY=$(echo "scale=1; 100 - ${IDLE:-0}" | bc)
  printf "%-6s %-12s %-12s %-10s\n" "$N" "${TOTAL} Mbps" "${AVG} Mbps" "${BUSY}"
done
