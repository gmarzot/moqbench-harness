#!/bin/bash
# Remove our benchmark footprint. Lives in a file so pkill patterns cannot
# match the invoking command line. Deliberately does NOT touch a pre-existing
# `moqx serve` relay.
pkill -f picoquicdemo 2>/dev/null
pkill -f aiomoqt.tools 2>/dev/null
pkill -x iperf3 2>/dev/null
sleep 2
pkill -9 -f picoquicdemo 2>/dev/null
pkill -9 -f aiomoqt.tools 2>/dev/null
pkill -9 -x iperf3 2>/dev/null
sleep 1

echo "host: $(hostname)"
echo -n "ours still running: "
pgrep -c -f "picoquicdemo|aiomoqt.tools" 2>/dev/null || echo 0
echo -n "iperf3 still running: "
pgrep -xc iperf3 2>/dev/null || echo 0
echo -n "pre-existing moqx relay (left alone): "
pgrep -f "moqx serve" >/dev/null && echo "RUNNING" || echo "none"
echo "--- qdisc ---"
tc qdisc show dev eth0
echo "--- listeners (non-loopback) ---"
ss -ulnp 2>/dev/null | grep -vE "127.0.0|::1" | tail -n +2 | awk '{print $5}' | sort -u | head -6
