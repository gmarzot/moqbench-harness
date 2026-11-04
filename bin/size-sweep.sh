#!/usr/bin/env bash
# How Mbps-per-core varies with stream bitrate, at a fixed subscriber count.
#
# The capacity search answers "how many subscribers", which conflates per-object
# cost with per-byte cost. Holding subscribers constant and varying only the
# object size separates them: if a relay's cost is dominated by per-object work
# (syscalls, headers, scheduling) then Mbps/core climbs steeply with object
# size; if it is dominated by moving bytes, the curve flattens.
#
# Confirm the selected path can carry the requested offered load before testing.
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
source ./lib/dotenv.sh
source ./lib/hosts.sh
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
export CLIENT_THREADS=${CLIENT_THREADS:-24}
OBJ_DELAY_MS=33 OBJ_PER_GROUP=60
# Gates open: this measures cost, it does not pass or fail a relay.
SLO_MS=100000 SLO_PCT=90 MAX_RESETS=100000000 CLIENT_MAX_BUSY=100 RATE_PCT=0
export MOQX_SEND=48 MOQX_RECV=64 MOQX_CC=bbr2 DEV_CC=delay
source ./lib/run.sh
detect_caps

SUBS=${SUBS:-240}; DUR=${DUR:-45}
echo "relay,obj_bytes,per_sub_mbps,mbps,proc_cores,host_cores,mbps_per_core,p90_ms"
for sz in ${SIZES:-1024 3788 8192 16384 33333 66000}; do
  persub=$(python3 -c "print(f'{$sz*8/($OBJ_DELAY_MS/1000)/1e6:.2f}')")
  for r in ${CONFIGS:-moqx cf quinn quiche}; do
    FIRST_OBJ=$sz OTHER_OBJ=$sz
    row=$(FIRST_OBJ=$sz OTHER_OBJ=$sz run_point "$r" 24 "$SUBS" "$DUR")
    IFS=, read -r _ _ _ mbps _ pc hc mpc p90 _ <<< "$row"
    echo "${LABEL[$r]:-$r},$sz,$persub,$mbps,$pc,$hc,$mpc,$p90"
  done
done
