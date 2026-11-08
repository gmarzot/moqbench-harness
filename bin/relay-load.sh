#!/usr/bin/env bash
# One load run against one relay, with any of the load generators.
#
#   ./bin/relay-load.sh <tool> <relay> [duration_s]
#
#   tool   moqperf   moxygen moqtest_server -> moqperf_test_client, one gated
#                    point (relay-capacity.sh searches with the same tool)
#          moqbench  Quicr moq-bench: moqbench_pub -> one moqbench_sub process
#                    per subscriber
#          adaptive  aiomoqt adaptive_bench --mode subs: fixed-rate publisher,
#                    subscriber-side ramp controller on each load host
#          fixed     same aiomoqt subscriber workers, fixed population until duration
#          loadsim   aiomoqt load_sim: scenario-shaped publishers and churning
#                    audiences
#          moqperf-ramp  moqtest_server on the publishing host, a moqperf_test_client
#                    ramp on every subscriber host, objects sized by SHAPE
#   relay  moqdev | moqx | cf | quinn | quiche | aiomoqt, or one relays.local.sh defines
#
# Where things run:
#   PUB_HOST=relay   (default) publisher beside the relay, so ingest stays off
#                    the wire — what effective-capacity numbers need
#   PUB_HOST=probe   publish from the probe node, across the network
#   PUB_HOST=client[:N]  publish from load host N (1-based, default 1), which
#                    still subscribes
#   SUB_HOSTS=clients (default) subscribers split over every load generator
#   SUB_HOSTS=client:N  subscribers on load host N only (1-based)
#   SUB_HOSTS=probe  subscribers on the probe only, for light probing
#
# Video shape (adaptive, fixed, moqperf-ramp): SHAPE=480p|720p|1080p|2160p from
# scenarios/shapes.json; adaptive and fixed then publish aiomoqt's --video
# profile instead of SUB_MBPS/OBJ_SIZE. LATENCY_MS (500) and SHORTFALL (tool
# default) set adaptive_bench's back-off thresholds.
#
# MoQT draft 16 by default. DRAFT=18 runs on moqx, moqdev (current main has no
# draft 16) and cf's draft-18-dev build (./bin/relay-build.sh), and needs a
# draft-18 moq-bench (MOQBENCH_DRAFT=18 ./bin/testbed-provision.sh).
#
# Output: results/load-<tool>-<relay>-<stamp>/ — relay and publisher logs, a log
# per subscriber host, the tool's own reports, cpu.txt and versions.txt.
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
source ./lib/dotenv.sh
source ./lib/hosts.sh
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
SCP="scp -q -i $KEY -o StrictHostKeyChecking=accept-new"

TOOL=${1:?tool: moqperf|moqperf-ramp|moqbench|adaptive|fixed|loadsim}
CFG=${2:?relay: moqdev|moqx|cf|quinn|quiche|aiomoqt}
DUR=${3:-${DUR:-60}}
# Existing mode measures a separately managed relay without restarting it.
EXISTING_RELAY=${EXISTING_RELAY:-0}
RELAY_PORT=${RELAY_PORT:-4533}
RELAY_UNIT=${RELAY_UNIT:-}
[[ $EXISTING_RELAY == 0 || $EXISTING_RELAY == 1 ]] || { echo 'EXISTING_RELAY must be 0 or 1' >&2; exit 2; }
[[ $RELAY_PORT =~ ^[0-9]+$ ]] && (( RELAY_PORT >= 1 && RELAY_PORT <= 65535 )) || { echo 'invalid RELAY_PORT' >&2; exit 2; }
[[ -z $RELAY_UNIT || $RELAY_UNIT =~ ^[A-Za-z0-9_.@-]+$ ]] || { echo 'invalid RELAY_UNIT' >&2; exit 2; }
if [[ $EXISTING_RELAY == 0 && $RELAY_PORT != 4533 ]]; then
  echo 'Custom RELAY_PORT requires EXISTING_RELAY=1' >&2; exit 2
fi
if [[ $EXISTING_RELAY == 1 && $TOOL == moqperf ]]; then
  echo 'Existing relay mode supports adaptive, fixed, loadsim and moqbench' >&2; exit 2
fi
RELAY_CPUS=${RELAY_CPUS:-}     # optional Linux CPU list, inherited at startup
THREADS=${THREADS:-8}          # relay worker threads; the 8 OCPU relay has 16 vCPU
SUBS=${SUBS:-200}              # total subscribers, split over the sub hosts
DRAFT=${DRAFT:-16}
PUB_HOST=${PUB_HOST:-relay}
SUB_HOSTS=${SUB_HOSTS:-clients}
STAGGER=${STAGGER:-0.02}       # seconds between subscriber joins on one host
SHAPE=${SHAPE:-}               # 480p | 720p | 1080p | 2160p, scenarios/shapes.json
LATENCY_MS=${LATENCY_MS:-500}  # adaptive_bench --latency-threshold
SHORTFALL=${SHORTFALL:-}       # adaptive_bench --shortfall-ratio; unset = tool default
source ./lib/shapes.sh
if [[ -n $SHAPE ]]; then shape_field "$SHAPE" nominal_mbps >/dev/null || exit 2; fi

