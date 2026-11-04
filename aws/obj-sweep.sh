#!/bin/bash
# Is the single-publisher ceiling per-object or per-byte? Hold streams fixed,
# vary object size. Flat obj/s with rising bitrate = per-object cost.
BENCH="$HOME/gmarzot-bench"
RELAY="${1:?usage: $0 <relay-host> [args]}"
P="${2:-4}"
SECS="${3:-15}"

printf "%-10s %-10s %-10s %-10s\n" "objsize" "bitrate" "obj/s" "pub_cpu%"
for S in 8192 16384 32768 65536 131072 262144; do
  "$BENCH/venv/bin/python" -u -m aiomoqt.tools.pub_bench \
    "moqt://${RELAY}:4533/moq-relay" -k -s "$S" -P "$P" -r 0 \
    -t "$SECS" --forward 1 >/tmp/osweep-$S.log 2>&1 &
  PID=$!
  sleep 5
  CPU=$(top -bn2 -d 2 -p "$PID" 2>/dev/null | tail -1 | awk '{print $9}')
  wait $PID 2>/dev/null
  LINE=$(grep -E "^  Sent:" /tmp/osweep-$S.log | tail -1)
  BR=$(echo "$LINE" | grep -oE "[0-9.]+[MG]bps" | tail -1)
  OPS=$(echo "$LINE" | grep -oE "[0-9]+/s" | tail -1)
  printf "%-10s %-10s %-10s %-10s\n" "$S" "${BR:-n/a}" "${OPS:-n/a}" "${CPU:-n/a}"
done
