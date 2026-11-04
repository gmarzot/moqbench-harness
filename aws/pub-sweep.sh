#!/bin/bash
# Single-publisher TX ceiling sweep: vary parallel subgroup streams, measure
# achieved bitrate and publisher CPU. forward=1 means no subscriber is needed.
BENCH="$HOME/gmarzot-bench"
RELAY="${1:?usage: $0 <relay-host> [args]}"
OBJ="${2:-32768}"
SECS="${3:-15}"

printf "%-8s %-10s %-10s %-10s\n" "streams" "bitrate" "obj/s" "pub_cpu%"
for P in 1 2 4 8 16; do
  "$BENCH/venv/bin/python" -u -m aiomoqt.tools.pub_bench \
    "moqt://${RELAY}:4533/moq-relay" -k -s "$OBJ" -P "$P" -r 0 \
    -t "$SECS" --forward 1 >/tmp/sweep-$P.log 2>&1 &
  PID=$!
  sleep 5
  CPU=$(top -bn2 -d 2 -p "$PID" 2>/dev/null | tail -1 | awk '{print $9}')
  wait $PID 2>/dev/null
  LINE=$(grep -E "^  Sent:" /tmp/sweep-$P.log | tail -1)
  BR=$(echo "$LINE" | grep -oE "[0-9.]+[MG]bps" | tail -1)
  OPS=$(echo "$LINE" | grep -oE "[0-9]+/s" | tail -1)
  printf "%-8s %-10s %-10s %-10s\n" "$P" "${BR:-n/a}" "${OPS:-n/a}" "${CPU:-n/a}"
done
