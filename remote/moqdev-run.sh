#!/usr/bin/env bash
# Installed beside moq-relay-dev and perf.toml under ~/Projects/moq-dev.
set -euo pipefail
DIR=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
cd "$DIR/.."
WORKERS=${1:-16}
PORT=${MOQDEV_PORT:-4533}
ADMIN=${MOQDEV_ADMIN_PORT:-4503}
PIN=${MOQDEV_PIN:-false}
CC=${MOQDEV_CC:-delay}
RUNTIME=${MOQDEV_RUNTIME:-workers}
[[ $WORKERS =~ ^[1-9][0-9]*$ ]] && (( WORKERS <= 65535 )) || { echo 'Invalid worker count' >&2; exit 2; }
for n in "$PORT" "$ADMIN"; do
  [[ $n =~ ^[1-9][0-9]*$ ]] && (( n <= 65535 )) || { echo 'Invalid port' >&2; exit 2; }
done
[[ $PIN == true || $PIN == false ]] || { echo 'MOQDEV_PIN must be true or false' >&2; exit 2; }
[[ $CC == delay || $CC == loss || $CC == none ]] || { echo 'MOQDEV_CC must be delay, loss or none' >&2; exit 2; }
export TOKIO_WORKER_THREADS=${MOQDEV_AUX_THREADS:-2}
# workers: WORKERS single-threaded QUIC workers; uring: the same on io_uring;
# shared: moq-dev's default work-stealing runtime with WORKERS threads.
case $RUNTIME in
  workers) N=$WORKERS URING=false ;;
  uring)   N=$WORKERS URING=true ;;
  shared)  N=0 URING=false; TOKIO_WORKER_THREADS=$WORKERS ;;
  *) echo 'MOQDEV_RUNTIME must be workers, uring or shared' >&2; exit 2 ;;
esac
export RUST_LOG=${RUST_LOG:-warn}
ulimit -n 1048576
exec "$DIR/moq-relay-dev" "$DIR/perf.toml" \
  --listen "0.0.0.0:$PORT" --internal-listen "127.0.0.1:$ADMIN" \
  --runtime-workers "$N" --runtime-pin="$PIN" --runtime-io-uring="$URING" --quic-congestion-control "$CC"
