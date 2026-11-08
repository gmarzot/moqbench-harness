# Shared single-point runner. Sourced by bin/relay-capacity.sh and bin/relay-load.sh, from
# the repo root; not executable alone.
#
# Caller provides: SSH, RELAY, RPRIV, CLIENTS[], FIRST_OBJ, OTHER_OBJ,
# OBJ_DELAY_MS, OBJ_PER_GROUP, CLIENT_MAX_BUSY, SLO_MS, SLO_PCT, MAX_RESETS.
#
# Per-subscriber bitrate is OTHER_OBJ * 8 / (OBJ_DELAY_MS/1000): the object
# size and the inter-object delay together set the load per subscriber.

# Remote identity and layout: cloud images use ubuntu with ~/Projects. Hosts set
# up another way take RUSER and RROOT from the environment.
RUSER=${RUSER:-ubuntu}
RROOT=${RROOT:-/home/ubuntu/Projects}

CSV_HEADER="config,threads,subs,mbps,obj_s,relay_proc_cores,relay_host_cores,mbps_per_core,lat_pctile_ms,obj_per_sub,resets,subs_held,relay_host_busy,relay_hot_core,client_busy_max,client_hot_core,status"

# Kernel truncates comm to 15 chars, so moq-relay-quiche only matches as
# moq-relay-quich. pkill -f would match this script's own command line.
declare -A PROC=( [moqdev]=moq-relay-dev [moqx]=moqx [cf]=moq-relay-ietf [quinn]=moq-relay-quinn [quiche]=moq-relay-quich [aiomoqt]=aiomoqt-relay )
declare -A PATHS=( [moqdev]=/anon [moqx]=/moq-relay [cf]=/ [quinn]=/anon [quiche]=/anon [aiomoqt]=/ )
# Where each relay's binary lives under $RROOT: what bin/relay-deploy.sh
# installs and versions.txt reports. moqx's is the release build's; moqx/moqx
# launches it.
declare -A BIN=( [moqdev]=moq-dev/moq-relay-dev [moqx]=moqx/bin/moqx [cf]=moq-rs/moq-relay-ietf [quinn]=moq-dev/moq-relay-quinn [quiche]=moq-dev/moq-relay-quiche )

# Reported names. The short keys are what you type; these are what lands in the
# CSV, because more than one historical relay runs on quinn and "quinn" alone does not
# say which. Every published figure should name the relay and the stack.
declare -A LABEL=(
  [moqdev]=moq-dev/moq-quic
  [moqx]=moqx/mvfst
  [cf]=cf-moq-rs/quinn
  [quinn]=moq-dev/quinn
  [quiche]=moq-dev/quiche
  [aiomoqt]=aiomoqt/picoquic
)

# Line 1 is the host aggregate plus the named process; the rest are per-core.
# Per-core matters because an average cannot show a single pegged core, and
# with one NIC receive queue all softirq lands on one CPU — the host can read
# as mostly idle while the thing that actually limits throughput is saturated.
CPUSNAP='read _ u n s i io irq sirq st rest < /proc/stat
T=$((u+n+s+i+io+irq+sirq+st)); I=$((i+io))
P=$(pgrep -x "$1" | head -1); C=0
[ -n "$P" ] && C=$(awk "{print \$14+\$15}" /proc/$P/stat 2>/dev/null || echo 0)
echo "$T $I ${C:-0} $(nproc)"
awk "/^cpu[0-9]/{print \$2+\$3+\$4+\$5+\$6+\$7+\$8+\$9, \$5+\$6}" /proc/stat'

# The Prometheus latency histogram only exists in builds carrying moxygen
# 8ea26dc9; older clients abort on the unknown flag rather than ignoring it.
# Without it the health gate falls back to per-interval max latency, which is
# stricter than p99 and is labelled as such in the output.
TEXTFILE_DIR=${TEXTFILE_DIR:-/var/lib/prometheus/node-exporter}
detect_caps() {
  if $SSH "$RUSER"@"${CLIENTS[0]}" "LD_LIBRARY_PATH=$RROOT/moxygen/lib $RROOT/moxygen/bin/moqperf_test_client --help 2>&1 | grep -q metrics_out"; then
    PROM_FLAG="--metrics_out $TEXTFILE_DIR/moqperf.prom"; LAT_METRIC=p99
  else
    PROM_FLAG=""; LAT_METRIC=maxint
    echo "  note: client has no --metrics_out; no client-side Prometheus export" >&2
  fi
  export PROM_FLAG LAT_METRIC
}

