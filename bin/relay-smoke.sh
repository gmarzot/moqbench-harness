#!/usr/bin/env bash
# Deploy validation — proves every piece works before spending on a real run.
# Not a benchmark: subscriber counts are deliberately small.
#
#   ./bin/relay-smoke.sh              # all three relays, ~90s
#   ./bin/relay-smoke.sh moqx         # just one
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
source ./lib/dotenv.sh
source ./lib/hosts.sh
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
CLIENT=${CLIENTS[0]}
WHICH=${1:-all}
SUBS=${SUBS:-50}; DUR=${DUR:-15}

# NOTE: Linux truncates process names at 15 chars, so "moq-relay-quiche"
# must be matched as "moq-relay-quich". This has bitten us twice.
stop_all() { $SSH ubuntu@"$RELAY" "pkill -x moqx; pkill -x moq-relay-ietf; pkill -x moq-relay-dev; pkill -x moq-relay-quinn; pkill -x moq-relay-quich; pkill -x moqtest_server; sleep 1" >/dev/null 2>&1; }

run_one() {
  local name=$1 start=$2 path=$3 extra=${4:-}
  stop_all
  $SSH ubuntu@"$RELAY" "cd ~/Projects && ulimit -n 65536
    $start >/tmp/relay.log 2>&1 &
    sleep 4
    LD_LIBRARY_PATH=\$HOME/Projects/moxygen/lib ~/Projects/moxygen/bin/moqtest_server \
      --relay_url https://127.0.0.1:4533$path --transport quic --versions 16 \
      --include_timestamp_extension=true $extra >/tmp/pub.log 2>&1 &
    sleep 3; grep -q 'Successfully published' /tmp/pub.log || { echo '    publish FAILED'; tail -2 /tmp/pub.log; }" >/dev/null 2>&1
  local out
  out=$($SSH ubuntu@"$CLIENT" "ulimit -n 65536
    LD_LIBRARY_PATH=\$HOME/Projects/moxygen/lib ~/Projects/moxygen/bin/moqperf_test_client \
      --relay_url https://$RPRIV:4533$path --transport quic --versions 16 --num_threads 2 \
      --subscriber_max $SUBS --subscriber_ramp $SUBS --duration $DUR ${5:-} \
      --first_object_size=26516 --other_object_size=3788 --objects_per_group 60 2>&1 |
    grep -oE 'Subs: [0-9]+ \| Obj/s: [0-9]+ \| Mbps: [0-9.]+' | tail -3 | head -1")
  printf '  %-26s %s\n' "$name" "${out:-NO DATA}"
}

echo "relay=$RELAY ($RPRIV)  client=$CLIENT  subs=$SUBS dur=${DUR}s"

[[ $WHICH == all || $WHICH == moqx ]] && run_one "moqx (mvfst)" \
  "MOQX_BIN=\$PWD/moqx/moqx MOQX_RESOLVED_CONFIG=/tmp/r.yaml LD_LIBRARY_PATH=\$HOME/Projects/moxygen/lib moqx/moqx-run.sh --config \$PWD/moqx/config.bench.yaml --insecure --no-cache --ignore-path-mtu --bind :: --port 4533 --admin-port 4500 --threads 4 --cc bbr2 --relay-thread --local-forwarders --udp-buffer 33554432 --moqt-versions 16" \
  "/moq-relay" "" "--delivery_timeout 5000"

[[ $WHICH == all || $WHICH == cf ]] && run_one "cf moq-rs (quinn)" \
  "RUST_LOG=warn TOKIO_WORKER_THREADS=4 MOQ_UDP_BUFFER_BYTES=33554432 moq-rs/moq-relay-ietf --bind 0.0.0.0:4533 --tls-cert bench/cert.pem --tls-key bench/key.pem --coordinator-file /tmp/c.json" \
  "/" "" "--delivery_timeout 5000"

# moq-dev resolves broadcasts by exact path (no prefix routing) and rejects the
# DELIVERY_TIMEOUT param, so it needs the full tuple announced and timeout 0.
NS="moq-test-00/0/0/0/$DUR/60/60/26516/3788/33/1/1/0/-1/-1/0"
[[ $WHICH == moqdev ]] && run_one "moq-dev (moq-quic workers)" \
  "moq-dev/run.sh 4" \
  "/anon" "--announce_namespace $NS" "--delivery_timeout 0"

[[ $WHICH == all || $WHICH == quinn ]] && run_one "moq-dev (quinn)" \
  "RUST_LOG=warn TOKIO_WORKER_THREADS=4 moq-dev/moq-relay-quinn --server-bind 0.0.0.0:4533 --tls-generate localhost,$RPRIV --auth-public '' --log-level warn" \
  "/anon" "--announce_namespace $NS" "--delivery_timeout 0"

[[ $WHICH == all || $WHICH == quiche ]] && run_one "moq-dev (quiche x4 shards)" \
  "RUST_LOG=warn TOKIO_WORKER_THREADS=4 MOQ_QUICHE_SHARDS=4 moq-dev/moq-relay-quiche --server-bind 0.0.0.0:4533 --tls-generate localhost,$RPRIV --auth-public '' --log-level warn" \
  "/anon" "--announce_namespace $NS" "--delivery_timeout 0"

stop_all
echo "done (relays stopped)"
