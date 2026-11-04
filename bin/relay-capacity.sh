#!/usr/bin/env bash
# Max healthy subscribers per relay, found by search on offered load.
#
#   ./bin/relay-capacity.sh moqx                 # one relay, fanout flavour
#   ./bin/relay-capacity.sh --matrix             # all relays x both flavours
#   FLAVOUR=bandwidth ./bin/relay-capacity.sh cf
#   THREADS=16 ./bin/relay-capacity.sh moqx
#
# Aggregate throughput rewards a relay that accepts every subscriber and serves
# them all badly, so the reported number is gated on health instead: p99 object
# latency within budget, no resets, and the subscriber count actually held.
#
# Two flavours, because they stress different costs:
#   fanout     3788 B  = 0.9 Mbps/sub  - per-subscriber replication cost
#   bandwidth  66000 B = 16 Mbps/sub   - bytes moved, GSO batching
#
# Threads are fixed (scaling.sh varies them); offered load is the variable,
# which is what exercises a relay's ability to spread work across cores.
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
source ./lib/dotenv.sh
# Hosts from the selected testbed, or any machines given as RELAY_HOST,
# RPRIV_HOST and CLIENT_HOSTS (lib/hosts.sh).
source ./lib/hosts.sh
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"

THREADS=${THREADS:-32}
# Tuple field 9 is an inter-object DELAY IN MILLISECONDS despite being named
# objectFrequency (MoQTestServer does `delay(params.objectFrequency)`), so 33
# is ~30 objects/sec. moqperf_test_client hardcodes 33 (kObjectInterval) with
# no flag to override, and moq-dev matches broadcasts by exact path, so any
# other value here would make the publisher's announced namespace disagree
# with what the client subscribes to and only the moq-dev relays would fail.
OBJ_DELAY_MS=${OBJ_DELAY_MS:-33}
OBJ_PER_GROUP=${OBJ_PER_GROUP:-60}
SLO_MS=${SLO_MS:-512}             # native histogram bound, so no interpolation
SLO_PCT=${SLO_PCT:-90}
MAX_RESETS=${MAX_RESETS:-0}
SEARCH_DUR=${SEARCH_DUR:-25}
CONFIRM_DUR=${CONFIRM_DUR:-40}
CLIENT_MAX_BUSY=${CLIENT_MAX_BUSY:-85}
TOL_PCT=${TOL_PCT:-8}             # stop bisecting inside this much of the answer

# Per-subscriber bitrate is otherObjectSize * 8 / (OBJ_DELAY_MS / 1000).
set_flavour() {
  case $1 in
    fanout)    FIRST_OBJ=26516; OTHER_OBJ=3788;  START=${START_OVERRIDE:-500} ;;  # 0.9 Mbps
    bandwidth) FIRST_OBJ=66000; OTHER_OBJ=66000; START=${START_OVERRIDE:-50}  ;;  # 16 Mbps
    *) echo "unknown flavour: $1" >&2; exit 2 ;;
  esac
}

source ./lib/run.sh

# Verdict for one subscriber count. Rows go to the probe log either way, since
# the failures are as interesting as the answer.
probe() {
  local cfg=$1 subs=$2 dur=$3 row status
  row=$(run_point "$cfg" "$THREADS" "$subs" "$dur")
  echo "$row" >> "$LOG"
  status=${row##*,}
  case $status in
    ok)           echo "healthy $row" ;;
    CLIENT_BOUND) echo "invalid $row" ;;
    *)            echo "$status $row" ;;
  esac
}

