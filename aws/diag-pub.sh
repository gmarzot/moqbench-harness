#!/bin/bash
# Foreground publisher diagnostic. Lives in a file so the pkill pattern cannot
# match the invoking command line.
BENCH="$HOME/gmarzot-bench"
RELAY="${1:-127.0.0.1}"
SECS="${2:-30}"

pkill -f aiomoqt.tools.adaptive_bench 2>/dev/null
sleep 1

cd "$BENCH"
timeout "$SECS" ./venv/bin/python -u -m aiomoqt.tools.adaptive_bench \
  "moqt://${RELAY}:4433/moq-relay" -k --role pub --mode subs \
  --sub-mbps 5 -s 20800 -N gmbench -T v1 -t $((SECS - 5)) -d \
  >/tmp/pubfg.log 2>&1
echo "exit=$?"
echo "=== output ==="
tail -60 /tmp/pubfg.log
