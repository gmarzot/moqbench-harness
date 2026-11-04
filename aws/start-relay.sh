#!/bin/bash
# Start the moqx relay for the aiomoqt bench. Mirrors scripts/perf/perf-test.sh
# relay args; logs and resolved config land under ~/gmarzot-bench/.
set -euo pipefail

REPO="$HOME/moqx"
LOGD="$HOME/gmarzot-bench/logs"
BIN="$REPO/build/default/moqx"
mkdir -p "$LOGD"

if pgrep -x moqx >/dev/null; then
  echo "moqx already running (pid $(pgrep -x moqx | tr '\n' ' ')) - not starting another"
  exit 0
fi

ulimit -n 65536 2>/dev/null || true

# Non-persistent; reverts on reboot. Values are the relay's own recommendation.
sudo sysctl -qw net.core.rmem_max=16777216 net.core.wmem_max=16777216 net.core.rmem_default=4194304 net.core.wmem_default=4194304 net.core.netdev_max_backlog=65536 net.core.optmem_max=1048576 net.ipv4.udp_rmem_min=262144 net.ipv4.udp_wmem_min=262144 2>/dev/null || true

export MOQX_RELAY_ID="gmarzot-bench"
export MOQX_RESOLVED_CONFIG="$LOGD/relay.yaml"
export MALLOC_CONF="thp:always"

UDPBUF="$(cat /proc/sys/net/core/wmem_max 2>/dev/null || echo 1048576)"

ARGS=(--insecure --no-cache --ignore-path-mtu)
ARGS+=(--bin "$BIN")
ARGS+=(--bind "::" --port 4433 --admin-port 19701 --endpoint /moq-relay)
ARGS+=(--threads 48 --cc bbr2 --udp-buffer "$UDPBUF")
ARGS+=(--relay-thread --local-forwarders -j auto)

cd "$REPO"
setsid ./scripts/moqx-run.sh "${ARGS[@]}" >"$LOGD/relay.log" 2>&1 </dev/null &
echo $! >"$LOGD/relay.pid"

deadline=$(( $(date +%s) + 20 ))
until curl -sf http://localhost:19701/info >/dev/null 2>&1; do
  if (( $(date +%s) >= deadline )); then
    echo "ERROR: relay not ready after 20s; tail of log:" >&2
    tail -30 "$LOGD/relay.log" >&2
    exit 1
  fi
  sleep 0.2
done

echo "relay ready: pid=$(pgrep -x moqx | head -1) port=4433 admin=19701 endpoint=/moq-relay"
curl -s http://localhost:19701/info
