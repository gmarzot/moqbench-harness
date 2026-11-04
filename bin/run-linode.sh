#!/usr/bin/env bash
# Drive the capacity matrix against a relay/client pair at an emulated RTT.
#
#   RELAY_HOST=<relay> CLIENT_HOSTS=<client> DELAY_MS=25 ./bin/run-linode.sh   # 50ms RTT
#   RELAY_HOST=<relay> CLIENT_HOSTS=<client> DELAY_MS=50 ./bin/run-linode.sh   # 100ms RTT
#
# Congestion control is each stack's best available BBR, which is NOT the same
# algorithm everywhere: quinn ships BBRv1 only, quiche ships BBRv2 only, mvfst
# has both. Rows must be labelled with the algorithm actually used. CC=cubic
# runs the one posture all four share, as a control.
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
export RELAY_HOST=${RELAY_HOST:?set RELAY_HOST} RPRIV_HOST=${RPRIV_HOST:-$RELAY_HOST}
export CLIENT_HOSTS=${CLIENT_HOSTS:?set CLIENT_HOSTS}
export RUSER=${RUSER:-ubuntu}; export RROOT=${RROOT:-/home/$RUSER/Projects}
export MOQX_SEND=48 MOQX_RECV=64
DELAY_MS=${DELAY_MS:-25}

if [[ ${CC:-bbr} == cubic ]]; then
  export MOQX_CC=cubic DEV_CC=loss
else
  export MOQX_CC=${MOQX_CC:-bbr2} DEV_CC=${DEV_CC:-delay}
fi

DELAY_MS=$DELAY_MS ./bin/testbed-tune.sh --host "$RELAY_HOST" --host "$CLIENT_HOSTS" >/dev/null 2>&1
echo "RTT=$((DELAY_MS*2))ms  moqx_cc=$MOQX_CC  moq_dev_cc=$DEV_CC"

# Longer windows than the sub-millisecond case: at 50-100ms RTT a congestion
# window needs time to grow before steady state means anything.
SEARCH_DUR=${SEARCH_DUR:-40} CONFIRM_DUR=${CONFIRM_DUR:-60} THREADS=${THREADS:-4} \
  OUTDIR=${OUTDIR:-results/linode-rtt$((DELAY_MS*2))-${CC:-bbr}} ./bin/relay-capacity.sh --matrix