# Exact versions under test, so a published number can be reproduced. RROOT is
# passed in rather than referenced inside the quoted script: it is set here,
# not on the remote host.
record_versions() {
  local out=$1 k bins=""
  for k in "${!BIN[@]}"; do bins+=" $k=${BIN[$k]}"; done
  # Every relay build on the host: the label its deploy recorded, or "placed by
  # hand" when the binary is not the one recorded, and its sha either way.
  $SSH "$RUSER"@"$RELAY" "cd $RROOT; BINS='$bins'"'
    for kv in $BINS; do
      p=${kv#*=}; [ -e "$p" ] || continue
      c=$(sha256sum "$p" | cut -d" " -f1); r=$(sed -n 1p "$p.rev" 2>/dev/null)
      [ "$c" = "$(sed -n 2p "$p.rev" 2>/dev/null)" ] || r="(placed by hand)"
      printf "%-15s %s sha %s\n" "${kv%%=*}:" "${r:--}" "${c:0:16}"
    done
    echo "moqx:      $(./moqx/moqx --version 2>/dev/null | head -1)"
    echo "aiomoqt-relay: $(aiomoqt/.venv/bin/python -c "import importlib.metadata as m; print(m.version(\"aiomoqt\"), \"aiopquic\", m.version(\"aiopquic\"))" 2>/dev/null || echo -)"
    echo "moqtest_server: $(sha256sum moxygen/bin/moqtest_server 2>/dev/null | cut -c1-16)"
    echo "kernel:    $(uname -r)"
    echo "relay-mtu: $(cat /sys/class/net/$(ip -o -4 route show to default | cut -d" " -f5 | head -1)/mtu)"
    echo "relay-cpu: $(nproc) vcpu, $(lscpu | awk -F: "/Model name/{print \$2}" | xargs)"' \
    > "$out/versions.txt" 2>&1
  $SSH "$RUSER"@"${CLIENTS[0]}" "cd $RROOT"'
    echo "moqperf_client: $(sha256sum moxygen/bin/moqperf_test_client 2>/dev/null | cut -c1-16)"
    echo "client-mtu:     $(cat /sys/class/net/$(ip -o -4 route show to default | cut -d" " -f5 | head -1)/mtu)"
    echo "moq-bench:      $(cat moq-bench/REVISION 2>/dev/null || echo -)"
    echo "aiomoqt:        $(aiomoqt/.venv/bin/python -m aiomoqt.versions 2>/dev/null | head -3 | xargs)"' \
    >> "$out/versions.txt" 2>&1
  cat "$out/versions.txt" >&2
}

# Load tools started by relay-load.sh run under setsid and record their process
# group in /tmp/loadgen.pgids, so they can be killed as groups. The Python ones
# cannot be matched by name: their comm is "python", and a pkill -f pattern
# would match the ssh command line running it.
KILL_LOADGEN='[ -f /tmp/loadgen.pgids ] && while read -r g; do kill -TERM -- "-$g" 2>/dev/null; done < /tmp/loadgen.pgids; rm -f /tmp/loadgen.pgids
pkill -x moqperf_test_client; pkill -x moqtest_server; pkill -x moqbench_pub; pkill -x moqbench_sub'

# Every relay PROC names, built-in or from relays.local.sh, so a relay left
# running from an earlier run can never hold the port into the next one.
stop_all() {
  local p kill_relays=""
  for p in $(printf '%s\n' "${PROC[@]}" | sort -u); do kill_relays+="pkill -x $p; "; done
  timeout 30 $SSH -n "$RUSER"@"$RELAY" "$kill_relays$KILL_LOADGEN; sleep 1" >/dev/null 2>&1
  for C in "${CLIENTS[@]}" ${PROBES[@]+"${PROBES[@]}"}; do
    timeout 30 $SSH -n "$RUSER"@"$C" "$KILL_LOADGEN; rm -f /tmp/moqperf.prom; sleep 1" >/dev/null 2>&1 &
  done; wait
}

# moqx tunables. SEND/RECV default to the values Config.h intends (48/64);
# moqx-run.sh's own defaults are 16/256, which silently override the C++ ones
# and are what every earlier benchmark actually ran with. GRO coalescing is off
# by default (numGROBuffers 1) and has no CLI flag, so it is injected into the
# config template instead. Each injected key is written only when its variable
# is non-empty.
MOQX_SEND=${MOQX_SEND:-48}
MOQX_RECV=${MOQX_RECV:-64}
MOQX_GRO=${MOQX_GRO-1}
# Write-loop budget as srtt/N (0 removes it) and the pacing tick in us. mvfst's
# default of 25 leaves ~16us per loop on a sub-millisecond path, and mvfst's own
# tperf sets 0 when pacing is on. Unset by default: moqx releases through 0.3.7
# lack both keys and will not start with them.
MOQX_WLRF=${MOQX_WLRF:-}
MOQX_PTI=${MOQX_PTI:-}
# jemalloc (-j) with transparent huge pages, as moqx's perf script runs it.
# MOQX_JEMALLOC=0 drops -j; MOQX_THP=0 drops MALLOC_CONF.
MOQX_JFLAG="-j"; [[ ${MOQX_JEMALLOC:-1} == 0 ]] && MOQX_JFLAG=""
MOQX_MALLOC="MALLOC_CONF=thp:always "; [[ ${MOQX_THP:-1} == 0 ]] && MOQX_MALLOC=""
MOQX_CC=${MOQX_CC:-bbr2}
# MoQT drafts moqx advertises. Every relay here runs draft 16; relay-load.sh adds 18
# for the draft-18 moq-bench build, which only moqx can serve.
MOQX_VERSIONS=${MOQX_VERSIONS:-16}
# The client divides subscriber_max by its thread count using integer division,
# so a request that is not a multiple of (threads x clients) silently attaches
# fewer subscribers than asked: 25 over 8 threads attaches 24, and so do 26, 28
# and 31. At fan-out counts that is a rounding error, but at the low counts a
# 16 Mbps profile reaches it is up to a quarter of the load, and it reads as the
# relay shedding. Fix the thread count so the harness can round to it.
CLIENT_THREADS=${CLIENT_THREADS:-8}
# Path MTU discovery is OFF for every relay. cf-moq-rs and moq-dev both call
# mtu_discovery_config(None), so they never probe; leaving moqx to probe would
# charge it for a behaviour no competitor performs. --ignore-path-mtu makes
# moqx skip discovery too. The relays still settle on different fixed sizes
# (quinn/quiche at their 1200-byte initial MTU), which is a residual difference
# to state rather than one the harness can remove. MOQX_PMTU=0 to re-enable
# probing on moqx alone.
MOQX_PMTU_FLAG="--ignore-path-mtu"; [[ ${MOQX_PMTU:-1} == 0 ]] && MOQX_PMTU_FLAG=""
# Stream concurrency moqx advertises, matched on moq-dev below. cf-moq-rs
# hardcodes quinn's default of 100 and cannot be matched without a patch.
MAX_STREAMS=${MAX_STREAMS:-8192}
# GSO already defaults on (gso.unwrap_or(true)); passing it a value makes clap
# read the value as a positional config path and the relay exits immediately.
# quinn and quiche both implement BBRv1; moqx's bbr2 is a different algorithm.
# MOQX_CC=bbr gives a like-for-like congestion control comparison.
DEV_CC=${DEV_CC:-delay}

# Relays that live outside this repo: relays.local.sh (untracked) may add
# PROC/PATHS/LABEL/BIN entries and a local_relay_cmd <cfg> <threads> for them,
# plus a local_deploy for one that needs more than a binary copied in. See
# relays.local.sh.example.
if [[ -f relays.local.sh ]]; then source ./relays.local.sh; fi

relay_cmd() {
  local cfg=$1 threads=$2
  case $cfg in
    moqdev)
      local pin=${MOQDEV_PIN:-false} cc=${MOQDEV_CC:-delay} aux=${MOQDEV_AUX_THREADS:-2} rt=${MOQDEV_RUNTIME:-workers}
      [[ $threads =~ ^[1-9][0-9]*$ && $aux =~ ^[1-9][0-9]*$ ]] || return 2
      [[ $pin == true || $pin == false ]] || return 2
      [[ $cc == delay || $cc == loss || $cc == none ]] || return 2
      [[ $rt == workers || $rt == uring || $rt == shared ]] || return 2
      echo "MOQDEV_PIN=$pin MOQDEV_CC=$cc MOQDEV_AUX_THREADS=$aux MOQDEV_RUNTIME=$rt moq-dev/run.sh $threads" ;;
    moqx)
      local kv inject=""
      for kv in "num_gro_buffers=$MOQX_GRO" "write_limit_rtt_fraction=$MOQX_WLRF" "pacing_tick_interval_us=$MOQX_PTI"; do
        [[ -n ${kv#*=} ]] && inject+="; print \"    ${kv%%=*}: ${kv#*=}\""
      done
      echo "awk '/^  mvfst:/{print$inject; next} {print}' moqx/config.bench.yaml > /tmp/cfg.yaml && ${MOQX_MALLOC}MOQX_BIN=\$PWD/moqx/moqx MOQX_RESOLVED_CONFIG=/tmp/r.yaml LD_LIBRARY_PATH=$RROOT/moxygen/lib moqx/moqx-run.sh --config /tmp/cfg.yaml --insecure $MOQX_JFLAG --no-cache $MOQX_PMTU_FLAG --bind :: --port 4533 --admin-port 4500 --threads $threads --cc $MOQX_CC --send-pkts $MOQX_SEND --recv-pkts $MOQX_RECV --relay-thread --local-forwarders --udp-buffer 33554432 --moqt-versions $MOQX_VERSIONS" ;;
    # The public relay's settings (aiomoqt deploy/aiomoqt-relay/relay.toml), gate from AIOMOQT_ADMISSION_CPU; one event loop, so threads is unused.
    aiomoqt) echo "aiomoqt/.venv/bin/aiomoqt-relay --dual --bind 0.0.0.0 --port 4533 --cert bench/cert.pem --key bench/key.pem --draft 22,20,18,16,14 --watchdog 30 --cache-bytes 268435456 --admission-cpu ${AIOMOQT_ADMISSION_CPU:-87} --cc-algo bbr" ;;
    cf)     echo "RUST_LOG=warn TOKIO_WORKER_THREADS=$threads MOQ_UDP_BUFFER_BYTES=33554432 moq-rs/moq-relay-ietf --bind 0.0.0.0:4533 --tls-cert bench/cert.pem --tls-key bench/key.pem --coordinator-file /tmp/c.json" ;;
    quinn)  echo "RUST_LOG=warn TOKIO_WORKER_THREADS=$threads MOQ_UDP_BUFFER_BYTES=33554432 moq-dev/moq-relay-quinn --server-bind 0.0.0.0:4533 --tls-generate localhost,$RPRIV --auth-public '' --log-level warn --server-quic-max-streams $MAX_STREAMS --server-quic-congestion-control $DEV_CC" ;;
    quiche) echo "RUST_LOG=warn TOKIO_WORKER_THREADS=$threads MOQ_QUICHE_SHARDS=$threads MOQ_UDP_BUFFER_BYTES=33554432 moq-dev/moq-relay-quiche --server-bind 0.0.0.0:4533 --tls-generate localhost,$RPRIV --auth-public '' --log-level warn --server-quic-max-streams $MAX_STREAMS --server-quic-congestion-control $DEV_CC" ;;
    *)      declare -F local_relay_cmd >/dev/null && local_relay_cmd "$cfg" "$threads" ;;
  esac
}

