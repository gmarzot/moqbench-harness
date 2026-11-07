# MoQ benchmark harness — runbook

Command reference for driving a selected testbed. See `BENCHMARKING-GUIDE.md`
for methodology and `STATE-AND-PLAN.md` for generic workflow status. Keep
deployment-specific operational notes and raw results in ignored local storage.

Commands are in `bin/`; the examples run them from the repo root as
`./bin/<name>`. Testbeds are created and powered with `./bin/testbed.sh <deployment>`
(see `oci/README.md`); the drivers read whichever environment it last selected.

---

## 1. The short version

```bash
# once: your profile, SSH key and egress IP, and your default environment (both gitignored)
cp oci/envs/moqbench-testbed.local.tfvars.example oci/envs/moqbench-testbed.local.tfvars
cp .env.example .env                    # TESTBED_ENV=moqbench-testbed

./bin/testbed.sh fanout-1 up --env moqbench-testbed -y  # relay + 2 load generators + probe
./bin/testbed-provision.sh                              # aiomoqt, moq-bench everywhere; moqx release on the relay
./bin/relay-deploy.sh moqx v0.3.6                       # another moqx release, or <relay> <binary> for any relay
./bin/testbed-tune.sh                                   # REQUIRED after every boot/resize, every host
./bin/bw-baseline.sh                                    # TCP + UDP, every node <-> relay, both ways (~5 min)
SUBS=20 ./bin/relay-load.sh adaptive moqx 30            # the relay serves subscribers?
./bin/relay-capacity.sh --matrix                        # the gated measurement (moqperf: needs SRC=, below)
./bin/relay-load.sh adaptive moqx 120                   # or any other load generator, §5
./bin/testbed.sh fanout-1 stop                          # between sessions
```

On AWS the same commands apply; only the environment differs:

```bash
cp aws/envs/my-account.local.tfvars.example aws/envs/moqbench-aws.local.tfvars  # profile, account id, key, CIDR
./bin/testbed.sh aws-1 up --env moqbench-aws -y   # c6gn relay + 2 load generators, arm64
```

`stop`/`start` use `aws ec2 stop-instances`/`start-instances`; public IPs
change across them and `start` refreshes the host list. A moqx release or CI
tarball is matched to the relay's architecture (`MOQX_ASSET`,
`MOQX_SCRIPTS_REF` override the asset and the scripts' git ref).

`./bin/testbed-tune.sh` is not optional. Half the network tuning does not survive a reboot
and reverts silently, so an untuned run looks like a real result.

---

## 2. Lifecycle and cost

| action | command |
|---|---|
| create (name it anything) | `./bin/testbed.sh <deployment> up [--env <environment>] [-y]`; without `--env`, `TESTBED_ENV` from `.env` or the shell |
| resize | edit `oci/envs/<environment>.tfvars`, then `./bin/testbed.sh <deployment> up` |
| preview a change | `./bin/testbed.sh <deployment> plan` |
| current IPs | `./bin/testbed.sh <deployment> output` |
| stop (keep disks) | `./bin/testbed.sh <deployment> stop` |
| start again | `./bin/testbed.sh <deployment> start` |
| delete it | `./bin/testbed.sh <deployment> destroy [-y]` |
| list environments and deployments | `./bin/testbed.sh` |

Resizing is an in-place reboot — `terraform plan` should say **0 to destroy**.
If it says destroy, stop and find out why; the boot volume carries every
binary and re-provisioning is slow.

Stopping halts compute billing and leaves only boot volumes (~$8.50/mo for the
pair). Intra-region traffic is free, so long high-rate runs cost nothing in
transfer. Public IPs change on stop/start unless reserved.

Environments are listed by `./bin/testbed.sh` with no arguments; sizes are in
`oci/README.md`.

Service limits cap what an environment can ask for. Check the target tenancy's
current values (each shape family has its own pool):

```bash
oci limits value list --service-name compute --compartment-id <tenancy> \
  --availability-domain <AD> --all \
  --query 'data[?starts_with(name, `standard-e`)].{name:name,value:value}' --output table
```

