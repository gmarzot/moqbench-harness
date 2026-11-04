#!/usr/bin/env bash
# Run ON the selected relay host (or via testbed-ssh.sh relay 'bash -s').
# Installs an isolated, pinned moqx release; never stops other relays or tunes
# host-wide sysctls. --print-config emits the exact configuration for review.
set -euo pipefail
VERSION=v0.3.6
RELEASE_SHA=d0840cd8ef7ab7c9ba63648c398731ec1fcf8512a4d1d349036bea56c71ab16f
UNIT=moqx-4433
CPUS=${MOQX_CPUS:-0,10,12,14}
print_config() {
cat <<'YAML'
relay_id: moqx-4433-baseline
threads: 3
use_relay_thread: true
use_local_forwarders: true
mvfst_bpf_steering: false
listener_defaults:
  quic:
    max_data: 67108864
    max_stream_data: 16777216
    max_uni_streams: 8192
    max_bidi_streams: 16
    idle_timeout_ms: 30000
    min_ack_delay_us: 1000
    max_ack_delay_us: 25000
  mvfst:
    pacing_enabled: true
    enable_gso: true
    num_gro_buffers: 8
    max_conn_packets_sent_per_loop: 48
    max_server_recv_packets_per_loop: 64
    udp_socket_buffer_bytes: 33554432
    ignore_path_mtu: true
    bbr2:
      exit_startup_on_loss: true
      enable_recovery_in_startup: true
      enable_recovery_in_probe_states: true
listeners:
  - name: relay
    quic_stack: mvfst
    moqt_versions: [16]
    udp:
      socket:
        address: "0.0.0.0"
        port: 4433
    tls:
      insecure: true
    endpoint: /moq-relay
    quic:
      cc_algo: bbr2
services:
  default:
    match:
      - authority: {any: true}
        path: {exact: /moq-relay}
      - authority: {any: true}
        path: {prefix: /}
service_defaults:
  cache:
    enabled: false
    max_tracks: 1000
    max_groups_per_track: 100
admin:
  port: 4501
  address: "127.0.0.1"
  plaintext: true
YAML
}
if [[ ${1:-} == --print-config ]]; then print_config; exit 0; fi
[[ $# == 0 ]] || { echo 'Usage: moqx-standalone.sh [--print-config]' >&2; exit 2; }
[[ $(uname -m) == x86_64 ]] || { echo 'This pinned artifact is x86_64 only.' >&2; exit 1; }
[[ $CPUS =~ ^[0-9]+(,[0-9]+)*$ ]] || { echo 'MOQX_CPUS must be comma-separated CPU IDs.' >&2; exit 2; }
for cmd in python3 curl tar sha256sum ss taskset systemctl; do command -v "$cmd" >/dev/null; done
sudo -n true
if systemctl is-active --quiet "$UNIT"; then
  echo "$UNIT is already running; no changes made. Inspect with: systemctl status $UNIT" >&2
  exit 1
fi
if [[ -n $(ss -H -lun 'sport = :4433') || -n $(ss -H -ltn 'sport = :4501') ]]; then
  echo 'Port 4433/UDP or 4501/TCP is occupied; no listener will be replaced.' >&2; exit 1
fi
ROOT="$HOME/Projects/moqx-standalone"
# Paths enter a systemd unit below; reject unit specifiers and whitespace.
[[ $ROOT =~ ^/[a-zA-Z0-9_./-]+$ ]] || { echo 'Unsupported installation path.' >&2; exit 1; }
mkdir -p "$ROOT"
WORK=$(mktemp -d "$ROOT/.stage.XXXXXX")
started=0
cleanup() {
  rc=$?
  if (( rc != 0 && started )); then sudo -n systemctl stop "$UNIT" || true; fi
  rm -rf "$WORK"
  exit "$rc"
}
trap cleanup EXIT
# Preserve identity and affinity of the existing listener on 4533, if present.
ss -H -lunp 'sport = :4533' > "$WORK/protected-socket.txt"
python3 - "$CPUS" "$WORK" <<'PY'
import json, os, pathlib, re, sys
cpus = {int(x) for x in sys.argv[1].split(',')}
w = pathlib.Path(sys.argv[2])
if len(cpus) != 4 or not cpus <= os.sched_getaffinity(0):
    raise SystemExit('Choose exactly four available CPUs in MOQX_CPUS.')
def core(cpu):
    p=pathlib.Path(f'/sys/devices/system/cpu/cpu{cpu}/topology')
    return ((p/'physical_package_id').read_text().strip(), (p/'core_id').read_text().strip())
cores = {core(cpu) for cpu in cpus}
if len(cores) != 4:
    raise SystemExit('MOQX_CPUS must select four distinct physical cores, not SMT siblings.')
snap = {}
socket_info=(w/'protected-socket.txt').read_text()
if socket_info.strip() and not re.search(r'pid=(\d+)', socket_info):
    raise SystemExit('Cannot inspect the existing 4533 listener owner; no changes made.')
for pid in set(re.findall(r'pid=(\d+)', (w/'protected-socket.txt').read_text())):
    p=pathlib.Path('/proc')/pid
    affinities={t.name: sorted(os.sched_getaffinity(int(t.name))) for t in (p/'task').iterdir()}
    existing={cpu for allowed in affinities.values() for cpu in allowed}
    if cores & {core(cpu) for cpu in existing}:
        raise SystemExit('Chosen CPUs overlap the existing 4533 listener affinity. Choose spare cores; its affinity will not be changed.')
    snap[pid]={'start': p.joinpath('stat').read_text().rsplit(')',1)[1].split()[19], 'affinities': affinities}
(w/'protected-processes.json').write_text(json.dumps(snap))
for key in ('rmem_max','wmem_max'):
    limit=int(pathlib.Path('/proc/sys/net/core',key).read_text())
    if limit < 33554432:
        raise SystemExit(f'net.core.{key}={limit}: need >=33554432 for this profile. Host tuning was not changed.')
print('CPU topology, spare cores and socket buffer limits checked.')
PY
# Only install the allocator if missing; do not rebuild the relay on a busy host.
JEMALLOC=$(/sbin/ldconfig -p | awk '/libjemalloc.so.2 .*x86-64/ && !found {print $NF; found=1}')
if [[ -z $JEMALLOC ]]; then
  sudo -n env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=120 install -y libjemalloc2
  JEMALLOC=$(/sbin/ldconfig -p | awk '/libjemalloc.so.2 .*x86-64/ && !found {print $NF; found=1}')
fi
[[ -f $JEMALLOC && $JEMALLOC =~ ^/[a-zA-Z0-9_./-]+$ ]]
curl -fLsS --connect-timeout 15 --max-time 180 \
  "https://github.com/openmoq/moqx/releases/download/$VERSION/moqx-linux-amd64.tar.gz" -o "$WORK/release.tar.gz"
printf '%s  %s\n' "$RELEASE_SHA" "$WORK/release.tar.gz" | sha256sum -c -
mkdir "$WORK/release"
tar xzf "$WORK/release.tar.gz" -C "$WORK/release"
BIN="$WORK/release/bin/moqx"
env -u LD_LIBRARY_PATH -u LD_PRELOAD "$BIN" --version
print_config > "$WORK/config.yaml"
env -u LD_LIBRARY_PATH LD_PRELOAD="$JEMALLOC" "$BIN" validate-config --strict_config --config "$WORK/config.yaml"
DEST="$ROOT/$VERSION"
if [[ -e $DEST ]]; then
  diff -qr "$WORK/release" "$DEST" >/dev/null || {
    echo "$DEST differs from the verified release; refusing to overwrite it." >&2; exit 1;
  }
else
  mv "$WORK/release" "$DEST"
fi
install -m 644 "$WORK/config.yaml" "$ROOT/config-4433.yaml"
cp "$WORK/protected-processes.json" "$ROOT/protected-processes.json"
{
  echo "version=$VERSION release_sha256=$RELEASE_SHA cpus=$CPUS"
  sha256sum "$DEST/bin/moqx" "$ROOT/config-4433.yaml"
  lscpu -e=CPU,CORE,SOCKET,NODE
} > "$ROOT/build-4433.txt"
USER_NAME=$(id -un)
cat > "$WORK/$UNIT.service" <<EOF
[Unit]
Description=Standalone moqx benchmark relay on UDP 4433
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$ROOT
ExecStart=$DEST/bin/moqx serve --strict_config --config $ROOT/config-4433.yaml --logging=.=WARN
Environment=LD_PRELOAD=$JEMALLOC
Environment=GLOG_minloglevel=1
Environment=GLOG_v=0
Environment=GLOG_logtostderr=1
UnsetEnvironment=LD_LIBRARY_PATH
CPUAffinity=${CPUS//,/ }
LimitNOFILE=1048576
Restart=no
KillSignal=SIGTERM
TimeoutStopSec=20
[Install]
WantedBy=multi-user.target
EOF
sudo -n systemd-analyze verify "$WORK/$UNIT.service"
sudo -n install -m 644 "$WORK/$UNIT.service" "/etc/systemd/system/$UNIT.service"
sudo -n systemctl daemon-reload
started=1
sudo -n systemctl start "$UNIT"
# Do not enable boot startup for this ad-hoc test service.
ready=0
for attempt in {1..20}; do
  if curl -fsS --max-time 1 http://127.0.0.1:4501/info >/dev/null; then ready=1; break; fi
  systemctl is-active --quiet "$UNIT" || break
  sleep 1
done
if (( ! ready )); then
  sudo -n journalctl -u "$UNIT" -n 50 --no-pager >&2
  sudo -n systemctl stop "$UNIT"
  exit 1
fi
python3 - "$ROOT/protected-processes.json" <<'PY'
import json, os, pathlib, sys
for pid, expected in json.load(open(sys.argv[1])).items():
    p=pathlib.Path('/proc')/pid
    assert p.joinpath('stat').read_text().rsplit(')',1)[1].split()[19] == expected['start'], 'Protected relay restarted'
    for tid, allowed in expected['affinities'].items():
        assert sorted(os.sched_getaffinity(int(tid))) == allowed, 'Protected thread affinity changed'
print('Existing 4533 listener process and thread affinities unchanged.')
PY
pid=$(systemctl show "$UNIT" --property MainPID --value)
awk '/libjemalloc/ && !found {print; found=1} END {if (!found) exit 1}' "/proc/$pid/maps"
ss -lunp 'sport = :4433'
printf '\nmoqx running: PID %s; UDP 4433; admin 127.0.0.1:4501; CPUs %s\n' "$pid" "$CPUS"
printf 'Logs: journalctl -u %s -f\nStop only this relay: sudo systemctl stop %s\n' "$UNIT" "$UNIT"