# run_point <config> <threads> <subs> <duration> -> one CSV row
run_point() {
  local cfg=$1 threads=$2 subs=$3 dur=$4
  local path=${PATHS[$cfg]} proc=${PROC[$cfg]}
  local ns="moq-test-00/0/0/0/$dur/$OBJ_PER_GROUP/$OBJ_PER_GROUP/$FIRST_OBJ/$OTHER_OBJ/$OBJ_DELAY_MS/1/1/0/-1/-1/0"
  local pubextra="" cliextra="--delivery_timeout 5000"
  # moq-dev resolves broadcasts by exact path and rejects DELIVERY_TIMEOUT.
  if [[ $cfg == moqdev || $cfg == quinn || $cfg == quiche ]]; then
    pubextra="--announce_namespace $ns"; cliextra="--delivery_timeout 0"
  fi

  stop_all
  # setsid + </dev/null: a backgrounded process still holding the SSH channel
  # keeps the session open and the call never returns.
  #
  # The publisher's namespace registration expires with the relay session idle
  # timeout, so it starts immediately before the client, never minutes ahead.
  # Staged through a file rather than `bash -c '...'`: the relay commands
  # contain their own single quotes (awk programs, empty-string flags) that
  # would terminate the wrapper's quoting. The heredoc is unquoted so $PWD and
  # $HOME still expand on the relay.
  timeout 120 $SSH -n "$RUSER"@"$RELAY" "cd $RROOT && ulimit -n 1048576
    cat > /tmp/relay-start.sh <<RELAYEOF
$(relay_cmd "$cfg" "$threads")
RELAYEOF
    setsid bash /tmp/relay-start.sh </dev/null >/tmp/relay.log 2>&1 &
    sleep 5
    LD_LIBRARY_PATH=$RROOT/moxygen/lib $RROOT/moxygen/bin/moqtest_server \
      --relay_url https://127.0.0.1:4533$path --transport quic --versions 16 \
      --include_timestamp_extension=true $pubextra </dev/null >/tmp/pub.log 2>&1 &
    sleep 4; grep -q 'Successfully published' /tmp/pub.log || echo PUBFAIL" >/tmp/.pubchk 2>&1
  if grep -q PUBFAIL /tmp/.pubchk; then
    echo "${LABEL[$cfg]:-$cfg},$threads,$subs,,,,,,,,,,,,,PUBLISH_FAILED"; return
  fi

  # Round to a whole number of per-thread groups so none are truncated away.
  local grain=$(( CLIENT_THREADS * ${#CLIENTS[@]} ))
  local rounded=$(( (subs / grain) * grain ))
  (( rounded < grain )) && rounded=$grain
  subs=$rounded
  local per=$(( subs / ${#CLIENTS[@]} ))
  # Attach fast enough that the ramp does not eat the measurement window.
  local ramp=$(( per / 4 )); [[ $ramp -lt 100 ]] && ramp=100

  timeout 30 $SSH -n "$RUSER"@"$RELAY" "bash -c '$CPUSNAP'" -- "$proc" > /tmp/.r0 2>/dev/null
  for i in "${!CLIENTS[@]}"; do
    timeout 30 $SSH -n "$RUSER"@"${CLIENTS[$i]}" "bash -c '$CPUSNAP'" -- moqperf_test_client > /tmp/.c0."$i" 2>/dev/null &
  done; wait

  for i in "${!CLIENTS[@]}"; do
    timeout $(( dur + 90 )) $SSH -n "$RUSER"@"${CLIENTS[$i]}" "ulimit -n 1048576
      LD_LIBRARY_PATH=$RROOT/moxygen/lib $RROOT/moxygen/bin/moqperf_test_client \
        --relay_url https://$RPRIV:4533$path --transport quic --versions 16 \
        --num_threads $CLIENT_THREADS --subscriber_max $per --subscriber_ramp $ramp \
        --duration $dur $cliextra --first_object_size=$FIRST_OBJ \
        --other_object_size=$OTHER_OBJ --objects_per_group $OBJ_PER_GROUP \
        $PROM_FLAG 2>&1 | grep -oE '\[AGGREGATE\].*'" \
      > /tmp/.cli."$i" 2>/dev/null &
  done; wait

  timeout 30 $SSH -n "$RUSER"@"$RELAY" "bash -c '$CPUSNAP'" -- "$proc" > /tmp/.r1 2>/dev/null
  for i in "${!CLIENTS[@]}"; do
    timeout 30 $SSH -n "$RUSER"@"${CLIENTS[$i]}" "bash -c '$CPUSNAP'" -- moqperf_test_client > /tmp/.c1."$i" 2>/dev/null &
    timeout 30 $SSH -n "$RUSER"@"${CLIENTS[$i]}" 'cat /tmp/moqperf.prom 2>/dev/null' > /tmp/.prom."$i" 2>/dev/null &
  done; wait
  stop_all

  CFG="${LABEL[$cfg]:-$cfg}" THREADS=$threads SUBS=$subs NCLI=${#CLIENTS[@]} \
  CLIENT_MAX_BUSY=$CLIENT_MAX_BUSY SLO_MS=$SLO_MS SLO_PCT=$SLO_PCT \
  MAX_RESETS=${MAX_RESETS:-0} OBJ_DELAY_MS=$OBJ_DELAY_MS python3 ./lib/summarize.py
}