case $PUB_HOST in
  relay) PUB=$RELAY;   PUB_ADDR=127.0.0.1 ;;
  probe) PUB=${PROBES[0]:?no probe host in this environment}; PUB_ADDR=$RPRIV ;;
  client|client:[1-9]*)
    n=${PUB_HOST#client}; n=${n#:}; n=${n:-1}
    [[ $n =~ ^[0-9]+$ ]] && (( n <= ${#CLIENTS[@]} )) || { echo "PUB_HOST=$PUB_HOST: there are ${#CLIENTS[@]} load hosts" >&2; exit 2; }
    PUB=${CLIENTS[n-1]}; PUB_ADDR=$RPRIV ;;
  *) echo "PUB_HOST must be relay, probe or client[:N]" >&2; exit 2 ;;
esac
case $SUB_HOSTS in
  clients) SUBHOSTS=("${CLIENTS[@]}") ;;
  client:[1-9]*)
    n=${SUB_HOSTS#client:}
    [[ $n =~ ^[0-9]+$ ]] && (( n <= ${#CLIENTS[@]} )) || { echo "SUB_HOSTS=$SUB_HOSTS: there are ${#CLIENTS[@]} load hosts" >&2; exit 2; }
    SUBHOSTS=("${CLIENTS[n-1]}") ;;
  probe)   SUBHOSTS=("${PROBES[0]:?no probe host in this environment}") ;;
  *) echo "SUB_HOSTS must be clients, client:N or probe" >&2; exit 2 ;;
esac

if [[ $DRAFT != 16 ]]; then
  [[ $DRAFT == 18 && $CFG =~ ^(moqx|moqdev|cf|aiomoqt)$ ]] || { echo "DRAFT=$DRAFT: only moqx, moqdev, cf and aiomoqt are set up for it here" >&2; exit 2; }
  [[ $CFG != moqx ]] || export MOQX_VERSIONS=16,18
fi

source ./lib/run.sh
if [[ $EXISTING_RELAY == 1 ]]; then
  # In this mode cleanup owns load-generator process groups only.
  stop_all() {
    local h
    for h in "$PUB" "${SUBHOSTS[@]}"; do
      timeout 30 $SSH -n "$RUSER@$h" "$KILL_LOADGEN; true" >/dev/null 2>&1
    done
  }
fi
[[ -n ${PROC[$CFG]:-} ]] || { echo "unknown relay: $CFG" >&2; exit 2; }
URLPATH=${PATHS[$CFG]}
OUT=${OUTDIR:-results/load-$TOOL-$CFG-$(date +%Y%m%d-%H%M%S)}
mkdir -p "$OUT"

# The moqperf path is relay-capacity.sh's single point, gate and all, so the two
# tools never disagree about what a point means.
if [[ $TOOL == moqperf ]]; then
  [[ -z $RELAY_CPUS ]] || { echo "RELAY_CPUS unsupported for moqperf" >&2; exit 2; }
  [[ $PUB_HOST == relay && $SUB_HOSTS == clients ]] || { echo "moqperf runs publisher-on-relay, clients-only (relay-capacity.sh's layout)" >&2; exit 2; }
  FIRST_OBJ=${FIRST_OBJ:-26516} OTHER_OBJ=${OTHER_OBJ:-3788} OBJ_DELAY_MS=33 OBJ_PER_GROUP=60
  SLO_MS=${SLO_MS:-512} SLO_PCT=${SLO_PCT:-90} CLIENT_MAX_BUSY=${CLIENT_MAX_BUSY:-85}
  detect_caps; record_versions "$OUT"
  { echo "$CSV_HEADER"; run_point "$CFG" "$THREADS" "$SUBS" "$DUR"; } | tee "$OUT/point.csv"
  exit 0
fi

# remote_bg <host> <name> <command>: detached in its own process group, logged to
# /tmp/<name>.log, pid in /tmp/<name>.pid, group recorded for stop_all. The
# command is staged through a quoted heredoc so its own quoting survives.
remote_bg() {
  local host=$1 name=$2 cmd=$3
  timeout 60 $SSH -n "$RUSER@$host" "cd $RROOT && ulimit -n 1048576
    cat > /tmp/$name.sh <<'LOADEOF'
(
$cmd
)
status=\$?
printf '%s\\n' \"\$status\" > /tmp/$name.exit
exit \"\$status\"
LOADEOF
    rm -f /tmp/$name.exit
    if [[ $name == loadsub ]]; then rm -f /tmp/loadreport*; fi
    setsid bash /tmp/$name.sh </dev/null >/tmp/$name.log 2>&1 &
    echo \$! > /tmp/$name.pid; echo \$! >> /tmp/loadgen.pgids"
}

# remote_wait <host> <name> <seconds>: until that process exits, or give up.
remote_wait() {
  timeout $(( $3 + 30 )) $SSH -n "$RUSER@$1" \
    "end=\$((SECONDS + $3)); while [[ ! -f /tmp/$2.exit ]] && kill -0 \$(cat /tmp/$2.pid) 2>/dev/null && (( SECONDS < end )); do sleep 2; done" >/dev/null 2>&1
}

# Host and per-core CPU at start and end: a client above ~85% busy is the
# client being measured, not the relay (the Linode lesson).
cpu_snap() {
  local tag=$1 h i=0
  timeout 30 $SSH -n "$RUSER@$RELAY" "bash -c '$CPUSNAP'" -- "${PROC[$CFG]}" > "$OUT/.cpu-$tag-relay" 2>/dev/null &
  for h in "${SUBHOSTS[@]}"; do
    timeout 30 $SSH -n "$RUSER@$h" "bash -c '$CPUSNAP'" -- none > "$OUT/.cpu-$tag-sub$i" 2>/dev/null &
    i=$((i + 1))
  done; wait
}
cpu_report() {
  python3 - "$OUT" "$CFG" <<'PY'
import glob, os, sys
out, cfg = sys.argv[1], sys.argv[2]
def load(p):
    rows = [list(map(int, l.split())) for l in open(p) if l.strip()]
    return rows[0], rows[1:]
for a in sorted(glob.glob(f"{out}/.cpu-a-*")):
    who = a.rsplit("-", 1)[1]
    b = a.replace(".cpu-a-", ".cpu-b-")
    if not (os.path.getsize(a) and os.path.exists(b) and os.path.getsize(b)):
        print(f"{who:8} no sample"); continue
    (t0, i0, c0, n), cores0 = load(a); (t1, i1, c1, _), cores1 = load(b)
    dt = max(t1 - t0, 1)
    busy = 100 * (1 - (i1 - i0) / dt)
    hot = max((100 * (1 - (y[1] - x[1]) / max(y[0] - x[0], 1)) for x, y in zip(cores0, cores1)), default=0)
    proc = "  process CPU: see telemetry-summary.txt (identity checked)" if who == "relay" else ""
    flag = "  <-- client-bound, discard" if who != "relay" and busy > 85 else ""
    print(f"{who:8} host {busy:5.1f}% busy  hottest core {hot:5.1f}%{proc}{flag}")
PY
}

# Each sampler has its own bounded lifetime and stop file. Never pgrep/kill a
# generic Python process; these samplers do not share load-generator groups.
MONITOR_HOSTS=()
MONITOR_NAMES=()
# Load-tool processes summed per host by comm (truncated to 15 characters).
LOAD_COMMS="--comm python3 --comm python --comm moqperf_test_cl --comm moqtest_server"
start_monitors() {
  local i=0 h name proc metrics comms
  MONITOR_BASE=/tmp/moqbench-monitor-$RUN_EPOCH-$$
  for h in "$RELAY" "${SUBHOSTS[@]}"; do
    if (( i == 0 )); then name=relay; proc=${PROC[$CFG]}; else name=sub$((i-1)); proc=none; fi
    comms=$LOAD_COMMS
    [[ $name != relay || $PUB == "$RELAY" ]] || comms=""
    metrics=""
    if [[ $name == relay && $CFG == moqdev ]]; then metrics="--metrics-url http://127.0.0.1:4503/metrics"; fi
    $SCP remote/load-monitor.py "$RUSER@$h:$MONITOR_BASE.py" || return 1
    timeout 30 $SSH -n "$RUSER@$h" "set -e
      pid=0; if [[ '$proc' != none ]]; then pid=\$(pgrep -xo '$proc'); fi
      rm -f $MONITOR_BASE.stop
      setsid python3 -u $MONITOR_BASE.py --pid \"\$pid\" --duration $((WAIT + 180)) $metrics $comms --stop-file $MONITOR_BASE.stop </dev/null >$MONITOR_BASE.jsonl 2>$MONITOR_BASE.err &
      sleep 1; kill -0 \$!" || return 1
    MONITOR_HOSTS+=("$h"); MONITOR_NAMES+=("$name")
    i=$((i+1))
  done
}
stop_monitors() {
  local h
  for h in "${MONITOR_HOSTS[@]}"; do
    timeout 10 $SSH -n "$RUSER@$h" "touch $MONITOR_BASE.stop" || true
  done
  (( ${#MONITOR_HOSTS[@]} == 0 )) || sleep 2
}
collect() {
  local i=0 h status=0
  : > "$OUT/collection.log"
  # Capture liveness BEFORE cleanup, so an intentional stop cannot hide a crash.
  timeout 30 $SSH -n "$RUSER@$RELAY" "pid=\$(pgrep -xo ${PROC[$CFG]}) || exit 1
    date -u; cat /proc/\$pid/stat; cat /proc/\$pid/status
    ss -lunp; dmesg --level=err,warn 2>/dev/null | tail -30; true" > "$OUT/relay-final-health.txt" 2>>"$OUT/collection.log" || status=1
  if [[ $EXISTING_RELAY == 1 && -n $RELAY_UNIT ]]; then
    timeout 30 $SSH -n "$RUSER@$RELAY" "journalctl -u $RELAY_UNIT --since '@$RUN_EPOCH' --no-pager" > "$OUT/relay.log" 2>>"$OUT/collection.log" || status=1
  elif [[ $EXISTING_RELAY == 0 ]]; then
    $SCP "$RUSER@$RELAY:/tmp/relay.log" "$OUT/relay.log" 2>>"$OUT/collection.log" || status=1
  else
    echo 'Existing relay has no RELAY_UNIT; relay logs unavailable.' >> "$OUT/collection.log"
  fi
  $SCP "$RUSER@$PUB:/tmp/loadpub.log" "$OUT/pub.log" 2>>"$OUT/collection.log" || status=1
  timeout 15 $SSH -n "$RUSER@$PUB" 'if [[ -f /tmp/loadpub.exit ]]; then cat /tmp/loadpub.exit; else echo still-running; fi' > "$OUT/pub-exit.txt" 2>>"$OUT/collection.log" || status=1
  case $(cat "$OUT/pub-exit.txt" 2>/dev/null) in 0|still-running) ;; *) status=1 ;; esac
  for h in "${SUBHOSTS[@]}"; do
    $SCP "$RUSER@$h:/tmp/loadsub.log" "$OUT/sub$i.log" 2>>"$OUT/collection.log" || status=1
    $SCP "$RUSER@$h:/tmp/loadsub.exit" "$OUT/sub$i-exit.txt" 2>>"$OUT/collection.log" || status=1
    [[ $(cat "$OUT/sub$i-exit.txt" 2>/dev/null) == 0 ]] || status=1
    # Missing optional moqbench files must not mask a tar failure.
    timeout 60 $SSH -n "$RUSER@$h" 'cd /tmp; shopt -s nullglob; files=(loadreport*); [[ ! -d moqbench-subs ]] || files+=(moqbench-subs); (( ${#files[@]} )) && tar czf - "${files[@]}"' 2>>"$OUT/collection.log" \
      | { mkdir -p "$OUT/sub$i"; tar xzf - -C "$OUT/sub$i" 2>>"$OUT/collection.log"; } || status=1
    i=$((i + 1))
  done
  for i in "${!MONITOR_HOSTS[@]}"; do
    h=${MONITOR_HOSTS[$i]}
    $SCP "$RUSER@$h:$MONITOR_BASE.jsonl" "$OUT/telemetry-${MONITOR_NAMES[$i]}.jsonl" 2>>"$OUT/collection.log" || status=1
    $SCP "$RUSER@$h:$MONITOR_BASE.err" "$OUT/telemetry-${MONITOR_NAMES[$i]}.err" 2>>"$OUT/collection.log" || status=1
  done
  echo "collection_status=$status" | tee -a "$OUT/collection.log"
  return "$status"
}

RUN_EPOCH=$(date +%s)
NS="bench-$(date +%H%M%S)"          # fresh per run: a relay can hold a stale registration
PER=$(( (SUBS + ${#SUBHOSTS[@]} - 1) / ${#SUBHOSTS[@]} ))
# AIOMOQT_ENV: environment for every aiomoqt process, e.g. MALLOC_ARENA_MAX=2
# STOP_AT_CEILING=1: adaptive ends at the first host's ceiling instead of running on to -t
# HOLD=SEC: adaptive steps and holds (--hold SEC --confirm CONFIRM) instead of ramping
# JOIN_SEARCH=B0: the first sub host searches the largest join batch (--join-burst) over its SEARCHER_SUBS;
#   the others hold PER subscribers as the relay's base load
PY="${AIOMOQT_ENV:+$AIOMOQT_ENV }aiomoqt/.venv/bin/python -u"
AIOMOQT_EXTRA=${AIOMOQT_EXTRA:-}   # e.g. "--max-queued-bytes N --max-inflight-bytes N"

case $TOOL in
  moqbench)
    PROFILE=${PROFILE:-moqbench/video-1080p.ini}
    # The publisher holds its first object until start_delay after setup, so
    # it has to outlast launching every subscriber on one host.
    START_DELAY_MS=${START_DELAY_MS:-$(python3 -c "print(int(5000 + $PER * $STAGGER * 1000 * 1.5))")}
    sed -E -e "s/^(start_delay *=).*/\1 $START_DELAY_MS/" -e "s/^(total_transmit_time *=).*/\1 $(( DUR * 1000 ))/" \
      "$PROFILE" > "$OUT/moqbench.ini"
    URI="moqt://ADDR:$RELAY_PORT$URLPATH"
    PUB_CMD="moq-bench/bin/moqbench_pub -c /tmp/moqbench.ini --connect_uri ${URI/ADDR/$PUB_ADDR}"
    sub_cmd() {  # <first test id> <count>
      echo "rm -rf /tmp/moqbench-subs; mkdir -p /tmp/moqbench-subs
for i in \$(seq $1 $(( $1 + $2 - 1 ))); do
  moq-bench/bin/moqbench_sub -i \$i -c /tmp/moqbench.ini --connect_uri ${URI/ADDR/$RPRIV} > /tmp/moqbench-subs/t_\${i}logs.txt 2>&1 &
  sleep $STAGGER
done
wait"
    }
    WAIT=$(( DUR + START_DELAY_MS / 1000 + 60 ))
    ;;
  adaptive|fixed)
    SUB_MBPS=${SUB_MBPS:-2} OBJ_SIZE=${OBJ_SIZE:-8192}
    STEP_SUBS=${STEP_SUBS:-10} JOIN=${JOIN:-5}
    # Objects per group: 60 at ~30 obj/s is a 2 s GOP. Subscribers join at the
    # next group start, so adaptive_bench's own default (4096, over two minutes
    # here) delivers nothing in a short run.
    GROUP=${GROUP:-60}
    PUB_FLAG=""
    if [[ $CFG == moqdev && $DRAFT == 16 ]]; then
      # Current upstream's draft-16 path is namespace + SUBSCRIBE. aiomoqt's
      # default PUBLISH+NAMESPACE stalls; its next-group filter is ignored.
      PUB_FLAG=--pub-ns
      SUB_FILTER=${SUB_FILTER:-latest-object}
      [[ $SUB_FILTER == latest-object ]] || { echo 'moqdev draft-16 baseline requires latest-object' >&2; exit 2; }
      [[ " $AIOMOQT_EXTRA " != *" --pub-both "* ]] || { echo 'moqdev baseline requires namespace publishing, not --pub-both' >&2; exit 2; }
    else
      SUB_FILTER=${SUB_FILTER:-next-group-start}
    fi
    case $SUB_FILTER in latest-object|next-group-start|absolute-start|absolute-range) ;; *) echo 'Invalid SUB_FILTER' >&2; exit 2;; esac
    RATE_FLAGS="--sub-mbps $SUB_MBPS -s $OBJ_SIZE"
    [[ -z $SHAPE ]] || RATE_FLAGS="--video $(shape_field "$SHAPE" aiomoqt_profile)"
    SUB_LIMITS="--latency-threshold $LATENCY_MS${SHORTFALL:+ --shortfall-ratio $SHORTFALL}"
    COMMON="-k --draft $DRAFT --mode subs -N $NS -T subs-ramp $RATE_FLAGS -g $GROUP $AIOMOQT_EXTRA $PUB_FLAG"
    BENCH="-m aiomoqt.tools.adaptive_bench"
    FIXED_FLAG=""
    SUB_EXIT="" SUB_HOLD=""
    if [[ -n ${JOIN_SEARCH:-} ]]; then
      [[ $TOOL == adaptive && $JOIN_SEARCH =~ ^[1-9][0-9]*$ && ${SEARCHER_SUBS:-} =~ ^[1-9][0-9]*$ ]] || {
        echo "JOIN_SEARCH and SEARCHER_SUBS: positive counts, for the adaptive tool" >&2; exit 2;
      }
      JOIN_FLAGS="--join-burst $JOIN_SEARCH --join-max ${JOIN_MAX:-$SEARCHER_SUBS} --join-bound ${JOIN_BOUND:-1000} --trial-gap ${TRIAL_GAP:-5} --base-s ${BASE_S:-45}"
    elif [[ ${HOLD:-0} != 0 ]]; then
      [[ $TOOL == adaptive && $HOLD =~ ^[0-9]+([.][0-9]+)?$ && ${CONFIRM:-30} =~ ^[0-9]+([.][0-9]+)?$ ]] || {
        echo "HOLD: seconds, for the adaptive tool" >&2; exit 2;
      }
      # Each host holds its confirmed level to -t, so hosts side by side end together.
      SUB_HOLD="--hold $HOLD --confirm ${CONFIRM:-30}"
      (( ${#SUBHOSTS[@]} > 1 )) || [[ ${STOP_AT_CEILING:-0} != 1 ]] || SUB_EXIT="--exit sustained"
    elif [[ ${STOP_AT_CEILING:-0} == 1 ]]; then
      SUB_EXIT="--exit max"
    fi
    if [[ $TOOL == fixed ]]; then
      [[ $SUBS =~ ^[1-9][0-9]*$ && $STEP_SUBS =~ ^[1-9][0-9]*$ && $DUR =~ ^[1-9][0-9]*$ ]] || {
        echo "fixed requires positive integer SUBS, STEP_SUBS and duration" >&2; exit 2;
      }
      (( SUBS % ${#SUBHOSTS[@]} == 0 && PER % STEP_SUBS == 0 )) || {
        echo "fixed: SUBS must divide evenly across hosts and each share must be a multiple of STEP_SUBS" >&2; exit 2;
      }
      BENCH=/tmp/moqbench-fixed-bench.py
      FIXED_FLAG=--fixed-subs
    fi
    PUB_CMD="$PY $BENCH moqt://$PUB_ADDR:$RELAY_PORT$URLPATH $COMMON --role pub -t $(( DUR + 30 ))"
    sub_cmd() {  # <first id> <count> <host index>
      local count=$2 filter=$SUB_FILTER limits=$SUB_LIMITS role=$SUB_HOLD
      if [[ -n ${JOIN_SEARCH:-} ]]; then
        if (( $3 == 0 )); then
          # The searcher: join latency is judged on the first object, so no wait for a group start.
          count=$SEARCHER_SUBS filter=latest-object role=$JOIN_FLAGS
        else
          # Base load: holds its subscribers whatever the latency; the searcher's watchers judge the relay.
          limits="--latency-threshold 1000000" role=""
        fi
      fi
      # Background jobs start with SIGINT ignored; the first host's ceiling ends the others with it.
      echo "env --default-signal=INT $PY $BENCH moqt://$RPRIV:$RELAY_PORT$URLPATH $COMMON --role sub \
--start-subs $STEP_SUBS --step-subs $STEP_SUBS --max-subs $count $FIXED_FLAG -j $JOIN -S $STAGGER \
--sub-filter $filter $limits $role $SUB_EXIT --report /tmp/loadreport.csv -t $DUR"
    }
    WAIT=$(( DUR + 60 ))
    ;;
  loadsim)
    # SCENARIO names a packaged one (default, rich-matrix, churn-storm,
    # datagram-mix, viewers); unset runs the one-track case. A scenario's
    # audiences run on every sub host, so N hosts carry N copies of them.
    SCN=${SCENARIO:+-f $SCENARIO}
    TRACK="-T track -r ${RATE:-30} -s ${OBJ_SIZE:-4096}"
    COMMON="-k --draft $DRAFT -N $NS $AIOMOQT_EXTRA"
    PUB_CMD="$PY -m aiomoqt.tools.load_sim moqt://$PUB_ADDR:$RELAY_PORT$URLPATH $COMMON ${SCN:-$TRACK} --role pub -t $(( DUR + 30 ))"
    sub_cmd() {
      echo "$PY -m aiomoqt.tools.load_sim moqt://$RPRIV:$RELAY_PORT$URLPATH $COMMON ${SCN:-$TRACK --subs $2} --role sub \
-t $DUR -i 5 --report /tmp/loadreport.csv"
    }
    WAIT=$(( DUR + 60 ))
    ;;
  moqperf-ramp)
    [[ -n $SHAPE ]] || { echo "moqperf-ramp needs SHAPE (480p, 720p, 1080p or 2160p)" >&2; exit 2; }
    DELIVERY_TIMEOUT=${DELIVERY_TIMEOUT:-500} MP_THREADS=${MP_THREADS:-32} MP_RAMP=${MP_RAMP:-64}
    MP="--transport=quic --versions=$DRAFT"
    PUB_CMD="LD_LIBRARY_PATH=$RROOT/moxygen/lib moxygen/bin/moqtest_server --relay_url=https://$PUB_ADDR:$RELAY_PORT$URLPATH $MP --include_timestamp_extension=true"
    # The client ramps by MP_RAMP per second up to its share; jemalloc when installed.
    sub_cmd() {
      echo "LD_PRELOAD=\$(ls /usr/lib/*-linux-gnu/libjemalloc.so.2 2>/dev/null | head -1) LD_LIBRARY_PATH=$RROOT/moxygen/lib \
moxygen/bin/moqperf_test_client --relay_url=https://$RPRIV:$RELAY_PORT$URLPATH $MP \
--subscriber_max=$2 --subscriber_ramp=$MP_RAMP --duration=$DUR --delivery_timeout=$DELIVERY_TIMEOUT --num_threads=$MP_THREADS \
--first_object_size=$(shape_field "$SHAPE" moqperf_first_object) --other_object_size=$(shape_field "$SHAPE" moqperf_other_object) \
--objects_per_group=$(shape_field "$SHAPE" objects_per_group) --object_interval_ms=$(shape_field "$SHAPE" object_interval_ms) \
--metrics_out=/tmp/loadreport.prom"
    }
    WAIT=$(( DUR + 60 ))
    ;;
  *) echo "unknown tool: $TOOL" >&2; exit 2 ;;
esac

echo "== $TOOL vs ${LABEL[$CFG]} draft $DRAFT  ${DUR}s  subs=$SUBS over ${#SUBHOSTS[@]} host(s)  pub on $PUB_HOST${SHAPE:+  shape $SHAPE}" | tee "$OUT/run.txt"
echo "   relay $RELAY ($RPRIV)  subs ${SUBHOSTS[*]}  ns $NS" | tee -a "$OUT/run.txt"
if [[ $TOOL == adaptive || $TOOL == fixed ]]; then
  printf 'publication=%s sub_filter=%s\n' "${PUB_FLAG:-aiomoqt-default}" "$SUB_FILTER" | tee -a "$OUT/run.txt"
fi
# Validate every participant before stopping or launching workloads.
if [[ $TOOL == adaptive || $TOOL == fixed || $TOOL == loadsim ]]; then
  want_moq=""
  [[ -z ${AIOMOQT_WHEEL:-} ]] || want_moq=$(basename "$AIOMOQT_WHEEL" | cut -d- -f2)
  printf -v check_args '%q %q' "$want_moq" "${AIOPQUIC_EXPECT_VERSION:-}"
  declare -A checked_hosts=()
  for h in "$PUB" "${SUBHOSTS[@]}"; do
    [[ -z ${checked_hosts[$h]:-} ]] || continue
    checked_hosts[$h]=1
    if ! timeout 30 $SSH "$RUSER@$h" "cd $RROOT && $PY - $check_args" >> "$OUT/loadgen-versions.txt" 2>&1 <<'PYCHECK'
import importlib.metadata as m
import platform
import subprocess
import sys
print(f"host={platform.node()} python={sys.version}", flush=True)
for package, expected in zip(('aiomoqt', 'aiopquic'), sys.argv[1:]):
    actual = m.version(package)
    print(f"{package}={actual}", flush=True)
    if expected and actual != expected:
        raise SystemExit(f"Expected {package}={expected}; provision this host before running.")
subprocess.run([sys.executable, '-m', 'aiomoqt.versions'], check=True)
PYCHECK
    then
      cat "$OUT/loadgen-versions.txt" >&2
      echo "Load-generator preflight failed on $h; workload not started." >&2
      exit 1
    fi
  done
fi
# Prepare a temporary CLI copy, preserving the installed package and native
# transport. Refuse a changed upstream source if the patch no longer applies.
if [[ $TOOL == fixed ]]; then
  cp patches/aiomoqt-fixed-subs.patch "$OUT/aiomoqt-fixed-subs.patch"
  for h in "${!checked_hosts[@]}"; do
    $SCP "$OUT/aiomoqt-fixed-subs.patch" "$RUSER@$h:/tmp/moqbench-fixed-subs.patch" || exit 1
    timeout 30 $SSH -n "$RUSER@$h" "set -e; cd $RROOT
      src=\$($PY -c 'import aiomoqt.tools.adaptive_bench as b; print(b.__file__)')
      patch --batch --fuzz=0 --output=/tmp/moqbench-fixed-bench.py \"\$src\" /tmp/moqbench-fixed-subs.patch
      $PY /tmp/moqbench-fixed-bench.py --help >/dev/null
      hostname; sha256sum \"\$src\" /tmp/moqbench-fixed-bench.py" >> "$OUT/fixed-runner.txt" 2>&1 || {
        cat "$OUT/fixed-runner.txt" >&2; exit 1;
      }
  done
fi
AFFINITY=""
if [[ -n $RELAY_CPUS ]]; then
  [[ $RELAY_CPUS =~ ^[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*$ ]] || { echo "invalid RELAY_CPUS" >&2; exit 2; }
  AFFINITY="taskset -c $RELAY_CPUS"
fi
printf 'threads=%s relay_cpus=%s subscribers_per_host=%s\n' "$THREADS" "${RELAY_CPUS:-default}" "$PER" | tee -a "$OUT/run.txt"
record_versions "$OUT"
if [[ $EXISTING_RELAY == 1 ]]; then
  printf 'existing_relay=1 port=%s unit=%s (THREADS and RELAY_CPUS do not alter service)\n' "$RELAY_PORT" "${RELAY_UNIT:-unset}" | tee -a "$OUT/run.txt"
  timeout 30 $SSH -n "$RUSER@$RELAY" "set -e
    pid=\$(pgrep -xo ${PROC[$CFG]})
    ss -H -lunp 'sport = :$RELAY_PORT' | grep -F \"pid=\$pid,\"
    echo pid=\$pid
    readlink -f /proc/\$pid/exe
    sha256sum /proc/\$pid/exe
    tr '\\0' ' ' < /proc/\$pid/cmdline; echo
    for status in /proc/\$pid/task/*/status; do
      echo \"\$status\"; grep -E '^(Name|Cpus_allowed_list):' \"\$status\"
    done" > "$OUT/relay-affinity.txt" 2>&1 || { cat "$OUT/relay-affinity.txt" >&2; exit 1; }
  stop_all
else
stop_all

timeout 120 $SSH -n "$RUSER@$RELAY" "cd $RROOT && ulimit -n 1048576
  cat > /tmp/relay-start.sh <<RELAYEOF
$(relay_cmd "$CFG" "$THREADS")
RELAYEOF
  setsid $AFFINITY bash /tmp/relay-start.sh </dev/null >/tmp/relay.log 2>&1 &
  sleep 5; pid=\$(pgrep -xo ${PROC[$CFG]}) || { echo 'relay did not start:'; tail -20 /tmp/relay.log; exit 1; }
  echo pid=\$pid threads=$THREADS requested_cpus=${RELAY_CPUS:-default}
  for status in /proc/\$pid/task/*/status; do
    echo \"\$status\"; grep -E '^(Name|Cpus_allowed_list):' \"\$status\"
  done" > "$OUT/relay-affinity.txt" 2>&1 || { cat "$OUT/relay-affinity.txt" >&2; exit 1; }
fi
cat "$OUT/relay-affinity.txt"
if [[ $CFG == moqdev ]]; then
  timeout 30 $SSH -n "$RUSER@$RELAY" "cd $RROOT; cat moq-dev/build.json; cat moq-dev/perf.toml; pid=\$(pgrep -xo moq-relay-dev); tr '\\0' ' ' < /proc/\$pid/cmdline; echo" > "$OUT/moqdev-config.txt" || exit 1
  if [[ $EXISTING_RELAY == 0 ]]; then
    if [[ ${MOQDEV_RUNTIME:-workers} == shared ]]; then
      actual=$(grep -Ec '^Name:[[:space:]]+tokio-rt-worker$' "$OUT/relay-affinity.txt")
      (( actual >= THREADS )) || { echo "moqdev requested $THREADS runtime threads but found $actual" >&2; stop_all; exit 1; }
    else
      actual=$(grep -Ec '^Name:[[:space:]]+moq-(quic|uring)-[0-9]+$' "$OUT/relay-affinity.txt")
      [[ $actual == "$THREADS" ]] || { echo "moqdev requested $THREADS QUIC workers but found $actual" >&2; stop_all; exit 1; }
    fi
  fi
fi
# Always clean up this run's load generators on interruption.
COLLECTED=0
finish_run() {
  local rc=$?
  trap - EXIT INT TERM
  if [[ $COLLECTED == 0 ]]; then
    stop_monitors
    collect || true
    echo "Interrupted/early exit: status=$rc; evidence may be partial." >> "$OUT/collection.log"
  fi
  stop_all
  exit "$rc"
}
trap finish_run EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ $TOOL == moqbench ]]; then
  for h in "$PUB" "${SUBHOSTS[@]}"; do $SCP "$OUT/moqbench.ini" "$RUSER@$h:/tmp/moqbench.ini"; done
fi
printf 'publisher_command=%s\nsubscriber_command_per_host=%s\n' "$PUB_CMD" "$(sub_cmd 1 "$PER")" > "$OUT/run-settings.txt"
start_monitors || { echo "Telemetry preflight failed; no load started." >&2; exit 1; }
remote_bg "$PUB" loadpub "$PUB_CMD" || exit 1
sleep 3
cpu_snap a
i=0
for h in "${SUBHOSTS[@]}"; do
  remote_bg "$h" loadsub "$(sub_cmd $(( i * PER + 1 )) "$PER" "$i")" || exit 1
  i=$((i + 1))
done
WAITERS=()
for h in "${SUBHOSTS[@]}"; do remote_wait "$h" loadsub "$WAIT" & WAITERS+=($!); done
if [[ ${STOP_AT_CEILING:-0} == 1 && $TOOL == adaptive ]]; then
  # One host at its ceiling ends the run: with its load gone the others would climb on.
  wait -n "${WAITERS[@]}"
  for h in "${SUBHOSTS[@]}"; do
    timeout 15 $SSH -n "$RUSER@$h" "pkill -INT -f 'adaptive_ben[c]h.*--role sub'; true" || true
  done
fi
wait "${WAITERS[@]}"
cpu_snap b
stop_monitors
RUN_STATUS=0
collect || RUN_STATUS=1
COLLECTED=1
python3 bin/load-telemetry-report.py "$OUT" | tee "$OUT/telemetry-summary.txt" || RUN_STATUS=1
stop_all

if [[ $TOOL == fixed ]]; then
  python3 bin/fixed-load-report.py "$OUT" --hosts "${#SUBHOSTS[@]}" --subs "$SUBS" --duration "$DUR" --slo-ms "${SLO_MS:-500}" --min-delivery "${MIN_DELIVERY:-0.95}" --min-steady-seconds "${MIN_STEADY_SECONDS:-30}" | tee -a "$OUT/run.txt" || RUN_STATUS=1
fi
echo "-- cpu" | tee -a "$OUT/run.txt"; cpu_report | tee -a "$OUT/run.txt"
case $TOOL in
  moqbench)
    # One "OR COMPLETE" CSV line per subscriber track that finished; field 18 is
    # objects published minus received.
    { echo "test_id,test_name,total_time,transmit_time,objects,bytes,sent_objects,sent_bytes,min_bps,max_bps,avg_bps,min_delta_us,max_delta_us,avg_delta_us,min_arrival_us,max_arrival_us,avg_arrival_us,delta_objects,over_multiplier"
      grep -rhoE 'OR COMPLETE, .*' "$OUT"/sub*/moqbench-subs 2>/dev/null | sed -E 's/^OR COMPLETE, //; s/, /,/g'; } > "$OUT/moqbench.csv"
    awk -F, -v want="$SUBS" 'NR > 1 { n++; lost += $18; bps += $11; if ($13 > mx) mx = $13 }
      END { printf "-- moq-bench: %d of %d subscriber tracks completed, %d objects short, avg %.2f Mbps/sub, worst object delta %.1f ms\n",
            n, want, lost, n ? bps / n / 1e6 : 0, mx / 1000 }' "$OUT/moqbench.csv" | tee -a "$OUT/run.txt" ;;
  *)
    for f in "$OUT"/sub*.log; do [[ -f $f ]] || continue; echo "-- $(basename "$f")"; tail -8 "$f"; done | tee -a "$OUT/run.txt" ;;
esac
echo "run_exit_status=$RUN_STATUS (nonzero means failed validation or incomplete evidence)" | tee -a "$OUT/run.txt"
echo "wrote $OUT"
exit "$RUN_STATUS"