A limit increase is requested in the console: Governance & Administration ->
Limits, Quotas and Usage -> service `compute`, scope the AD, pick the limit,
"Request a service limit increase".

---

## 3. Access

```bash
./bin/testbed-ssh.sh relay                       # or client-01, client-02, probe-01
./bin/testbed-ssh.sh client-01 'iperf3 --version'  # one command
```

Dashboards, without exposing anything publicly:

```bash
./bin/testbed-ssh.sh relay -N -L 9100:localhost:9100 -L 4500:localhost:4500
```

`:9100` is node_exporter (both hosts), `:4500` is moqx's admin server when the
relay is up — `/metrics`, `/state`, `/info`, `/config`, `/logs`. Point the local
Prometheus/Grafana stack (moqx PR #539) at them. For moqx's single-file
dashboard (`tools/metrics-dashboard.html`) use `bin/dash.py`, which proxies `/metrics` from the same
origin — moqx's admin server sends no CORS header, so a browser pointed
straight at it fails.

---

## 4. Tuning

```bash
./bin/testbed-tune.sh            # apply to both hosts, then report
./bin/testbed-tune.sh --check    # report only
QDISC=pfifo_fast ./bin/testbed-tune.sh    # untuned baseline, for an A/B
```

Sysctls persist via cloud-init (`/etc/sysctl.d/99-quic-relay-tuning.conf`).
`./bin/testbed-tune.sh --mtu-only` pins only the MTU, for an otherwise untuned
baseline. The MTU is 1500 on every host and that is not optional: OCI's DHCP
hands out 9000, and jumbo frames inside the VCN would inflate every TCP number,
and any QUIC stack doing path-MTU discovery, beyond what a real subscriber
path carries. `testbed-tune.sh` sets it and adds a networkd drop-in so DHCP cannot
raise it again; `bw-baseline.sh` refuses to run above 1500.

What `testbed-tune.sh` fixes is everything that does not: txqueuelen (resets to 1000),
qdisc (resets to `pfifo_fast`), RPS/RFS, NIC channel and ring counts.

Things worth knowing about this hardware:

- `fq` is the usual QUIC answer and is **wrong here**. It classifies by socket,
  and a relay serves every subscriber from one UDP socket, so all of them
  collapse into a single flow capped at `flow_limit 100p`. `fq_codel` hashes
  the 5-tuple, so each subscriber gets its own queue. AQM is deliberately
  relaxed (target 20ms) so codel does not drop into the measurement.
- **Few NIC queues.** The virtio NIC exposes very few queues: 1 on the
  smallest shapes, 2 on E5.Flex at 4 and 8 OCPU (16 vCPU), with 256-entry rings.
  Receive work therefore lands on one or two cores, so `testbed-tune.sh` enables RPS
  across every CPU whenever there are fewer queues than CPUs (`RPS=0` for an
  A/B). Re-check after resizing: `ethtool -l <if>`. The interface is `enp0s9`
  on the 8 OCPU hosts and `enp0s5` on the probe.
- **One qdisc per queue.** `fq_codel` goes under `mq`, one instance per
  transmit queue, not as a single root. A single root qdisc makes every
  sending core take one lock: on these NICs it cut 4-sender UDP throughput
  sharply, and a QUIC relay sends from many cores.

---

## 4b. Bandwidth baseline

`./bin/bw-baseline.sh` measures every node against the relay with iperf3: TCP
with 1 and 8 streams, UDP 1400 B datagrams with 1 and `UDP_PROCS` (default 4)
processes, in both directions, one node at a time. Then every node pulls from
the relay at once, which shows the relay's egress ceiling: the number a
fan-out result has to be read against. It records receiver-side rates only.

```bash
./bin/bw-baseline.sh                       # DUR=10 s per test
DUR=20 UDP_PROCS=6 ./bin/bw-baseline.sh    # longer, more UDP senders
```

How to read it:

- **UDP is the part that counts.** MoQT runs over QUIC over UDP, and a fan-out
  test publishes one track and sends it to N subscribers, so the
  relay -> node UDP rows and the all-nodes UDP aggregate are the ceiling. TCP
  is there to tell a path problem from a UDP-sender problem.
- **Expect the shape's ceiling:** E5.Flex is 1 Gbps per OCPU, so 8 Gbps for an
  8 OCPU host and 4 for the probe. TCP should come close in both directions.
- **`udp1` well below `udpN`** is iperf3 3.9 running out of one core, not the
  path. If `udpN` still trails TCP with little loss, raise `UDP_PROCS`.
- **High loss at a high rate** means the sender is outrunning the receiver or
  the link. The delivered number is the one that counts.
- **Loss in the UDP rows is excess or real, by mode.** With unlimited senders
  (the default) it is what they offered beyond what the path delivered. With
  `UDP_RATE=1.6G` (per sender) it is real loss at that rate.
- **The `drops` lines say where loss happened.** They give each host's drop
  counters over the round: `qdisc_dropped` is the sender's queue, `nic_*` and
  `if_rx_*` the receiver's NIC ring, `softnet_dropped` the receiver's backlog
  (spread it with RPS), `UdpRcvbufErrors` a receiver socket the application
  could not drain. `softnet_squeezed` counts the softirq running out of budget,
  not a drop. If neither host counted the loss, it happened in the network
  between them.
- **The concurrent total** is the most a relay on this host can send, whatever
  the stack. Quote it beside every throughput result from the testbed.

Results land in `results/bw-baseline-<stamp>/`: `run.txt` (the table),
`paths.csv`, and the raw iperf3 JSON per node.

## 5. Running measurements

```bash
./bin/relay-smoke.sh                 # all four relays, 50 subs, ~90s
./bin/relay-smoke.sh moqx            # one: moqx | cf | quinn | quiche

./bin/relay-capacity.sh moqx                     # capacity for one relay
./bin/relay-capacity.sh --matrix                 # every relay x both flavours
FLAVOUR=bandwidth ./bin/relay-capacity.sh moqx
THREADS=16 ./bin/relay-capacity.sh moqx
```

Results land in `results/cap-<stamp>/`: `capacity.csv` (one row per relay),
`probes.csv` (every probe, including the failures), `versions.txt` (binary
hashes and CPU model, so a published number can be reproduced).

**What capacity means.** The largest subscriber count where all three hold:
p99 object latency <= `SLO_MS`, resets and subscribe failures <= `MAX_RESETS`,
and the subscriber count sustained through the steady window. Aggregate
throughput alone would reward a relay that accepts everyone and serves them
badly, so it is not the metric.

Search: climb x3 until something fails, bisect to `TOL_PCT`, then re-run the
answer at `CONFIRM_DUR`. Latency near the knee varies a lot run to run — the
same point can land well inside the bound on one run and far over it on the
next — so the confirm matters and a single probe at the edge means little.

Knobs:

| variable | default | note |
|---|---|---|
| `THREADS` | 32 | relay worker threads, same for all four relays |
| `FLAVOUR` | fanout | `fanout` 3788 B ~0.9 Mbps/sub; `bandwidth` 66000 B = 16 Mbps/sub |
| `SLO_MS` | 512 | a native histogram bucket bound, so no interpolation |
| `MAX_RESETS` | 0 | resets + subscribe failures tolerated |
| `SEARCH_DUR` / `CONFIRM_DUR` | 25 / 40 | probe and confirmation seconds |
| `CLIENT_MAX_BUSY` | 85 | above this a point is `CLIENT_BOUND`, not a result |
| `OBJ_DELAY_MS` | 33 | see below — do not change without a client patch |

Statuses: `ok`, `SLO_<n>ms`, `SHED_<n>`, `RESETS_<n>`, `CLIENT_BOUND`,
`CLIENT_CORE_BOUND`, `PUBLISH_FAILED`, `NO_DATA`.

---

### Start moqx alongside an existing relay

This installs the checksum-pinned v0.3.6 Linux x86-64 release into
`~/Projects/moqx-standalone/v0.3.6` and starts only the `moqx-4433` systemd
service. Run from the harness directory on your laptop:

```bash
./bin/testbed-ssh.sh relay 'bash -s' < bin/moqx-standalone-host.sh
```

The host script uses passwordless sudo for its service and, only if missing,
`libjemalloc2`. It preserves the listener on UDP 4533 and checks that its
process identity and existing thread affinities remain unchanged. It refuses
occupied moqx ports, CPU overlap, or insufficient kernel socket-buffer limits;
it never changes another relay's affinity or host-wide sysctls. This is a
standalone path: do not use `testbed-provision.sh` or `relay-load.sh` to start
it alongside another running relay, since those scripts stop other relays.

Configuration:

- UDP 4433, raw QUIC and WebTransport (`/moq-relay`), draft 16, development TLS.
- Admin HTTP on **127.0.0.1:4501**, avoiding the other relay's admin port.
- mvfst/BBR2 with pacing, GSO, 8 GRO buffers, 48 send packets and 64 receive
  packets per loop, 32 MiB socket buffers, and PMTU probing disabled.
- Three IO workers plus a dedicated relay execution thread; local forwarders
  enabled and object caching disabled. jemalloc is preloaded and verified.
- Four separate cores, default logical CPUs `0,10,12,14`. This default suits
  a 16-vCPU host whose other relay is pinned to `2,4,6,8`; actual topology and
  the existing listener's affinities are checked before installation. Override
  with a remote environment assignment when needed:
  `./bin/testbed-ssh.sh relay 'MOQX_CPUS=0,2,4,6 bash -s' < bin/moqx-standalone-host.sh`.

Review the exact config locally with
`bash bin/moqx-standalone-host.sh --print-config`. The pinned release lacks
`write_limit_rtt_fraction` and `pacing_tick_interval_us`; this profile omits
those fields and validates with `--strict_config` before launch.

On the relay host:

```bash
systemctl status moqx-4433 --no-pager
journalctl -u moqx-4433 -f
curl -fsS http://127.0.0.1:4501/info
sudo systemctl stop moqx-4433     # stops only this service
sudo systemctl start moqx-4433
```

The installer does not enable the service at boot and refuses to overwrite
an active service. Configuration and artifact hashes are recorded in
`~/Projects/moqx-standalone/build-4433.txt`; the resolved config is
`config-4433.yaml` beside it. This is a performance-oriented starting profile,
not a measured optimum. For comparable scaling measurements, load only one
relay at a time: separate cores still share the NIC and memory bandwidth.

### Fixed subscriber population

`fixed` fills to `SUBS` total across the client hosts and maintains that
requested population until the duration cap. It does not add subscribers
beyond the target or back off when latency rises. Failed workers are replaced;
reported active subscribers can still fall below the requested population.

```bash
THREADS=3 RELAY_CPUS=2,4,6,8 PUB_HOST=relay SUB_HOSTS=clients \
SUBS=1200 SUB_MBPS=2 OBJ_SIZE=8333 GROUP=60 STEP_SUBS=50 JOIN=5 STAGGER=0.02 \
AIOMOQT_EXTRA="-P 1 --cc-algo bbr --latency-threshold 500" \
./bin/relay-load.sh fixed cf 600
```

Select the relay and any compatibility flags appropriate to that relay.
On two client hosts this requests 600 subscriptions each, 2.4 Gbps total
payload. Duration includes filling: with these settings, allow roughly
60–70 seconds to reach the requested population, then observe the remainder.
The total must divide evenly across the hosts, and each host's count must
be a multiple of `STEP_SUBS`, the subscriber worker batch size.

The runner stages `patches/aiomoqt-fixed-subs.patch` against a temporary copy
of the installed `aiomoqt.tools.adaptive_bench` on each participant. It checks
patch application before stopping workloads; installed packages and native
transport binaries are untouched. This patch targets the 0.14 development
runner; a mismatched version fails preflight. `fixed-runner.txt` records the
source and temporary script hashes. No package rebuild is needed.

`RELAY_CPUS` applies affinity before relay startup, inherited by its threads.
Choose CPUs using the host's topology. `relay-affinity.txt` records every
thread's initial allowed CPU list. This option supports this script's
`fixed`, `adaptive`, `loadsim`, and `moqbench` paths.

The report leads with the combined active count and received Mbps.
`combined.csv` reports approximate sums with the whole timestamp span bounded to five seconds;
clocks must be synchronized. It reports the worst host's reported p90,
not a pooled percentile. `SLO_MS` (default 500 for this report) sets the
validation threshold; fixed mode never adapts to it or aborts early on SLO failure. The summary shows
full-population sample coverage and whether every client reached its
requested duration. Filling samples are included in coverage. A target
count alone is not evidence that all those subscribers received 2 Mbps.

### Any load generator: `relay-load.sh`

```bash
./bin/relay-load.sh <tool> <relay> [seconds]         # relay: moqx | cf | quinn | quiche

./bin/relay-load.sh moqperf  moqx 60                 # one gated point, same as relay-capacity.sh's
SUBS=400 ./bin/relay-load.sh moqbench moqx 60        # 400 moqbench_sub processes over the clients
PROFILE=moqbench/av-1080p.ini ./bin/relay-load.sh moqbench cf 60
SUB_MBPS=2 STEP_SUBS=20 SUBS=600 ./bin/relay-load.sh adaptive moqx 180   # ramp to 300 per client
SCENARIO=churn-storm ./bin/relay-load.sh loadsim quinn 120
PUB_HOST=probe ./bin/relay-load.sh adaptive moqx 120 # publish across the network from the probe
SUB_HOSTS=probe SUBS=20 ./bin/relay-load.sh loadsim moqx 60              # light probe only
SHAPE=720p SUBS=3000 ./bin/relay-load.sh moqperf-ramp moqx 300         # moqperf_test_client ramp, 720p objects
```

Results land in `results/load-<tool>-<relay>-<stamp>/`: `run.txt` (what ran,
host CPU with the client-bound flag, the tool's summary), relay and publisher
logs, a log per subscriber host, the tool's own reports (`loadreport.csv`,
per-subscriber moq-bench logs and `moqbench.csv`), and `versions.txt`.

| variable | default | note |
|---|---|---|
| `SUBS` | 200 | total, split evenly over the subscriber hosts |
| `THREADS` | 8 | relay worker threads (the 8 OCPU relay has 16 vCPU) |
| `PUB_HOST` | relay | `probe` publishes from the probe node over the network; `client[:N]` from load host N (default 1), which still subscribes |
| `SUB_HOSTS` | clients | `client:N` puts the subscribers on load host N, `probe` on the probe node |
| `DRAFT` | 16 | 18 only with moqx and a `MOQBENCH_DRAFT=18` build |
| `STAGGER` | 0.02 | seconds between joins on one host; spaces the QUIC Initials |
| `PROFILE` | moqbench/video-1080p.ini | moq-bench tracks; `start_delay` and duration are set per run |
| `SUB_MBPS`, `OBJ_SIZE`, `STEP_SUBS`, `JOIN` | 2, 8192, 10, 5 | adaptive_bench ramp |
| `AIOMOQT_ENV` | | environment for every aiomoqt process, e.g. `MALLOC_ARENA_MAX=2` |
| `SHAPE` | | `480p`, `720p`, `1080p`, `2160p` (`scenarios/shapes.json`): adaptive/fixed publish `--video SHAPE` instead of `SUB_MBPS`/`OBJ_SIZE`; required by `moqperf-ramp` |
| `LATENCY_MS`, `SHORTFALL` | 500, tool default | adaptive_bench `--latency-threshold`, `--shortfall-ratio` |
| `DELIVERY_TIMEOUT`, `MP_THREADS`, `MP_RAMP` | 500, 32, 64 | `moqperf-ramp` client: delivery timeout, threads, subscribers added per second |
| `SCENARIO`, `RATE` | one track, 30 | load_sim; a scenario's audiences run on every subscriber host |
| `AIOMOQT_EXTRA` | | extra flags for both aiomoqt tools, e.g. `--max-queued-bytes` |

### Watching a run

```bash
./bin/live-dashboard                      # then open http://127.0.0.1:8765/
./bin/live-dashboard --replay results/sweep-<name>/<run>
```

The page shows subscribers, worst latency against the SLO, relay egress, packets
and CPU, load-host busy against the 85% limit, and drop and NIC allowance counters.
It adds nothing to a host beyond one ssh connection following the files a run
already writes with a niced `tail`, so it can attach or detach at any time and a
failure cannot stop the run. It binds to localhost only.

moq-bench is one process per subscriber, so a client's ceiling is processes,
not connections. Watch the client-bound flag in `run.txt` before scaling
`SUBS` up. A relay kept out of this repo can be added through
`relays.local.sh` (see `relays.local.sh.example`); it then works here and in
`relay-capacity.sh` like the built-in four.

### Video-shape sweeps

A sweep is a plan of relay-load.sh points against moqx: aiomoqt adaptive_bench
(`aio`) or moxygen's moqperf_test_client (`mp`, the `moqperf-ramp` tool), a PGO
or regular moqx build, and a video shape from `scenarios/shapes.json` (480p,
720p, 1080p, 2160p at 1.10, 2.19, 4.30 and 16 Mbps per subscriber; aiomoqt's 2160p needs a build with `--video 4kuhd`). The plan's
`KEY=VALUE` lines set the environment of the points after them;
`scenarios/sweep-g3.plan` is the nine-point all-Graviton3 sweep (60 relay
threads, publisher on load host 2, draft 18).

```bash
AIOMOQT_SRC=1 ./bin/testbed-provision.sh       # aiopquic + aiomoqt dev heads; moxygen v0.3.7 test tools
./bin/testbed-tune.sh && ./bin/testbed-tune.sh --check   # also thread limits, chrony on AWS
MOQX_PGO_TARBALL=$PWD/moqx-pgo.tar.gz MOQX_REG_TARBALL=$PWD/moqx-reg.tar.gz \
  ./bin/sweep.sh scenarios/sweep-g3.plan         # results/sweep-g3/<run>/
./bin/sweep-report.py results/sweep-g3           # results/sweep-g3/datasets/*.json
```

A point line is `<tool> <build> <shape> <max_per_box> <seconds> <run>`. moqx is
redeployed with `relay-deploy.sh` when the build changes, from the named tarball
or else the release (`MOQX_RELEASE`, default latest). A point that already
passed is skipped, so rerunning the same plan resumes it. Run names follow
`<tool>-<build><threads>-<boxes>b-<shape>`, then departures from the standard
setup.

`sweep-report.py` writes the dashboard's four datasets: `runs.json` (one
summary per run), `latency.json` (adaptive_bench's 10 s rows, subscribers
summed and the worst host's latency), `sut.json` (relay CPU, NIC and UDP
errors every 5 s) and `loadgen.json` (load-tool cores and memory per
subscriber). A run's plateau is its samples within 5% of the peak subscriber
count; its `limit` says what saturated there: relay (a CPU at or over 90%), load
generator (a host at 85% or a CPU at 97%), both, or latency with no CPU
saturated. moqx runs with jemalloc and `MALLOC_CONF=thp:always` by default
(`MOQX_JEMALLOC=0`, `MOQX_THP=0` turn them off); `MOQX_WLRF` and `MOQX_PTI`
inject config keys releases through 0.3.7 do not accept, so they are unset by
default.

---

## 6. Traps that have already cost time

- **`testbed-tune.sh` after every boot or resize.** Silent reversion, plausible numbers.
- **Object rate is a delay, not a frequency.** Tuple field 9 is milliseconds
  between objects despite being named `objectFrequency`; 33 is ~30 obj/s.
  `moqperf_test_client` hardcodes it (`kObjectInterval`) with no flag, and
  moq-dev matches broadcasts by exact path, so changing `OBJ_DELAY_MS` makes
  the announced namespace disagree with what the client subscribes to and
  **only the moq-dev relays fail** — which reads as a relay difference.
- **Process names truncate at 15 chars**: `moq-relay-quiche` matches only as
  `moq-relay-quich`. Never `pkill -f <pattern>` where the pattern matches your
  own command line; it kills the shell.
- **The patched `moqtest_server`** must carry `--announce_namespace`. The
  copy staged on a shared build host may not, and every moq-dev run then silently
  gets zero subscribers. A `SRC=` pull of that host's tree brings that copy, so
  re-push that one binary by hand or fix the source copy.
- **Client URL path differs per relay**: moqx `/moq-relay`, cf `/`, moq-dev
  `/anon`. moq-dev also needs `--delivery_timeout 0` (it rejects the param).
- **Start the publisher immediately before the client** — its namespace
  registration expires with the relay's session idle timeout.
- **`uv venv` as root** symlinks Python into `/root` and leaves the venv
  unusable by `ubuntu`. Install as the login user.
- **Background SSH commands hold the channel open**; redirect to `/dev/null`
  and `setsid`, or the command never returns.
- **Never quote Mbps/core** without stating object size, subscriber count,
  relay-vs-endpoint, loopback-vs-NIC, and host. The same moqx binary spans
  a several-fold range across those axes with no code change.

## Current moq-dev relay

Use `./bin/moqdev-build.sh`, then `./bin/relay-deploy.sh moqdev` after the
testbed is running. `THREADS=16 ./bin/relay-load.sh adaptive moqdev 60` uses
16 QUIC workers with independent sockets, unpinned, with compiled-in jemalloc.
See [MOQDEV.md](MOQDEV.md) for source pins, build provenance, supported controls,
fixed-load commands and validation. Historical `quinn`/`quiche` builds stay separate.


### Fixed-run validation and time-series evidence

`relay-load.sh` now stages a read-only sampler on the relay and each subscriber
host before starting traffic. `telemetry-*.jsonl` retains one-second `/proc`
snapshots: per-core CPU, relay thread CPU and process identity, RSS, memory,
interface bytes/errors/drops, UDP counters, and softnet counters. For `moqdev`,
the relay sampler also requests loopback `/metrics` every five seconds, with a
0.5-second timeout; scrape failures remain visible in the evidence. The sampler
has a bounded lifetime and its own stop file. No load or relay settings change.

`telemetry-*.csv` and `telemetry-summary.txt` show interval CPU/network rates,
peak process cores, and UDP error deltas. NIC rates sum non-loopback interfaces;
on hosts with bridges/tunnels, inspect raw per-interface counters to avoid double
counting. JSONL retains individual thread and interface data. Sampling gaps,
missing data, relay disappearance or a changed PID start time fail collection.

Publisher/subscriber exit status and relay health are captured before cleanup;
collection failures produce a nonzero run exit. Interruptions attempt to preserve
partial logs and telemetry before stopping owned workloads. `collection.log`
records collection problems. `run-settings.txt` preserves the generated commands.

A fixed run reaching its duration limit is **not a capacity pass**. After the
first full population and a 10-second settling period, the default gate requires
at least 30 seconds of samples, with at least 95% meeting all these conditions:
full requested population, every host receiving at least 95% of its target rate,
every host reporting known p90 latency <= `SLO_MS` (default 500 ms), and zero
reported object loss. Steady sample gaps over 10 seconds fail validation.
`MIN_DELIVERY` and `MIN_STEADY_SECONDS` expose the delivery and hold thresholds.
The duration includes the ramp; choose it long enough to leave a useful hold.

`combined.json` records thresholds and verdict. Subscriber counts refer to
active slots, not independently verified receiving subscribers. Zero reported
loss does not prove complete delivery: missing whole groups and silent receivers
can escape that counter. Even a passing aggregate gate is not per-subscriber proof.

Reports use approximate nearest-time sample sums, with the **whole sample set**
bounded to a five-second timestamp span. They are not simultaneous NIC readings;
latency is the worst host's p90, not a pooled p90. Original reports can be preserved
when re-analyzing with `fixed-load-report.py --output-prefix reviewed`.

These diagnostics locate the CPU/throughput transition but do not identify a hot
code path. A short `perf` recording during a reproducible collapse is still needed
before attributing it to a QUIC algorithm, scheduling, retransmission or application
queueing. Do not change several of those variables at once.
