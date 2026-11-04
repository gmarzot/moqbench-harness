#!/bin/bash
# One subscriber measurement at a given emulated RTT, reporting packet rate as
# well as bitrate -- a pps ceiling and a cwnd ceiling look different.
# usage: run-sub.sh <pub_ip> <delay_ms> <secs> [inflight_bytes]
BENCH="$HOME/gmarzot-bench"
PUB="${1:?pub ip}"
D="${2:-0}"
SECS="${3:-40}"
INFLIGHT="${4:-}"
IF=$(ip route show default | awk '{print $5; exit}')

tc qdisc del dev "$IF" root 2>/dev/null
if [ "$D" -gt 0 ]; then
  tc qdisc add dev "$IF" root handle 1: netem delay "${D}ms" limit 200000
  tc qdisc add dev "$IF" parent 1: handle 2: fq_codel limit 200000 flows 65536
else
  tc qdisc add dev "$IF" root fq_codel limit 200000 flows 65536
fi

RX0=$(awk -v i="$IF" '$1 ~ i":" {print $3}' /proc/net/dev)
ARGS=("moqt://${PUB}:4444" -N aiomoqt -t "$SECS" -i 60 --draft 16 -k)
[ -n "$INFLIGHT" ] && ARGS+=(--max-inflight-bytes "$INFLIGHT")
OUT=$("$BENCH/venv/bin/python" -u -m aiomoqt.tools.sub_bench "${ARGS[@]}" 2>&1)
RX1=$(awk -v i="$IF" '$1 ~ i":" {print $3}' /proc/net/dev)

MBPS=$(echo "$OUT" | awk '/Throughput:/{print $2}')
P50=$(echo "$OUT"  | grep -oE "p50=[0-9.]+" | cut -d= -f2)
P99=$(echo "$OUT"  | grep -oE "p99=[0-9.]+" | cut -d= -f2)
PPS=$(echo "scale=0; ($RX1 - $RX0) / $SECS" | bc)
echo "${MBPS:-n/a} ${PPS:-n/a} ${P50:-n/a} ${P99:-n/a}"

tc qdisc del dev "$IF" root 2>/dev/null
tc qdisc add dev "$IF" root fq_codel limit 200000 flows 65536 2>/dev/null