search() {
  local cfg=$1
  local lo=0 hi=0 best=0 bestrow="" at=$START out verdict

  # The starting guess can be too high as easily as too low, and reporting 0
  # for "the first probe failed" hides a working relay behind a bad guess.
  # Halve until something passes, then climb from there.
  while :; do
    out=$(probe "$cfg" "$at" "$SEARCH_DUR"); verdict=${out%% *}
    printf '    %7d subs  %s\n' "$at" "$verdict" >&2
    [[ $verdict == invalid ]] && { echo "CLIENT_BOUND,"; return; }
    [[ $verdict == healthy ]] && break
    hi=$at; at=$(( at / 2 ))
    if (( at < 10 )); then echo "0,${out#* }"; return; fi
  done
  best=$at; bestrow=${out#* }; lo=$at; at=$(( at * 3 ))

  # Climb until something breaks, so no upper bound has to be guessed. x3 keeps
  # the climb to a few runs without overshooting so far that bisection is long.
  while (( hi == 0 )); do
    out=$(probe "$cfg" "$at" "$SEARCH_DUR"); verdict=${out%% *}
    printf '    %7d subs  %s\n' "$at" "$verdict" >&2
    [[ $verdict == invalid ]] && { echo "CLIENT_BOUND,"; return; }
    if [[ $verdict == healthy ]]; then
      best=$at; bestrow=${out#* }; lo=$at; at=$(( at * 3 ))
    else
      hi=$at; break
    fi
  done
  (( best == 0 )) && { echo "0,${out#* }"; return; }

  while (( hi - lo > lo * TOL_PCT / 100 )); do
    local mid=$(( (lo + hi) / 2 ))
    (( mid <= lo || mid >= hi )) && break
    out=$(probe "$cfg" "$mid" "$SEARCH_DUR"); verdict=${out%% *}
    printf '    %7d subs  %s\n' "$mid" "$verdict" >&2
    if [[ $verdict == healthy ]]; then best=$mid; bestrow=${out#* }; lo=$mid
    else hi=$mid; fi
  done

  # Re-run the answer for longer: a 25s probe can pass on a load that a 40s run
  # would fail, since queues take time to build.
  printf '    confirming %d at %ds\n' "$best" "$CONFIRM_DUR" >&2
  out=$(probe "$cfg" "$best" "$CONFIRM_DUR"); verdict=${out%% *}
  if [[ $verdict == healthy ]]; then bestrow=${out#* }
  else printf '    confirm FAILED (%s) - reporting as unconfirmed\n' "$verdict" >&2; fi
  echo "$best,$bestrow"
}

emit() {
  local cfg=$1 flavour=$2 res=$3
  local best=${res%%,*} row=${res#*,}
  IFS=, read -r _ _ _ mbps _ pcores hcores mpc p99 orate resets held _ hot _ _ st <<< "$row"
  printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
    "$cfg" "$flavour" "$THREADS" "$OBJ_DELAY_MS" "$best" "$mbps" "$pcores" \
    "$hcores" "$mpc" "$p99" "${st:-}" | tee -a "$OUT/capacity.csv"
}

STAMP=$(date +%Y%m%d-%H%M%S)
OUT=${OUTDIR:-results/cap-$STAMP}; mkdir -p "$OUT"
LOG=$OUT/probes.csv
detect_caps
record_versions "$OUT"
echo "$CSV_HEADER" > "$LOG"
echo "config,flavour,threads,obj_delay_ms,max_healthy_subs,mbps,relay_proc_cores,relay_host_cores,mbps_per_core,lat_pctile_ms,confirm" \
  > "$OUT/capacity.csv"

if [[ ${1:-} == --matrix ]]; then
  for fl in ${FLAVOURS:-fanout bandwidth}; do
    set_flavour "$fl"
    for cfg in ${CONFIGS:-moqx cf quinn quiche}; do
      echo "== $cfg / $fl / ${THREADS}t" >&2
      emit "$cfg" "$fl" "$(search "$cfg")"
    done
  done
else
  fl=${FLAVOUR:-fanout}; set_flavour "$fl"
  echo "== ${1:?relay: moqx|cf|quinn|quiche} / $fl / ${THREADS}t" >&2
  emit "$1" "$fl" "$(search "$1")"
fi
echo >&2; echo "wrote $OUT/capacity.csv (every probe in $LOG)" >&2
