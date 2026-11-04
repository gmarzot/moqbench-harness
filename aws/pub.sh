#!/bin/bash
# aiomoqt adaptive_bench publisher (fixed-rate) for split-host subs-mode runs.
# Usage: pub.sh <relay_ip> <mbps> <object_size> <duration_s> [namespace] [track]
set -euo pipefail

BENCH="$HOME/gmarzot-bench"
PY="$BENCH/venv/bin/python"

RELAY="${1:?relay ip}"
MBPS="${2:-5}"
OBJSZ="${3:-20800}"
DUR="${4:-900}"
NS="${5:-gmbench}"
TRACK="${6:-v1}"

mkdir -p "$BENCH/logs"
LOG="$BENCH/logs/pub-${NS}.log"

pkill -f aiomoqt.tools.adaptive_bench 2>/dev/null || true
sleep 1

ulimit -n 65536 2>/dev/null || true

ARGS=("moqt://${RELAY}:4433/moq-relay" -k)
ARGS+=(--role pub --mode subs)
ARGS+=(--sub-mbps "$MBPS" -s "$OBJSZ")
ARGS+=(-N "$NS" -T "$TRACK")
ARGS+=(-t "$DUR")

setsid "$PY" -u -m aiomoqt.tools.adaptive_bench "${ARGS[@]}" >"$LOG" 2>&1 </dev/null &
echo $! >"$BENCH/logs/pub.pid"

sleep 6
echo "=== publisher log ==="
cat "$LOG"
