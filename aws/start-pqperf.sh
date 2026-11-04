#!/bin/bash
# picoquicdemo QUIC-PERF server (draft-banks-quic-performance, ALPN "perf").
# Native C picoquic with its own sockloop -- so unlike aiopquic it uses the
# UDP_SEGMENT/GSO send path. This is the control that separates "picoquic the
# library" from "aiopquic's asyncio transport".
P="$HOME/gmarzot-bench/src/aiopquic/third_party/picoquic"
PORT="${1:-4443}"

pkill -f picoquicdemo 2>/dev/null
sleep 1

cd "$P" || exit 1
setsid ./build/picoquicdemo -p "$PORT" -c certs/cert.pem -k certs/key.pem \
  -S . -D -w /tmp >/tmp/pqserver.log 2>&1 </dev/null &
sleep 3

echo "pid=$(pgrep -f picoquicdemo | head -1)"
ss -ulnp 2>/dev/null | grep "$PORT" | head -2
echo "--- log ---"
tail -8 /tmp/pqserver.log
