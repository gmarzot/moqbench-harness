#!/usr/bin/env bash
# Measure the path between every node and the relay before attributing anything
# to a relay or a QUIC stack (README, "The one rule"). TCP and UDP, both
# directions, receiver-side numbers: first one node at a time, then every node
# at once against the relay's egress, the direction a fan-out relay saturates.
#
#   ./bin/bw-baseline.sh                     # ~5 min for 3 nodes at the defaults
#   DUR=20 UDP_PROCS=6 ./bin/bw-baseline.sh
#
# UDP senders run unlimited by default, which finds the ceiling: the loss
# column is then the senders' excess over what the path delivered, not path
# loss. UDP_RATE paces each sender instead (iperf3 -b, e.g. 1.6G; the total is
# UDP_RATE x UDP_PROCS), and the loss column becomes real loss at that rate.
#
#   DUR=7 ./bin/bw-baseline.sh                    # ~60 s per node instead of ~90
#   UDP_RATE=1.6G ./bin/bw-baseline.sh            # loss at a sustainable 6.4 Gbps
#   UDP_PROCS=8 UDP_RATE=0.8G ./bin/bw-baseline.sh   # same rate, half the load per receiver
#
# A UDP receiver here is one iperf3 thread doing a recv per datagram; past
# ~140k packets/s it overflows its own socket, which shows as UdpRcvbufErrors on
# the receiving host: the tool's loss, not the path's. UDP tests therefore get a
# 32 MB socket buffer (UDP_WINDOW, iperf3 -w), and more processes at a lower
# rate each keep every receiver within what one core can drain.
#
# UDP uses 1400 B datagrams, the only size comparable to QUIC (QUIC packets must
# fit the path MTU, so large-datagram numbers are unreachable for it). iperf3
# 3.9 is single-threaded, so one UDP process measures the sending core rather
# than the path: the udpN rows run UDP_PROCS processes on separate ports and
# sum them. Compare against the shape's ceiling, 1 Gbps per OCPU on E5.Flex.
#
# Every host must be at an Internet-sized MTU (1500), or TCP inside the VCN rides
# jumbo frames no real path carries; it refuses to run otherwise. OCI's DHCP
# default is 9000: ./bin/testbed-tune.sh pins 1500, or ./bin/testbed-tune.sh --mtu-only for an
# otherwise untuned baseline. ALLOW_JUMBO=1 measures anyway.
#
# Output: the table, plus results/bw-baseline-<stamp>/paths.csv and the raw
# iperf3 JSON per node.
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
source ./lib/dotenv.sh
source ./lib/hosts.sh
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
DUR=${DUR:-10}
N=${UDP_PROCS:-4}
UDP_RATE=${UDP_RATE:-0}
UDP_WINDOW=${UDP_WINDOW:-32M}
BASE=5201

# Every client and probe is measured against the relay (lib/hosts.sh).
NODES=() NAMES=()
for i in "${!CLIENTS[@]}"; do NODES+=("${CLIENTS[$i]}"); NAMES+=("client-$(printf %02d $(( i + 1 )))"); done
for i in "${!PROBES[@]}"; do NODES+=("${PROBES[$i]}"); NAMES+=("probe-$(printf %02d $(( i + 1 )))"); done

OUT=${OUTDIR:-results/bw-baseline-$(date +%Y%m%d-%H%M%S)}
mkdir -p "$OUT"
echo "relay $RELAY ($RPRIV); nodes: ${NAMES[*]}; ${DUR}s per test, UDP x$N processes" | tee "$OUT/run.txt"
if [[ $UDP_RATE == 0 ]]; then
  echo "UDP senders unlimited: loss is their excess over what was delivered, not path loss" | tee -a "$OUT/run.txt"
else
  echo "UDP senders paced at $UDP_RATE each (x$N): loss is real loss at that rate" | tee -a "$OUT/run.txt"
fi

