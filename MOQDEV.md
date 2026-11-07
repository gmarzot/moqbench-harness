# moq-dev performance candidate

`moqdev` is the current upstream relay, with its `moq-quic` transport. The
`quinn` and `quiche` harness keys remain historical builds. Do not apply
`patches/moq-dev.patch` to this candidate: the APIs, transport implementation
and configuration have changed since that patch was made.

Source snapshot: `8e281b8f02478f37e7a20404949aa021a1c742cd`, fetched 2026-10-26.
The package still reports `moq-relay 0.17.2`, but this main-branch snapshot is
2,135 commits beyond that tag. Identify results by commit and binary SHA,
not just the package version. No upstream source patches are applied.

## Build and deploy

Requires Docker, Git and Python 3 locally. Builds do not contact any testbed.

```bash
./bin/moqdev-build.sh
# Repeat this exact source revision:
MOQDEV_REF=8e281b8f02478f37e7a20404949aa021a1c742cd ./bin/moqdev-build.sh
```

The default fetches `origin/main` into `../moq-dev`, then builds a Git archive
of that revision. Local uncommitted changes are not included. `MOQDEV_SRC`
chooses another checkout; `FETCH=0` uses its existing refs.

Outputs under ignored `patches/bin/moqdev/`:

- `moq-relay-dev`: executable, renamed so process accounting is unambiguous.
- `build.json`: full source revision, lockfile hash, binary SHA-256, compiler,
  CPU target, features, profile and builder image ID.
- `help.txt`: CLI help from the executable run in the Ubuntu 22.04 builder.

Build: Ubuntu 22.04/glibc 2.35, repository-pinned Rust 1.98.1, `--locked`,
`--release --no-default-features --features quic,jemalloc`. Upstream release
settings provide opt-level 3, fat LTO and one codegen unit. Debug line tables,
frame pointers and symbols are retained for perf/crash diagnosis. Jemalloc is
compiled in; do not add LD_PRELOAD. QUIC/WebTransport and the internal admin
endpoint are included; optional ACME, mDNS, WebSocket, Iroh, qlog and io_uring
features are omitted.

The default Rust and C target is `x86-64-v3` (AVX2-class), compatible with the
EPYC test machines and suitable for local validation. This deliberately avoids
`target-cpu=native` on a different laptop CPU. `MOQDEV_CPU` can select another
compiler target, but the same spelling must be accepted by Rust and GCC and
must run on the deployment host. `BUILD_JOBS` defaults to 4.

After the selected testbed has been started and tuned:

```bash
./bin/relay-deploy.sh moqdev
# Or include it in provisioning without reinstalling load generators:
TOOLS=none RELAYS=moqdev ./bin/testbed-provision.sh
```

Deployment verifies the local binary against its manifest, preserves previous
files, and installs under `~/Projects/moq-dev/`: binary, `perf.toml`, `run.sh`,
`build.json` and the normal `.rev` fingerprint. It does not start the relay.
Existing `~/Projects/bench/cert.pem` and `key.pem` are required; provisioning
creates test certificates if absent. These remain untracked local material.

## Scaling configuration

`configs/moqdev-perf.toml` is the checked-in baseline:

| Setting | Baseline and reason |
|---|---|
| Protocol | Native QUIC / WebTransport, MoQT draft 16, harness URL path `/anon` |
| Workers | 16 independent single-thread QUIC workers, each with a UDP socket |
| Steering | Upstream SO_REUSEPORT with connection-ID-aware socket selection |
| Affinity | Unpinned; explicit `MOQDEV_PIN=true` enables upstream worker pinning |
| Auxiliary Tokio workers | 2 for shared runtime tasks; independent of QUIC worker count |
| Congestion control | `delay`, mapped to upstream BBRv3; `loss` is CUBIC |
| GSO / PMTUD | GSO enabled, path MTU discovery disabled |
| Streams | 8,192 concurrent streams per connection, not subscriber capacity |
| QUIC windows | 128 MiB connection receive, 4 MiB stream receive, 16 MiB send |
| UDP buffers | Upstream compiled-in request of 8 MiB in each direction per socket |
| Cache | 1 GiB target, 2-second non-latest-group retention; latest group always retained |
| Admin | `127.0.0.1:4503`, including `/health`, `/metrics` and `/sessions` |
| Authentication | Anonymous `**`, for the private test network only |
| Shutdown | 5-second graceful drain |

Keep the established host tuning, including 64 MiB `rmem_max`/`wmem_max`,
queue tuning and open-file limits. The older `MOQ_UDP_BUFFER_BYTES` patch knob
does not apply to pristine upstream: setting it does not turn these requests
into 64 MiB. Linux reports doubled SO_RCVBUF/SO_SNDBUF accounting values.

The kernel must support the reuseport steering mechanism; startup errors must
not be silently replaced with a single-socket run. `relay-load.sh` checks the
actual `moq-quic-N` thread count before launching clients and saves the config,
command line and manifest with the results.

`runtime.pin` pins to allowed logical CPUs, not automatically one sibling per
physical core. For an eight-physical-core experiment use the host's verified
one-sibling-per-core affinity list plus `THREADS=8 MOQDEV_PIN=true`. Start with
16 workers/unpinned, then compare 8 under otherwise identical conditions.
`THREADS` changes QUIC workers for this relay, not merely Tokio pool size.

