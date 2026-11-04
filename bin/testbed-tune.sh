#!/usr/bin/env bash
# Network tuning that does NOT survive a reboot or a reshape.
#
# The sysctls land via cloud-init (/etc/sysctl.d/99-quic-relay-tuning.conf) and
# persist. Everything here lives on the device or in per-queue sysfs, is reset
# when the instance reboots or changes shape, and reverts silently — a measured
# run on an untuned host looks like a real result. Run this after every apply,
# reboot, or resize, and again in --check mode before trusting numbers.
#
#   ./bin/testbed-tune.sh              # apply to relay, clients and probes, then verify
#   ./bin/testbed-tune.sh --check      # report only, change nothing
#   ./bin/testbed-tune.sh --mtu-only   # only pin the MTU: an otherwise untuned baseline
#   ./bin/testbed-tune.sh --host 203.0.113.10
#   QDISC=fq_codel ./bin/testbed-tune.sh
#
# Apply also raises the thread and process limits ~10k subscribers per box
# need, and on AWS (the inventory's provider, or PROVIDER=aws) points chrony at
# Amazon Time Sync so cross-host timestamps line up. Both persist; --check
# reports them.
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
source ./lib/dotenv.sh
MODE=apply
HOSTS=()
while [[ $# -gt 0 ]]; do
  case $1 in
    --check) MODE=check; shift ;;
    --mtu-only) MODE=mtu; shift ;;
    --host)  HOSTS+=("$2"); shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
