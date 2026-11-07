#!/usr/bin/env bash
# Long sustained load: one relay, one continuous run, no search. 1280
# subscribers of 8 KB objects (~2 Mbps each) is chosen to sit below the knee
# on an 8-core relay pair, so it holds steady for hours.
#
#   RELAY_HOST=<relay> CLIENT_HOSTS="<client> ..." ./bin/demo-run.sh   # 4 hours at 1280 subs
#   SUBS=960 DUR=7200 ./bin/demo-run.sh
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
KEY=${KEY:-$HOME/.ssh/id_ed25519}
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
RELAY=${RELAY_HOST:?set RELAY_HOST}; RPRIV=${RPRIV_HOST:-$RELAY}
read -r -a CLIENTS <<< "${CLIENT_HOSTS:?set CLIENT_HOSTS}"
export RUSER=${RUSER:-ubuntu}; export RROOT=${RROOT:-/home/$RUSER/Projects}
FIRST_OBJ=26516 OTHER_OBJ=8192 OBJ_DELAY_MS=33 OBJ_PER_GROUP=60
# Gates wide open: this is sustained load, not a measurement. Nothing should
# abort the run early.
SLO_MS=100000 SLO_PCT=90 MAX_RESETS=100000000 CLIENT_MAX_BUSY=100 RATE_PCT=0
export MOQX_SEND=48
source ./lib/run.sh
detect_caps
echo "$CSV_HEADER"
run_point moqx "${THREADS:-6}" "${SUBS:-1280}" "${DUR:-14400}"
