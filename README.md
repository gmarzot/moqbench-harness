# moqbench-harness

Benchmark harness for MoQ relays — moqx/mvfst, cf-moq-rs/quinn, moq-dev
(current `moq-quic`, plus historical quinn and quiche) — driven by three independent load generators (moxygen's
moqperf, aiomoqt on picoquic, Quicr's moq-bench on libquicr), plus the
picoquic and iperf3 controls used to keep the numbers honest.

A generic deployment and benchmark helper for public MoQ implementations.
Cloud backends are optional; deployment identities and private relay adapters
belong in ignored local files.

## Running a series

`bin/moqbench` runs a benchmark from three kinds of file:

| file | what it says |
|---|---|
| `deployments/<name>.toml` | The nodes: instance type or address, and each node's roles. Your account settings go in `deployments/<provider>.local.toml` (see the `.example` files), which git ignores. |
| `suites/<name>.toml` | The tests: test type, load tool, video shape, limits. No hardware. |
| `plans/<name>.toml` | A deployment, its suites, the relays, builds and tuning, the budget, and whether to tear down. |

```bash
./bin/moqbench run moqx-c7gn12x --dry-run   # hosts, commands, each setting's source, time and cost
./bin/moqbench run moqx-c7gn12x             # up, provision, tune, baseline, suites, report, down
./bin/moqbench run moqx-c7gn12x --only ceiling-2160 --no-setup
./bin/moqbench test capacity --tool moqperf --video 1080p --max-per-box 7000 --seconds 420
./bin/moqbench status
./bin/moqbench dashboard                    # watch a run; see RUNBOOK.md
```

The test types are `capacity` (ramp until the latency SLO is crossed) and `ceiling`
(ramp past it to the egress plateau, moqperf only). The commands below it remain
available and are what `moqbench` calls.

## Start here

| file | what it is |
|---|---|
| **`BENCHMARKING-GUIDE.md`** | **Read first.** How to benchmark a MoQ relay and judge it production-ready: what to measure, test design, latency measurement in MoQT, fault injection, overload and migration, host tuning, and the kernel acceleration appendix. |
| `oci/README.md` | The OCI testbed: environments, credentials, adding your own tenancy, `./bin/testbed.sh` lifecycle, tenancy constraints, shapes. |
| `aws/main.tf` | The AWS testbed, driven by the same `./bin/testbed.sh` (see [AWS testbed](#aws-testbed)). |
| `MOQDEV.md` | Current moq-dev build, scaling configuration and deployment. |
| `RUNBOOK.md` | Commands, including `relay-load.sh` for every load generator. |
| `OCI-READINESS.md` | Generic OCI deployment prerequisites, path characterization and runtime checks. |
| `STATE-AND-PLAN.md` | Generic harness status and next steps; deployment-specific journals remain local. |
| `aws/AIOMOQT-FINDINGS.md` | Written for the aiomoqt maintainer: five issues, plus a correction withdrawing an earlier analysis that turned out to be link-limited. |
| `aws/README.md` | The legacy AWS/Linode harness scripts — setup, running, and the gotchas that cost time. |
| `PARITY.md` | What was equalised across relays. Read before quoting any cross-relay number. |
| `aws/data/` | Retained CSVs from hosts that may be rebuilt. |

## The one rule

Characterise the path before attributing anything to the stack:

```bash
iperf3 -s -p 4533                                          # receiver
iperf3 -c <peer> -p 4533 -u -b 0 -l 1400 -t 15             # both directions
iperf3 -c <peer> -p 4533 -u -b 0 -l 1400 -t 15 -P 4        # scales with flows?
```

A full day went into explaining an 880 Mbps "QUIC ceiling" that was a 1 Gbps
link nobody had measured. Read the **receiver** line, use **1400 B** datagrams,
and record the ceiling beside every number.

---

## Layout

| path | what |
|---|---|
| `bin/` | Every command. Run them as `./bin/<name>` from anywhere in the repo, or put `bin/` on your `PATH`; each finds the repo root itself. |
| `bin/testbed.sh` | Create, resize, stop, start and destroy an environment's hosts, in whichever cloud the environment is defined for (`oci/testbed.sh` or `aws/testbed.sh`). |
| `bin/testbed-provision.sh` | Install the load generators on every host and the relays under test (moqx by default). |
| `bin/relay-deploy.sh` | Install one relay build on the relay host: moqx from its GitHub release, or any relay's binary built elsewhere. Checks it runs there, keeps the previous build, and records which one is installed for `versions.txt`. |
| `bin/testbed-tune.sh` | Host network tuning: MTU 1500, per-queue `fq_codel`, receive spreading. Required after every boot. |
| `bin/testbed-ssh.sh` | Log in to a host by role: `relay`, `client-01`, `probe-01`. |
| `bin/relay-smoke.sh` | Does each relay serve subscribers? Uses moxygen's moqperf tools, which come only from a prebuilt tree (`SRC=` in provisioning). |
| `bin/bw-baseline.sh` | iperf3 TCP and UDP between every node and the relay, both ways, then all nodes at once: the path ceiling every result is read against. |
| `bin/relay-capacity.sh` | Health-gated capacity search (moqperf). |
| `bin/relay-load.sh` | One run with any load generator: `moqperf`, `moqbench`, `adaptive`, `fixed`, `loadsim`. |
| `lib/` | Helpers the commands use: `hosts.sh` (which hosts to drive), `run.sh` (one measured point) and `summarize.py`. |
| `oci/` | The OCI testbed: Terraform module, `testbed.sh`, and `envs/` (shared env definitions, plus gitignored per-tester `*.local.tfvars` from the `.example` templates). |
| `remote/` | Scripts that run on a host, fed over ssh by `bin/testbed-provision.sh`. |
| `moqbench/` | moq-bench track profiles. |
| `relays.local.sh.example` | How to add a relay that is kept out of this repo (gitignored `relays.local.sh`). |
| `.env.example` | Personal defaults every command reads from a gitignored `.env`, such as `TESTBED_ENV` (the environment for new deployments). Shell variables override it. |
| `aws/` | The AWS testbed: `main.tf`, `testbed.sh` and `envs/`, like `oci/`. The other `aws/*.sh` and `bin/run-linode.sh` are the older c7gn and Linode setups, driven via `RELAY_HOST`/`CLIENT_HOSTS`. |

The commands take their hosts from `.testbed/inventory.json`, a cloud-neutral
host list that `./bin/testbed.sh <deployment> up|start|output|use` writes for
the selected deployment, so none of them depends on the cloud. For machines no
testbed created, name them instead with `RELAY_HOST`, `RPRIV_HOST`,
`CLIENT_HOSTS` and `PROBE_HOSTS`. Paths given to the commands (`PROFILE`,
`OUTDIR`) are relative to the repo root.

## AWS testbed

`aws/` mirrors `oci/`: same `./bin/testbed.sh` verbs, same outputs, same
`.testbed/inventory.json`. The default is Graviton (`arch = "arm64"`, Ubuntu
24.04 from SSM) in one cluster placement group; `./bin/relay-deploy.sh moqx`
picks the moqx tarball for the relay host's architecture.

```bash
cp aws/envs/my-account.local.tfvars.example aws/envs/moqbench-aws.local.tfvars  # profile, account id, key, CIDR
./bin/testbed.sh aws-1 plan --env moqbench-aws
./bin/testbed.sh aws-1 up -y && ./bin/testbed-provision.sh && ./bin/testbed-tune.sh
./bin/testbed.sh aws-1 stop        # or destroy
```

## Load generators

Independent implementations, so no relay result rests on one client stack:

| tool | stack | shape | role split |
|---|---|---|---|
| moqperf (`moqperf_test_client` + `moqtest_server`) | moxygen / mvfst, C++ | N subscribers per process, fixed object profile, Prometheus latency histogram | publisher on relay, subscribers on clients |
| aiomoqt `adaptive_bench` | Python on picoquic | feedback ramp of subscribers (or bitrate) until latency or loss backs it off | `--role pub` / `--role sub` |
| aiomoqt `load_sim` | Python on picoquic | scenario populations: publishers by tracks, joining and churning audiences | `--role pub` / `--role sub` |
| moq-bench (`moqbench_pub` / `moqbench_sub`) | libquicr on picoquic, C++ | one process per subscriber, ini-defined tracks, per-subscriber completion report | publisher anywhere, subscribers by process |

Every relay runs MoQT draft 16. Upstream moq-bench has moved to draft 18, so
`testbed-provision.sh` builds the last draft-16 moq-bench against libquicr's `draft-16`
tag (`MOQBENCH_DRAFT=18` builds the current one, for moqx only).

The publisher runs on the relay host for effective-capacity numbers, which
keeps ingest off the wire. The optional probe node carries every tool, so
it can publish across the network and probe a relay that the clients are
loading (`PUB_HOST=probe`, `SUB_HOSTS=probe`).

To measure a relay that is already managed by a service, set
`EXISTING_RELAY=1 RELAY_PORT=4433` with `bin/relay-load.sh`. In this mode the
harness verifies the listener and records its executable hash and thread
CPU affinity, then starts and cleans up only load generators. `THREADS` and
`RELAY_CPUS` do not reconfigure the existing relay. Set `RELAY_UNIT=moqx-4433`
to collect its systemd journal for the run. The default mode still starts its
own relay on port 4533. Existing mode supports `adaptive`, `fixed`, `loadsim`,
and `moqbench`.

For example, ramp two client hosts toward 1,500 subscribers each at 2 Mbps
against an existing moqx service:

```bash
EXISTING_RELAY=1 RELAY_PORT=4433 RELAY_UNIT=moqx-4433 \
PUB_HOST=relay SUB_HOSTS=clients SUBS=3000 \
SUB_MBPS=2 OBJ_SIZE=8333 GROUP=60 STEP_SUBS=50 JOIN=5 STAGGER=0.02 \
AIOMOQT_EXTRA="-P 1 --cc-algo bbr --latency-threshold 500" \
./bin/relay-load.sh adaptive moqx 600
```

`SUBS` is the total ceiling across clients. Adaptive mode can back off before
reaching it and ends on reaching the ceiling; the duration is a maximum,
not a guaranteed hold at that population.

## Repository content boundary

Tracked files and PR descriptions hold generic harness behavior, public cloud
interfaces and public upstream relay integrations only. Keep account and tenancy
identifiers, host addresses, credentials and private results in the ignored
`.env`, `*.local.tfvars`, `relays.local.sh`, `.testbed/` and `results/`.
`.gitignore` does not sanitize tracked content or history: review the commit
range before pushing.

## Contributing

See `CONTRIBUTING.md`.

## License

MIT; see `LICENSE`. Patches in `patches/` apply to third-party projects and
stay under those projects' licenses.