# Every host must have finished first boot (cloud-init installs iperf3) and be
# at MAX_MTU or below on its primary interface.
MAX_MTU=${MAX_MTU:-1500}
for h in "$RELAY" "${NODES[@]}"; do
  timeout 600 $SSH -n "$RUSER@$h" 'cloud-init status --wait >/dev/null 2>&1; iperf3 --version | head -1
    IF=$(ip -o -4 route show to default | cut -d" " -f5 | head -1); echo "mtu $(cat /sys/class/net/$IF/mtu)"' > "$OUT/.ver-$h" 2>&1 &
done; wait
head -1 "$OUT/.ver-$RELAY" | sed 's/^/relay iperf3: /' | tee -a "$OUT/run.txt"
JUMBO=()
for i in -1 "${!NODES[@]}"; do
  if [[ $i == -1 ]]; then h=$RELAY n=relay; else h=${NODES[$i]} n=${NAMES[$i]}; fi
  m=$(sed -n 's/^mtu //p' "$OUT/.ver-$h"); echo "$n mtu ${m:-unknown}" >> "$OUT/run.txt"
  (( ${m:-0} > MAX_MTU )) && JUMBO+=("$n=$m")
done
echo "mtu: $(grep ' mtu ' "$OUT/run.txt" | awk '{print $1"="$3}' | xargs)"
if [[ ${#JUMBO[@]} -gt 0 && ${ALLOW_JUMBO:-0} != 1 ]]; then
  echo "refusing to measure: ${JUMBO[*]} above $MAX_MTU, so TCP would use jumbo frames." >&2
  echo "run ./bin/testbed-tune.sh, or ./bin/testbed-tune.sh --mtu-only for an otherwise untuned baseline" >&2
  exit 1
fi

# One iperf3 server per port on the relay: N per node, so the concurrent round
# never shares a port. Removed on exit, however the script ends.
NPORTS=$(( ${#NODES[@]} * N ))
stop_servers() { timeout 30 $SSH -n "$RUSER@$RELAY" 'pkill -x iperf3' >/dev/null 2>&1; }
trap stop_servers EXIT
UP=$(timeout 60 $SSH -n "$RUSER@$RELAY" "pkill -x iperf3; sleep 0.5
  for p in \$(seq $BASE $(( BASE + NPORTS - 1 ))); do iperf3 -s -D -p \$p; done; sleep 1; pgrep -xc iperf3")
[[ ${UP:-0} -ge $NPORTS ]] || { echo "only ${UP:-0} of $NPORTS iperf3 servers started on the relay" >&2; exit 1; }

run_node() {  # <index> <mode> [start epoch]
  local i=$1 mode=$2 dir=$OUT/${NAMES[$1]}/$2
  mkdir -p "$dir"
  timeout $(( 10 * DUR + 120 )) $SSH "$RUSER@${NODES[$i]}" \
    "bash -s -- $RPRIV $(( BASE + i * N )) $N $DUR $mode ${3:-0} $UDP_RATE $UDP_WINDOW" < remote/bw-baseline-node.sh | tar xzf - -C "$dir"
}

# Drop counters on both ends of every round (remote/net-counters.sh): where a
# loss was counted says which layer dropped it, and a loss nobody counted was
# dropped by the network in between.
snap() {  # <host> <file>
  timeout 30 $SSH "$RUSER@$1" 'bash -s' < remote/net-counters.sh > "$2" 2>/dev/null || true
}
for i in "${!NODES[@]}"; do
  echo "-- ${NAMES[$i]} alone, both directions (~$(( 8 * DUR + 10 ))s)"
  c=$OUT/${NAMES[$i]}/counters; mkdir -p "$c"
  snap "$RELAY" "$c/pair-relay-before"; snap "${NODES[$i]}" "$c/pair-node-before"
  run_node "$i" pair
  snap "$RELAY" "$c/pair-relay-after"; snap "${NODES[$i]}" "$c/pair-node-after"
done
# The concurrent round starts each phase at the same second on every node, by
# the relay's clock, so the aggregate is a real simultaneous load.
START_AT=$(( $(timeout 30 $SSH -n "$RUSER@$RELAY" 'date +%s') + 8 ))
echo "-- every node at once, relay -> nodes (~$(( 2 * DUR + 15 ))s)"
snap "$RELAY" "$OUT/conc-relay-before" &
for i in "${!NODES[@]}"; do snap "${NODES[$i]}" "$OUT/${NAMES[$i]}/counters/conc-node-before" & done; wait
for i in "${!NODES[@]}"; do run_node "$i" concurrent "$START_AT" & done; wait
snap "$RELAY" "$OUT/conc-relay-after" &
for i in "${!NODES[@]}"; do snap "${NODES[$i]}" "$OUT/${NAMES[$i]}/counters/conc-node-after" & done; wait

python3 - "$OUT" "$N" "$DUR" <<'PY' | tee -a "$OUT/run.txt"
import csv, glob, json, os, re, sys
out, n, dur = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])

def load(p):
    try:
        return json.load(open(p))
    except Exception as e:
        return {"error": f"unreadable {os.path.basename(p)}: {e}"}

def tcp(j):
    """Receiver-side Gbps and sender retransmits."""
    e = j["end"]
    return e["sum_received"]["bits_per_second"] / 1e9, e.get("sum_sent", {}).get("retransmits")

def udp(j):
    """Delivered packets x datagram size over the test time: receiver-side in
    either direction and in both the 3.9 and later JSON layouts."""
    e, size = j["end"], j["start"]["test_start"]["blksize"]
    s = e.get("sum_received") if "packets" in e.get("sum_received", {}) else e["sum"]
    pk, lost, secs = s["packets"], s["lost_packets"], s["seconds"] or 1
    return max(pk - lost, 0) * size * 8 / secs / 1e9, pk, lost

def measure(paths, proto):
    """(gbps, detail) summed over one or more iperf3 runs, or (None, error)."""
    gbps = retr = pk = lost = 0
    for p in paths:
        j = load(p)
        if "error" in j:
            return None, j["error"]
        if proto == "tcp":
            g, r = tcp(j); gbps += g; retr += r or 0
        else:
            g, a, b = udp(j); gbps += g; pk += a; lost += b
    if proto == "tcp":
        return gbps, f"retr {retr}"
    return gbps, f"loss {100 * lost / pk:.1f}%" if pk else "no packets"

def aligned(paths):
    """Aggregate receiver Gbps per wall-clock second, over the seconds every run
    covered. Summing per-run averages instead overstates a simultaneous load
    whenever the runs start at different times: one that runs alone at either
    end briefly has the whole link."""
    runs = []
    for p in paths:
        j = load(p)
        if "error" in j:
            return None, j["error"]
        t0 = j["start"]["timestamp"]["timesecs"]
        runs.append({t0 + int(i["sum"]["start"]): i["sum"]["bytes"] * 8 / i["sum"]["seconds"] / 1e9
                     for i in j["intervals"] if i["sum"]["seconds"] > 0})
    common = set.intersection(*(set(r) for r in runs)) if runs else set()
    # Start times are whole seconds, so the edges blur; a short overlap is all
    # edge (runs starting or finishing), and its sum means nothing.
    need = max(3, int(0.6 * dur))
    if len(common) < need:
        return None, f"runs overlapped only {len(common)} s of the {need} needed; senders did not run together"
    per = sorted(sum(r[t] for r in runs) for t in common)
    return per[len(per) // 2], f"median of {len(common)} s together, {per[0]:.2f}-{per[-1]:.2f}"

def counters(p):
    try:
        return {f[0]: int(f[1]) for f in (l.split() for l in open(p)) if len(f) == 2 and f[1].lstrip("-").isdigit()}
    except OSError:
        return None

def drops(name, before, after):
    """What one host counted between two snapshots, largest first."""
    b, a = counters(before), counters(after)
    if not a or b is None:
        return f"{name}: no counters"
    d = sorted(((k, a[k] - b.get(k, 0)) for k in a if a[k] - b.get(k, 0) > 0), key=lambda kv: -kv[1])
    return f"{name}: " + (", ".join(f"{k} +{v}" for k, v in d) if d else "nothing")

def cell(m):
    g, d = m
    return f"{g:6.2f} Gbps {d:<11}" if g is not None else f"ERROR {d}"[:28]

tests = [("tcp1", "tcp"), ("tcp8", "tcp"), ("udp1", "udp"), (f"udp{n}", "udp")]
rows = []
nodes = sorted(d for d in os.listdir(out) if os.path.isdir(os.path.join(out, d)))
print(f"\n{'':10} {'':6} {'relay -> node (fan-out)':<29} node -> relay (ingest)")
for node in nodes:
    pair = os.path.join(out, node, "pair")
    setup = glob.glob(os.path.join(pair, "setup.json"))
    if setup:
        print(f"{node:10} ERROR {load(setup[0])['error']}"); continue
    ping = open(os.path.join(pair, "ping.txt")).read() if os.path.exists(os.path.join(pair, "ping.txt")) else ""
    rtt = re.search(r"= [\d.]+/([\d.]+)/", ping)
    print(f"{node:10} {'rtt':6} {rtt.group(1) + ' ms avg' if rtt else 'no ping reply'}")
    for t, proto in tests:
        cells = {}
        for d in ("down", "up"):
            files = sorted(glob.glob(os.path.join(pair, f"{t}-{d}.json")) + glob.glob(os.path.join(pair, f"{t}-{d}-*.json")))
            m = measure(files, proto) if files else (None, "not run")
            cells[d] = m
            rows.append([node, "alone", t, "relay->node" if d == "down" else "node->relay", m[0], m[1]])
        print(f"{'':10} {t:6} {cell(cells['down']):<29} {cell(cells['up'])}")
    c = os.path.join(out, node, "counters")
    print(f"{'':10} {'drops':6} {drops('relay', f'{c}/pair-relay-before', f'{c}/pair-relay-after')}")
    print(f"{'':17} {drops(node, f'{c}/pair-node-before', f'{c}/pair-node-after')}")

print(f"\nevery node at once, relay -> nodes (the relay's egress ceiling):")
for t, proto in [("tcp8", "tcp"), (f"udp{n}", "udp")]:
    parts, every = [], []
    for node in nodes:
        files = sorted(glob.glob(os.path.join(out, node, "concurrent", f"{t}-down*.json")))
        every += files
        g, d = measure(files, proto) if files else (None, "not run")
        rows.append([node, "concurrent", t, "relay->node", g, d])
        parts.append(f"{node} {g:.2f}" if g is not None else f"{node} ERR")
    g, d = aligned(every)
    rows.append(["all", "concurrent", t, "relay->node", g, d])
    agg = f"{g:.2f} Gbps ({d})" if g is not None else f"no aggregate: {d}"
    print(f"{'':10} {t:6} {'  '.join(parts)}  | together {agg}")

print(f"{'':10} {'drops':6} {drops('relay', f'{out}/conc-relay-before', f'{out}/conc-relay-after')}")
for node in nodes:
    c = os.path.join(out, node, "counters")
    print(f"{'':17} {drops(node, f'{c}/conc-node-before', f'{c}/conc-node-after')}")
print("\ndrops: what each host counted during that round, all tests and both directions.\n"
      "Loss that no host counted was dropped in the network between them.")

with open(os.path.join(out, "paths.csv"), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["node", "round", "test", "direction", "gbps", "detail"])
    for r in rows:
        w.writerow(r[:4] + [f"{r[4]:.3f}" if r[4] is not None else ""] + [r[5]])
if any(r[4] is None and "connect" in str(r[5]) for r in rows):
    print("\nconnection failures: the relay's host firewall (cloud-init flushes INPUT on"
          " first boot) or the VCN security list; the raw errors are in the JSON files.")
print(f"\nwrote {out}/paths.csv")
PY