# Every host by default: an untuned second client or probe produces exactly the
# plausible-but-wrong numbers this script exists to prevent.
if [[ ${#HOSTS[@]} -eq 0 ]]; then
  source ./lib/hosts.sh
  HOSTS=("$RELAY" "${CLIENTS[@]}" ${PROBES[@]+"${PROBES[@]}"})
fi
KEY=${KEY:-$HOME/.ssh/id_ed25519}   # --host without a testbed selected
RUSER=${RUSER:-ubuntu}
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"

# Qdisc choice matters more than it looks. `fq` — the usual QUIC answer —
# classifies by socket, and a relay serves every subscriber from one UDP
# socket, so all of them collapse into a single flow capped at flow_limit
# 100p. fq_codel hashes the 5-tuple instead, giving each subscriber its own
# queue, so one backlogged peer cannot head-of-line the rest.
#
# AQM is deliberately relaxed (target/interval well above default): we are
# measuring the relay, and codel dropping into a standing queue would show up
# as retransmits. Raise flows/limit past subscriber count for the same reason.
# QDISC=pfifo_fast is the neutral baseline, QDISC=fq the (bad) comparison.
QDISC=${QDISC:-fq_codel}
QD_FLOWS=${QD_FLOWS:-65536}
QD_LIMIT=${QD_LIMIT:-200000}
QD_TARGET=${QD_TARGET:-20ms}
QD_INTERVAL=${QD_INTERVAL:-200ms}
QD_MEMLIMIT=${QD_MEMLIMIT:-536870912}
# Emulated one-way delay, milliseconds. Applied on BOTH hosts, so the resulting
# RTT is twice this. 0 leaves the native path alone. netem installs as the root
# qdisc and would otherwise discard the fq_codel tuning below, so fq_codel is
# attached as its child rather than replaced.
DELAY_MS=${DELAY_MS:-0}
TXQLEN=${TXQLEN:-20000}
RFS_ENTRIES=${RFS_ENTRIES:-65536}
# Wire packets stay at Internet size. OCI hands VNICs a 9000-byte MTU over
# DHCP, which lets TCP inside the VCN (and any QUIC stack doing path-MTU
# discovery) use jumbo frames that no real subscriber path carries, inflating
# every number. This is not a tuning choice, so --mtu-only applies it too.
MTU=${MTU:-1500}
PROVIDER=${PROVIDER:-$(python3 -c 'import json; print(json.load(open(".testbed/inventory.json")).get("provider", ""))' 2>/dev/null)}

cat > /tmp/.tune_remote.sh <<'REMOTE'
set -uo pipefail
MODE=$1 QDISC=$2 QD_FLOWS=$3 QD_LIMIT=$4 TXQLEN=$5 RFS_ENTRIES=$6
QD_TARGET=$7 QD_INTERVAL=$8 QD_MEMLIMIT=$9 DELAY_MS=${10} MTU=${11} PROVIDER=${12}
IF=$(ip -o -4 route show to default | awk '{print $5}' | head -1)   # two default routes exist; take the first
NCPU=$(nproc)

# All-CPU hex mask for RPS, 32 bits per comma-separated group, high group first.
rps_mask() {
  local n=$1
  local groups=$(( (n + 31) / 32 )) out="" i bits
  for (( i = groups - 1; i >= 0; i-- )); do
    bits=$(( n - i * 32 )); (( bits > 32 )) && bits=32
    out+=$(printf '%08x' $(( (1 << bits) - 1 )))
    (( i > 0 )) && out+=","
  done
  echo "$out"
}

if [[ $MODE == apply || $MODE == mtu ]]; then
  # Now, and at every later DHCP renewal and boot: networkd would otherwise
  # re-apply the 9000 the lease offers. The drop-in sits beside whichever
  # .network file networkd uses for this interface.
  [[ $(cat /sys/class/net/"$IF"/mtu) == "$MTU" ]] || sudo ip link set dev "$IF" mtu "$MTU"
  NETFILE=$(networkctl status "$IF" 2>/dev/null | sed -n 's/.*Network File: *//p' | head -1)
  if [[ -n $NETFILE ]]; then
    sudo mkdir -p "/etc/systemd/network/$(basename "$NETFILE").d"
    printf '[Link]\nMTUBytes=%s\n\n[DHCPv4]\nUseMTU=false\n' "$MTU" |
      sudo tee "/etc/systemd/network/$(basename "$NETFILE").d/50-mtu.conf" >/dev/null
  else
    echo "$(hostname): no networkd file for $IF; MTU set until the next DHCP renewal or boot" >&2
  fi
fi

if [[ $MODE == apply ]]; then
  # virtio exposes at most one combined queue on small shapes; ask for one per
  # CPU and accept the refusal, since larger shapes do grant more.
  MAXQ=$(ethtool -l "$IF" 2>/dev/null | awk '/^Combined:/{print $2; exit}')
  [[ -n ${MAXQ:-} && $MAXQ -gt 1 ]] && sudo ethtool -L "$IF" combined "$((NCPU < MAXQ ? NCPU : MAXQ))" 2>/dev/null

  RXMAX=$(ethtool -g "$IF" 2>/dev/null | awk '/^RX:/{print $2; exit}')
  TXMAX=$(ethtool -g "$IF" 2>/dev/null | awk '/^TX:/{print $2; exit}')
  [[ -n ${RXMAX:-} ]] && sudo ethtool -G "$IF" rx "$RXMAX" tx "$TXMAX" 2>/dev/null

  sudo ip link set dev "$IF" txqueuelen "$TXQLEN"

  sudo tc qdisc del dev "$IF" root 2>/dev/null
  leaf() {  # <parent spec>: the chosen qdisc, with fq_codel's relaxed AQM
    case $QDISC in
      fq_codel) sudo tc qdisc add dev "$IF" $1 fq_codel \
                  limit "$QD_LIMIT" flows "$QD_FLOWS" target "$QD_TARGET" \
                  interval "$QD_INTERVAL" memory_limit "$QD_MEMLIMIT" noecn ;;
      *)        sudo tc qdisc add dev "$IF" $1 "$QDISC" ;;
    esac
  }
  NTXQ=$(ls /sys/class/net/"$IF"/queues/ | grep -c '^tx-')
  if [[ ${DELAY_MS:-0} -gt 0 ]]; then
    # netem's own queue must hold a full delay-bandwidth product or it drops:
    # at 10 Gbps and 50ms that is ~60k packets, so the limit is generous. netem
    # is a single root qdisc, so emulated delay always funnels every TX queue.
    sudo tc qdisc add dev "$IF" root handle 1: netem \
      delay "${DELAY_MS}ms" limit 200000
    leaf "parent 1:1 handle 10:"
  elif (( NTXQ > 1 )); then
    # One qdisc per hardware TX queue under mq, the way the kernel sets it up.
    # A single root qdisc over several queues makes every sending core take the
    # same lock: on the 2-queue E5 NIC it cut 4-sender UDP from ~6.9 to ~5.0
    # Gbps. A QUIC relay sends on many cores, so this is the case that matters.
    sudo tc qdisc add dev "$IF" root handle 1: mq
    for (( q = 1; q <= NTXQ; q++ )); do leaf "parent 1:$(printf %x "$q")"; done
  else
    leaf root
  fi

  # Receive work lands on one core per hardware RX queue. With fewer queues than
  # CPUs (E5.Flex gives 2 for 16) that is a handful of cores carrying every
  # packet, and a fan-out relay receives a lot of small ones: an ACK stream from
  # each subscriber. RPS spreads them over every CPU. RPS=0 for the A/B.
  NQ=$(ls /sys/class/net/"$IF"/queues/ | grep -c '^rx-')
  if [[ ${RPS:-1} == 1 && $NQ -lt $NCPU ]]; then
    sudo sysctl -qw net.core.rps_sock_flow_entries="$RFS_ENTRIES"
    for q in /sys/class/net/"$IF"/queues/rx-*; do
      echo "$(rps_mask "$NCPU")" | sudo tee "$q"/rps_cpus >/dev/null
      echo $(( RFS_ENTRIES / NQ )) | sudo tee "$q"/rps_flow_cnt >/dev/null
    done
  else
    for q in /sys/class/net/"$IF"/queues/rx-*; do echo 0 | sudo tee "$q"/rps_cpus >/dev/null; done
    sudo sysctl -qw net.core.rps_sock_flow_entries=0
  fi
fi

if [[ $MODE == apply ]]; then
  # One thread per subscriber in some load tools: lift kernel, PAM and systemd caps.
  printf 'kernel.threads-max=4194304\nkernel.pid_max=4194304\nvm.max_map_count=4194304\n' |
    sudo tee /etc/sysctl.d/61-moq-bench-threads.conf >/dev/null
  sudo sysctl -q -p /etc/sysctl.d/61-moq-bench-threads.conf
  printf '* soft nproc unlimited\n* hard nproc unlimited\n' | sudo tee /etc/security/limits.d/61-moq-bench-nproc.conf >/dev/null
  sudo mkdir -p /etc/systemd/system.conf.d /etc/systemd/logind.conf.d /etc/systemd/system/user-.slice.d
  printf '[Manager]\nDefaultTasksMax=infinity\n' | sudo tee /etc/systemd/system.conf.d/61-moq-bench-tasks.conf >/dev/null
  printf '[Login]\nUserTasksMax=infinity\n' | sudo tee /etc/systemd/logind.conf.d/61-moq-bench-tasks.conf >/dev/null
  printf '[Slice]\nTasksMax=infinity\n' | sudo tee /etc/systemd/system/user-.slice.d/61-moq-bench-tasks.conf >/dev/null
  sudo systemctl daemon-reload
  sudo systemctl set-property --runtime "user-$(id -u).slice" TasksMax=infinity
  if [[ $PROVIDER == aws ]]; then
    command -v chronyc >/dev/null || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q chrony >/dev/null
    want="server 169.254.169.123 prefer iburst minpoll 4 maxpoll 4"
    if [[ $(cat /etc/chrony/sources.d/aws-time-sync.sources 2>/dev/null) != "$want" ]]; then
      echo "$want" | sudo tee /etc/chrony/sources.d/aws-time-sync.sources >/dev/null
      sudo systemctl restart chrony
    fi
  fi
fi

NQ=$(ls /sys/class/net/"$IF"/queues/ | grep -c '^rx-')
printf '%-14s if=%-8s cpus=%-3s queues=%s/%s mtu=%s txqlen=%s\n' \
  "$(hostname)" "$IF" "$NCPU" "$NQ" "$(ls /sys/class/net/$IF/queues/ | grep -c '^tx-')" \
  "$(cat /sys/class/net/$IF/mtu)" "$(cat /sys/class/net/$IF/tx_queue_len)"
CUR_MTU=$(cat /sys/class/net/"$IF"/mtu 2>/dev/null)
(( ${CUR_MTU:-0} > MTU )) && echo "               WARNING: mtu $CUR_MTU above $MTU; jumbo frames would inflate every result"
tc qdisc show dev "$IF" | head -2 | sed 's/^/               qdisc: /' | cut -c1-130
printf '%-14s qdiscs: %s fq_codel under %s\n' "" "$(tc qdisc show dev "$IF" | grep -c fq_codel)" \
  "$(tc qdisc show dev "$IF" | awk '/root/{print $2; exit}')"
printf '%-14s rings: %s   rps=%s rfs=%s\n' "" \
  "$(ethtool -g "$IF" 2>/dev/null | awk '/Current hardware/{f=1} f&&/^RX:/{r=$2} f&&/^TX:/{t=$2} END{print r"/"t}')" \
  "$(cat /sys/class/net/$IF/queues/rx-*/rps_cpus 2>/dev/null | xargs | tr ' ' '/')" \
  "$(sysctl -n net.core.rps_sock_flow_entries)"
printf '%-14s sock buf: wmem_max=%s rmem_max=%s  nofile=%s  gso=%s\n' "" \
  "$(sysctl -n net.core.wmem_max)" "$(sysctl -n net.core.rmem_max)" "$(ulimit -Hn)" \
  "$(ethtool -k "$IF" 2>/dev/null | awk -F': ' '/^generic-segmentation-offload/{print $2}')"
printf '%-14s limits: threads-max=%s pid_max=%s max_map_count=%s nproc=%s TasksMax=%s\n' "" \
  "$(sysctl -n kernel.threads-max)" "$(sysctl -n kernel.pid_max)" "$(sysctl -n vm.max_map_count)" \
  "$(ulimit -Hu)" "$(systemctl show -p TasksMax --value "user-$(id -u).slice" 2>/dev/null)"
(( $(sysctl -n kernel.pid_max) >= 4194304 )) || echo "               WARNING: thread limits not raised; ~10k subscribers per box will fail to spawn"
if [[ $PROVIDER == aws ]]; then
  printf '%-14s chrony: %s\n' "" "$(chronyc -n sources 2>/dev/null | awk '/169.254.169.123/{print $1, $2, "+/-", $NF; f=1} END{if (!f) print "WARNING: not using Amazon Time Sync"}')"
fi
REMOTE

echo "mode=$MODE mtu=$MTU qdisc=$QDISC one-way-delay=${DELAY_MS}ms (RTT=$((DELAY_MS*2))ms) provider=${PROVIDER:-unknown}"
for H in "${HOSTS[@]}"; do
  $SSH "$RUSER"@"$H" 'bash -s' -- "$MODE" "$QDISC" "$QD_FLOWS" "$QD_LIMIT" \
    "$TXQLEN" "$RFS_ENTRIES" "$QD_TARGET" "$QD_INTERVAL" "$QD_MEMLIMIT" "$DELAY_MS" "$MTU" "${PROVIDER:-none}" \
    < /tmp/.tune_remote.sh 2>&1
done
rm -f /tmp/.tune_remote.sh
