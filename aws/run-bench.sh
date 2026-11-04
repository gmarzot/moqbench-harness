#!/bin/bash
# Drive one split-host aiomoqt run. Generates a unique namespace per run so the
# relay never sees a re-registration of a track it still holds.
#
# usage: run-bench.sh <mbps> <objsize> <start> <step> <max> <slo_ms> <dur_s> [join] [stagger]
set -euo pipefail

KEY=${KEY:-$HOME/.ssh/id_ed25519}
SSHO="-n -i $KEY -o StrictHostKeyChecking=no -o ConnectTimeout=10"
RELAY_PUB=${RELAY_PUB:?set RELAY_PUB, the relay public address}
RELAY_PRIV=${RELAY_PRIV:?set RELAY_PRIV, the relay private address}
CLIENT=${CLIENT:?set CLIENT, the load host public address}

MBPS="${1:-5}"
OBJSZ="${2:-20800}"
START="${3:-100}"
STEP="${4:-100}"
MAX="${5:-15000}"
SLO="${6:-512}"
DUR="${7:-600}"
JOIN="${8:-2}"
STAGGER="${9:-0.01}"

RUNID="$(date +%H%M%S)"
NS="gmbench${RUNID}"
TRACK="v${MBPS}"

echo "=== run $RUNID: ns=$NS track=$TRACK ${MBPS}Mbps obj=${OBJSZ}B max=${MAX} slo=${SLO}ms dur=${DUR}s ==="

echo "--- starting publisher ---"
timeout 60 ssh $SSHO ubuntu@$RELAY_PUB "~/gmarzot-bench/pub.sh $RELAY_PRIV $MBPS $OBJSZ $((DUR + 120)) $NS $TRACK" 2>&1 | tail -3

echo "--- starting subscriber ramp ---"
timeout 120 ssh $SSHO ubuntu@$CLIENT "~/gmarzot-bench/sub.sh $RELAY_PRIV $MBPS $OBJSZ $START $STEP $MAX $SLO $DUR $JOIN $STAGGER $NS $TRACK" 2>&1 | tail -16

echo "=== run $RUNID launched; tag=$NS ==="
