#!/bin/bash
# Foreground subscriber diagnostic: 2 subscribers, debug on, single process.
BENCH="$HOME/gmarzot-bench"
RELAY="${1:?relay ip}"
SECS="${2:-35}"

pkill -f aiomoqt.tools.adaptive_bench 2>/dev/null
sleep 1

cd "$BENCH"
timeout "$SECS" ./venv/bin/python -u -m aiomoqt.tools.adaptive_bench \
  "moqt://${RELAY}:4433/moq-relay" -k --role sub --mode subs \
  -N gmbench -T v1 --sub-mbps 5 -s 20800 \
  --start-subs 2 --step-subs 2 --max-subs 2 \
  -l 512 --interval 5 -t $((SECS - 5)) -d \
  >/tmp/subfg.log 2>&1
echo "exit=$?"
echo "=== output ==="
tail -70 /tmp/subfg.log
