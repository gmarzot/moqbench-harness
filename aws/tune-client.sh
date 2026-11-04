#!/bin/bash
# Non-persistent receive-path tuning for the load-generator box. Reverts on reboot.
set -euo pipefail

sudo sysctl -qw \
  net.core.rmem_max=134217728 \
  net.core.wmem_max=134217728 \
  net.core.rmem_default=33554432 \
  net.core.wmem_default=33554432 \
  net.core.netdev_max_backlog=250000 \
  net.core.optmem_max=1048576 \
  net.ipv4.udp_rmem_min=1048576 \
  net.ipv4.udp_wmem_min=262144 \
  net.core.somaxconn=65535

# NIC ring buffers to hardware max where supported.
MAXRX=$(ethtool -g ens5 2>/dev/null | awk '/^RX:/{print $2; exit}')
if [ -n "${MAXRX:-}" ]; then
  sudo ethtool -G ens5 rx "$MAXRX" 2>/dev/null || true
fi

echo "--- applied ---"
sysctl -n net.core.rmem_max net.core.netdev_max_backlog net.ipv4.udp_rmem_min | tr '\n' ' '
echo
ethtool -g ens5 2>/dev/null | tail -5 | head -2
