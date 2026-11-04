#!/bin/bash
# Standalone MoQT publisher server (no relay). Restart-safe.
# usage: start-pubserver.sh [port] [objsize] [streams] [inflight_bytes]
BENCH="$HOME/gmarzot-bench"
PORT="${1:-4444}"
OBJ="${2:-32768}"
P="${3:-4}"
INFLIGHT="${4:-}"
CC="${5:-}"
CERTS="$BENCH/src/aiomoqt/certs"

pkill -f aiomoqt.tools.pub_server 2>/dev/null
sleep 1

ARGS=(--quic --bind "::" -p "$PORT" -s "$OBJ" -P "$P" -r 0 -g 10000)
ARGS+=(--cert "$CERTS/cert.pem" --key "$CERTS/key.pem")
[ -n "$INFLIGHT" ] && ARGS+=(--max-inflight-bytes "$INFLIGHT")
[ -n "$CC" ] && ARGS+=(--cc-algo "$CC")

setsid "$BENCH/venv/bin/python" -u -m aiomoqt.tools.pub_server "${ARGS[@]}" \
  >"$BENCH/logs/pubserver.log" 2>&1 </dev/null &

sleep 4
echo "pub_server pid=$(pgrep -f aiomoqt.tools.pub_server | head -1) port=$PORT obj=$OBJ P=$P inflight=${INFLIGHT:-default}"
tail -6 "$BENCH/logs/pubserver.log"
