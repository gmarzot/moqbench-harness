#!/bin/bash
# aiomoqt adaptive_bench subscriber ramp (controller lives here).
# Usage: sub.sh <relay_ip> <mbps> <objsize> <start> <step> <max> <slo_ms> <dur_s> [ns] [track]
set -euo pipefail

BENCH="$HOME/gmarzot-bench"
PY="$BENCH/venv/bin/python"

RELAY="${1:?relay ip}"
MBPS="${2:-5}"
OBJSZ="${3:-20800}"
START="${4:-50}"
STEP="${5:-50}"
MAX="${6:-2000}"
SLO="${7:-512}"
DUR="${8:-600}"
JOIN="${9:-2}"
STAGGER="${10:-0.01}"
NS="${11:-gmbench}"
TRACK="${12:-v1}"

mkdir -p "$BENCH/logs"
TAG="${NS}-${MBPS}mbps"
LOG="$BENCH/logs/sub-${TAG}.log"
CSV="$BENCH/logs/sub-${TAG}.csv"

pkill -f aiomoqt.tools.adaptive_bench 2>/dev/null || true
sleep 1

ulimit -n 200000 2>/dev/null || ulimit -n 65536 2>/dev/null || true
echo "nofile=$(ulimit -n)"

ARGS=("moqt://${RELAY}:4433/moq-relay" -k)
ARGS+=(--role sub --mode subs)
ARGS+=(-N "$NS" -T "$TRACK")
ARGS+=(--sub-mbps "$MBPS" -s "$OBJSZ")
ARGS+=(--start-subs "$START" --step-subs "$STEP" --max-subs "$MAX")
ARGS+=(-l "$SLO" --interval 5 -j "$JOIN" -S "$STAGGER" --mp)
ARGS+=(-t "$DUR" --report "$CSV")

setsid "$PY" -u -m aiomoqt.tools.adaptive_bench "${ARGS[@]}" >"$LOG" 2>&1 </dev/null &
echo $! >"$BENCH/logs/sub.pid"

sleep 20
echo "=== subscriber log (first 20s) ==="
tail -40 "$LOG"