io_uring is not built: upstream requires Linux 6.12+, while the testbed is 6.8.
BBRv3 has an upstream conditional ProbeRTT skip for application-limited,
uncongested intervals. It is **not globally disabled**, and there is no relay
configuration switch to disable it. This build preserves upstream behavior.

## First run after restart

Start with a small interoperability run, then a fixed population:

```bash
THREADS=16 SUBS=60 STEP_SUBS=20 JOIN=2 SUB_MBPS=2 \
  OBJ_SIZE=8333 GROUP=60 AIOMOQT_EXTRA="-P 1 --cc-algo bbr" \
  ./bin/relay-load.sh adaptive moqdev 45

THREADS=16 PUB_HOST=relay SUB_HOSTS=clients \
  SUBS=1200 SUB_MBPS=2 OBJ_SIZE=8333 GROUP=60 \
  STEP_SUBS=50 JOIN=5 STAGGER=0.02 \
  AIOMOQT_EXTRA="-P 1 --cc-algo bbr --latency-threshold 500" \
  ./bin/relay-load.sh fixed moqdev 600
```

For `moqdev`, adaptive/fixed runs select `--pub-ns` and
`--sub-filter latest-object` automatically and record both in `run.txt`.
The local smoke test found aiomoqt's default PUB-BOTH stalls: the relay attempts
a SUBSCRIBE that aiomoqt rejects because its PUBLISH already holds the track.
Upstream also logs that draft-16 next-group (decoded as `Relative(0)`) is
unsupported and ignores it. Namespace publishing plus live-edge subscribing
is the tested interoperable path; the generic next-group default remains for
other relay keys. This is a join-behavior difference to state in comparisons,
not a claim that all draft-16 operations interoperate. Do not add `--pub-both`
to `AIOMOQT_EXTRA` for this baseline. The legacy source patch is not applied.

These examples assume three clients. The first is a cap-limited smoke test,
not a soak: adaptive exits when each client hits its cap. `fixed` includes
population fill within the duration. Generic harness runs own relay processes
and stop other harness relays; use existing-service mode for concurrent manual
services instead. Avoid simultaneous capacity runs on a shared relay VM.

Manual foreground start on the relay, with a different port if needed:

```bash
cd ~/Projects
MOQDEV_PORT=4435 MOQDEV_ADMIN_PORT=4503 moq-dev/run.sh 16
```

Then use `EXISTING_RELAY=1 RELAY_PORT=4435` with the load command. Private VCN
rules must permit the chosen port; this change adds no public ingress rules.

Judge results using delivered aggregate rate, per-client delivery, latency,
resets and relay survival. The current aiomoqt active count does not require
recent per-slot data, and zero reported object loss does not detect a silent
subscription. The fixed report's latency summary alone is not a capacity pass.

## Source references

These are pinned to the built source, because the public docs can lag main:

- [Relay configuration](https://github.com/moq-dev/moq/blob/8e281b8f02478f37e7a20404949aa021a1c742cd/doc/bin/relay/config.md)
- [Runtime controls](https://github.com/moq-dev/moq/blob/8e281b8f02478f37e7a20404949aa021a1c742cd/rs/moq-relay/src/runtime.rs)
- [UDP buffer handling](https://github.com/moq-dev/moq/blob/8e281b8f02478f37e7a20404949aa021a1c742cd/rs/moq-sock/src/bind.rs)
- [Congestion-controller mapping](https://github.com/moq-dev/moq/blob/8e281b8f02478f37e7a20404949aa021a1c742cd/rs/moq-tokio/src/quic/backend.rs)
- [BBRv3 including ProbeRTT behavior](https://github.com/moq-dev/moq/blob/8e281b8f02478f37e7a20404949aa021a1c742cd/rs/moq-congestion/src/bbr3/mod.rs)

## Validation on 2026-10-26

Built binary SHA-256:
`7ed23edaf9653cc13a0963aa6ce652b17f41105683ac66aff0b4bc125a73e57f`.
It runs in a minimal Ubuntu 22.04 image and requires at most `GLIBC_2.34`.
ELF symbols confirm jemalloc is linked. Shell syntax and whitespace checks pass.
Local deployment emulation verified first install, repeated install, config
backups, executable permissions and binary/manifest consistency without SSH to
the testbed.

A local aiomoqt 0.14.0.dev677 + aiopquic 0.7.0.dev199 test used the installed
launcher and performance config, with 16 verified QUIC worker threads and
20 subscriptions at 2 Mbps each. After fill, the reported samples met the
36 Mbps delivery floor with zero reported loss, known latency and p90 below
500 ms. The subscriber ran to its 25-second cap,
the publisher finished, and the relay survived both then exited cleanly on
SIGTERM. This is an interoperability/teardown smoke test, not a capacity claim.
The laptop's socket-buffer sysctls clamped the 8 MiB request to 4 MiB; no host
sysctls were changed for this smoke test.

Raw logs, build manifest, CSV and the validation scripts are saved locally in
`results/moqdev-local-validation-20261026/` (ignored). The failed PUB-BOTH test
is retained there too. That validation did not use a cloud testbed.
