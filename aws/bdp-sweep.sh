#!/bin/bash
# Throughput vs BDP for a single aiopquic connection, no relay.
# netem goes on the subscriber's egress (delays ACKs only), so the sender sees
# the inflated RTT without us touching the publisher box. fq_codel stays as the
# child qdisc so netem's own queue isn't the thing under test.
#
# usage: bdp-sweep.sh <pubserver_ip> [inflight_bytes]
BENCH="$HOME/gmarzot-bench"
PUB="${1:?pubserver ip}"
INFLIGHT="${2:-}"
IF=$(ip route show default | awk '{print $5; exit}')

restore() {
  tc qdisc del dev "$IF" root 2>/dev/null
  tc qdisc add dev "$IF" root fq_codel limit 200000 flows 65536 2>/dev/null
}
trap restore EXIT

printf "%-8s %-10s %-12s %-10s %-10s %-10s\n" "rtt_ms" "bitrate" "bdp" "p50_ms" "p99_ms" "loss%"
for D in 0 5 25 50; do
  tc qdisc del dev "$IF" root 2>/dev/null
  if [ "$D" -gt 0 ]; then
    tc qdisc add dev "$IF" root handle 1: netem delay "${D}ms" limit 200000
    tc qdisc add dev "$IF" parent 1: handle 2: fq_codel limit 200000 flows 65536
  else
    tc qdisc add dev "$IF" root fq_codel limit 200000 flows 65536
  fi

  ARGS=("moqt://${PUB}:4444" -N aiomoqt -t 25 -i 30 --draft 16 -k)
  [ -n "$INFLIGHT" ] && ARGS+=(--max-inflight-bytes "$INFLIGHT")

  OUT=$("$BENCH/venv/bin/python" -u -m aiomoqt.tools.sub_bench "${ARGS[@]}" 2>&1)
  BR=$(echo "$OUT"  | awk '/Throughput:/{print $2" "$3}')
  P50=$(echo "$OUT" | grep -oE "p50=[0-9.]+" | cut -d= -f2)
  P99=$(echo "$OUT" | grep -oE "p99=[0-9.]+" | cut -d= -f2)
  LOSS=$(echo "$OUT"| awk '/Lost:/{print $3}' | tr -d '()%')
  MBPS=$(echo "$BR" | grep -oE "^[0-9.]+")
  BDP=$(echo "scale=2; ${MBPS:-0} * $D / 8 / 1000" | bc 2>/dev/null)
  printf "%-8s %-10s %-12s %-10s %-10s %-10s\n" \
    "$D" "${MBPS:-n/a}" "${BDP:-0} MB" "${P50:-n/a}" "${P99:-n/a}" "${LOSS:-n/a}"
done
