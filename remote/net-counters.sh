#!/usr/bin/env bash
# Drop and error counters on this host, one "name value" per line. Fed over ssh
# by bin/bw-baseline.sh, which snapshots both ends around each round: the
# difference says where packets were lost. The sender's qdisc or NIC, the
# receiver's NIC ring, its softirq backlog, or its socket buffer each have a
# counter here. Nothing on either host means the network between them dropped
# the packets.
IF=$(ip -o -4 route show to default | awk '{print $5; exit}')
S=/sys/class/net/$IF/statistics
for k in rx_dropped tx_dropped rx_missed_errors rx_fifo_errors; do
  echo "if_$k $(cat "$S/$k" 2>/dev/null || echo 0)"
done
# softnet_stat: one line per CPU, hex; column 2 is backlog drops, column 3
# times the softirq ran out of budget with packets still queued.
d=0 s=0
while read -r _ x y _; do d=$(( d + 0x$x )); s=$(( s + 0x$y )); done < /proc/net/softnet_stat
echo "softnet_dropped $d"
echo "softnet_squeezed $s"
nstat -saz 2>/dev/null | awk '$1 ~ /^Udp(InErrors|RcvbufErrors|SndbufErrors)$/ {print $1, $2}'
# Leaf qdiscs only: an mq root reports its children's drops again.
tc -s qdisc show dev "$IF" | awk '/^qdisc/ {kind = $2}
  /dropped/ && kind != "mq" {for (i = 1; i < NF; i++) if ($i == "dropped") {v = $(i+1); sub(",", "", v); t += v}}
  END {print "qdisc_dropped", t + 0}'
ethtool -S "$IF" 2>/dev/null | awk -F: '$1 ~ /drop|miss|fifo|full|err/ {k = $1; gsub(/[ \t]/, "", k); print "nic_" k, $2 + 0}'
