#!/bin/bash
# Loopback control: same stack, no NIC, 64 KB MTU. If the wire ceiling is
# packet-rate then loopback should blow past it; if it's Python per-object
# cost then obj/s should land at the same place as on the wire.
BENCH="$HOME/gmarzot-bench"
SECS="${1:-20}"
CERTS="$BENCH/src/aiomoqt/certs"

printf "%-10s %-12s %-12s %-10s %-10s\n" "objsize" "Mbps" "obj/s" "p50_ms" "p99_ms"
for S in 4096 8192 16384 32768 65536 131072; do
  pkill -f aiomoqt.tools.pub_server 2>/dev/null
  sleep 1
  setsid "$BENCH/venv/bin/python" -u -m aiomoqt.tools.pub_server \
    --quic --bind 127.0.0.1 -p 4445 -s "$S" -P 4 -r 0 -g 10000 \
    --cert "$CERTS/cert.pem" --key "$CERTS/key.pem" \
    >/tmp/lpub-$S.log 2>&1 </dev/null &
  sleep 4

  OUT=$("$BENCH/venv/bin/python" -u -m aiomoqt.tools.sub_bench \
        moqt://127.0.0.1:4445 -N aiomoqt -t "$SECS" -i 60 --draft 16 -k 2>&1)
  MBPS=$(echo "$OUT" | awk '/Throughput:/{print $2}')
  OPS=$(echo "$OUT"  | awk '/ObjRate:/{print $2}')
  P50=$(echo "$OUT"  | grep -oE "p50=[0-9.]+" | cut -d= -f2)
  P99=$(echo "$OUT"  | grep -oE "p99=[0-9.]+" | cut -d= -f2)
  printf "%-10s %-12s %-12s %-10s %-10s\n" \
    "$S" "${MBPS:-n/a}" "${OPS:-n/a}" "${P50:-n/a}" "${P99:-n/a}"
done
pkill -f aiomoqt.tools.pub_server 2>/dev/null
