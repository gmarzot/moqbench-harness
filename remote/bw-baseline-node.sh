#!/usr/bin/env bash
# Path measurements from one node to the relay. Runs on the node, fed by
# bin/bw-baseline.sh; writes nothing to stdout but a tar of the results.
#
#   ssh node 'bash -s' -- <relay private IP> <first port> <udp procs> <seconds> <pair|concurrent> \
#     [start epoch] < remote/bw-baseline-node.sh | tar xzf - -C <dir>
#
# pair: TCP with 1 and 8 streams, UDP 1400 B with 1 and N processes, each in
#   both directions (-R is relay->node), plus ping.
# concurrent: relay->node only, TCP 8 streams then UDP with N processes. Run on
#   every node at once, it loads the relay's egress the way fan-out does. With a
#   start epoch (the relay's clock; hosts are NTP-synced) each phase begins at an
#   agreed second on every node, so the senders overlap instead of drifting.
#
# N UDP processes because iperf3 3.9 (Ubuntu 22.04) is single-threaded: one
# 1400 B UDP stream measures the sending core, not the path.
set -u
R=$1 BASE=$2 N=$3 DUR=$4 MODE=$5 START_AT=${6:-0} RATE=${7:-0}   # RATE: UDP -b per process, 0 = unlimited
WIN=${8:-0}   # UDP socket buffer (iperf3 -w, both ends), 0 = system default
UDPW=(); [[ $WIN != 0 ]] && UDPW=(-w "$WIN")
until_epoch() { while (( $(date +%s) < $1 )); do sleep 0.1; done; }
D=$(mktemp -d /tmp/bw-baseline.XXXXXX)

if ! command -v iperf3 >/dev/null; then
  echo "{\"error\": \"iperf3 not installed on $(hostname); cloud-init still running?\"}" > "$D/setup.json"
else
  one() {  # <name> <iperf3 args...>
    local name=$1; shift
    iperf3 -J "$@" > "$D/$name.json" 2> "$D/$name.err"
  }
  udpn() {  # <name> [extra args]: N UDP processes at once, one relay port each
    local name=$1 j; shift
    for (( j = 0; j < N; j++ )); do
      one "$name-$j" -c "$R" -p $(( BASE + j )) -t "$DUR" -u -b "$RATE" -l 1400 ${UDPW[@]+"${UDPW[@]}"} "$@" &
    done
    wait
  }
  if [[ $MODE == pair ]]; then
    ping -c 20 -i 0.2 -q "$R" > "$D/ping.txt" 2>&1
    for dir in up down; do
      rev=(); [[ $dir == down ]] && rev=(-R)
      one "tcp1-$dir" -c "$R" -p "$BASE" -t "$DUR" "${rev[@]}"
      one "tcp8-$dir" -c "$R" -p "$BASE" -t "$DUR" -P 8 "${rev[@]}"
      one "udp1-$dir" -c "$R" -p "$BASE" -t "$DUR" -u -b "$RATE" -l 1400 ${UDPW[@]+"${UDPW[@]}"} "${rev[@]}"
      udpn "udp$N-$dir" "${rev[@]}"
    done
  else
    until_epoch "$START_AT"
    one "tcp8-down" -c "$R" -p "$BASE" -t "$DUR" -P 8 -R
    (( START_AT > 0 )) && until_epoch $(( START_AT + DUR + 5 ))
    udpn "udp$N-down" -R
  fi
fi
tar czf - -C "$D" .
rm -rf "$D"
