#!/usr/bin/env bash
# Capacity matrix across a cross-region host pair.
#
# Set RELAY_HOST and CLIENT_HOSTS to test hosts in separate regions. Measure the
# actual path capacity and RTT first; no deployment is built in. Check the
# egress pricing before running.
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
source ./lib/dotenv.sh
export RELAY_HOST=${RELAY_HOST:?Set RELAY_HOST to the selected relay address}
export RPRIV_HOST=$RELAY_HOST          # no shared private network across regions
export CLIENT_HOSTS=${CLIENT_HOSTS:?Set CLIENT_HOSTS to the selected client addresses}
export RUSER=ubuntu RROOT=/home/ubuntu/Projects
export MOQX_SEND=48 MOQX_RECV=64 MOQX_CC=${MOQX_CC:-bbr2} DEV_CC=${DEV_CC:-delay}
export CLIENT_THREADS=${CLIENT_THREADS:-24}

# A 40-60s confirm proved optimistic on Linode: a load that passed it drifted
# 2.7 -> 1.8 Gbps over several minutes. Confirm for minutes instead.
SEARCH_DUR=${SEARCH_DUR:-45} CONFIRM_DUR=${CONFIRM_DUR:-180} \
THREADS=${THREADS:-24} SLO_PCT=${SLO_PCT:-90} SLO_MS=${SLO_MS:-512} \
  OUTDIR=${OUTDIR:-results/oci-xregion-$(date +%Y%m%d-%H%M%S)} ./bin/relay-capacity.sh --matrix
