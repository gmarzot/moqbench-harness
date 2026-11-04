# aiomoqt / aiopquic — findings from a high-rate relay benchmark

Written for the aiomoqt maintainers. Everything
below was measured on 2026-08-26; nothing here is speculative unless labelled
"inferred".

## What was run

`aiomoqt.tools.adaptive_bench` in split-host subs mode against a moxygen-based
relay (moqx `v0.3.1-11-gbffca928`), pushing fan-out much harder than the tool
appears to have been exercised before.

| | |
|---|---|
| aiomoqt | dev branch `gmarzot-0.11.0`, commit `8b10ea7` |
| aiopquic | dev branch `gmarzot-0.4.0`, commit `477b5f2` (AWS runs, 2026-08-26) |
| Python | CPython 3.14.7 (uv-managed), aarch64, built from source |
| hosts | 2 × AWS c7gn.12xlarge — 48 vCPU aarch64, 92 GB, 150 Gbps NIC |
| path | same VPC subnet, sub-millisecond RTT |
| track | 20,800 B objects, 30 objects/s, 5 Mbps per subscriber |
| filter | `latest-object`, single subgroup stream, 1 publisher / N subscribers |

Publisher on the relay host, subscriber controller on the second host:

```
# relay host
python -u -m aiomoqt.tools.adaptive_bench moqt://<relay>:4433/moq-relay -k \
  --role pub --mode subs --sub-mbps 5 -s 20800 -N <ns> -T v5 -t 420

# client host
python -u -m aiomoqt.tools.adaptive_bench moqt://<relay>:4433/moq-relay -k \
  --role sub --mode subs -N <ns> -T v5 --sub-mbps 5 -s 20800 \
  --start-subs 200 --step-subs 200 --max-subs 15000 \
  -l 512 --interval 5 -j 1 -S 0.005 --mp -t 300 --report out.csv
```

## The good news

It scaled cleanly and linearly, with zero loss, to a genuinely high load:

| subscribers (actual) | aggregate | mean | p90 | loss |
|---|---|---|---|---|
| 1,799 | 8.7 Gbps | 6.5 ms | 8.0 ms | 0% |
| 2,800 | 14 Gbps | 9.2 ms | 12 ms | 0% |
| 3,800 | 19 Gbps | 12 ms | 15 ms | 0% |
| 4,800 | 24 Gbps | 16 ms | 19 ms | 0% |
| 5,800 | 29 Gbps | 21 ms | 24 ms | 0% |
| 6,794 | 35 Gbps | 26 ms | 31 ms | 0% |

Latency percentiles tracked an independent C++ load generator on the same
path, which is the cross-check that matters — two unrelated stacks agreeing.

Ceiling reached: **~6,950 subscribers / 37 Gbps from a single 48-core box.**

## Issue 1 — roughly one OS thread per subscription

**Measured.** With `--step-subs 200` (so 200 subscriptions per worker
process), each worker process showed **202 threads**. About 30 workers were
live, giving roughly 6,000 threads on a 48-core box. Load average reached
**978** with all cores at 98%.

The 200-subscriptions → 202-threads correspondence is what makes this look
structural rather than incidental.

**Inferred, needs maintainer confirmation:** the threads are not created in
Python. There is no `threading.Thread`, `ThreadPoolExecutor`, `to_thread` or
`run_in_executor` anywhere on this path — `aiomoqt/utils/workers.py` is
properly asyncio throughout (`_slot_supervisor` per subscription as tasks,
batched into one process per group). So the threads appear to originate in the
aiopquic native binding. The presence of `_binding/spsc_ring.pxd` and
`_binding/_transport.pyx` suggests a per-transport single-producer/
single-consumer ring with its own native I/O thread, which would produce
exactly one thread per QUIC connection.

If that is right, the fix is to multiplex many connections onto a bounded
pool of native I/O threads rather than one per transport. That single change
probably moves the ceiling more than anything else on this list.

## Issue 2 — receive queues grow without bound

**Measured.** Resident memory across the worker processes went from **22 GB to
89 GB of 92 GB inside a single run**, with only 3.42 GB available at the peak.
The box did not OOM-kill, but it was close, and the GC pressure is a plausible
contributor to the CPU saturation in Issue 1.

`adaptive_bench` already exposes `--max-queued-bytes` and
`--max-inflight-bytes`. Neither was set in these runs, and neither appears to
have a default. Two suggestions:

- Give both a sane non-`None` default, scaled to the subscription count.
- When a subscriber falls behind, drop or skip rather than accumulate. For a
  benchmark tool, unbounded buffering converts a throughput measurement into a
  memory-exhaustion test, and the interesting signal (latency, loss) is lost.

This is the cheapest thing to try next and may substantially move the ceiling
on its own.

## Issue 3 — collapse instead of graceful degradation

**Measured.** Past roughly 7,000 subscriptions the run did not degrade — it
fell off a cliff:

```
 36s   35Gbps   7000 target │ 35Gbps  6794 actual │ p90 31ms  │ ramp
 43s   36Gbps   7200 target │ 37Gbps  6947 actual │ p90 66ms  │ ramp
 57s   37Gbps   7400 target │ 1.7Gbps  400 actual │ p90 2.8s  │ collapse
 74s   37Gbps   7400 target │ 2.9Gbps  598 actual │ p90 23s   │ collapse
```

219 connections terminated with **error 1054**. That decodes as
`PICOQUIC_ERROR_STATELESS_RESET` — picoquic's `PICOQUIC_ERROR_CLASS` is
`0x400`, and `0x400 + 30 = 1054`. It is a picoquic class code, not a MoQT
session code, which is not obvious from the log line
`QUIC error: code: 1054 reason: ConnectionTerminated`.

**Inferred mechanism:** CPU-starved workers stop servicing their event loops
long enough to miss the relay's idle timeout. The relay drops those
connections. When a worker finally sends again, the relay has no state for
that connection ID and answers with a stateless reset. The self-healing
subscribers then all reconnect at once, and the cycle repeats — the run never
recovered to its previous high-water mark.

Suggestions:

- Surface 1054 with its symbolic name. `ConnectionTerminated` hides the cause.
- Consider whether the self-healing reconnect should back off. A synchronised
  reconnect storm after a mass drop makes recovery harder, not easier.
- An explicit "worker event loop starved for N ms" warning would have made
  this diagnosable in seconds instead of an hour.

## Issue 4 — a stale publish reads as a subscribe failure

**Measured, and this cost the most time of anything here.**

If the publisher is restarted while the relay still holds the previous
registration of the same namespace/track, the relay refuses the new PUBLISH.
The publisher sits at `forward=0` forever and never sends. On the subscriber
side the only symptom is:

```
  8s  500Mbps  100 │ 500Mbps  0.0bps  0 │ ... │ rx=0 (no data)
 14s  500Mbps  100 │ 500Mbps  0.0bps  0 │ ... │ ABORT: rx=0
  Reason: no data received — subscribe failed or publisher stalled
```

Everything looks healthy: the session establishes, `PublishNamespace` gets
`RequestOk`, `Publish` gets `PublishOk`. The only tell is `forward=0` in the
`PublishOk`, which is easy to read as normal because it *is* normal before any
subscriber arrives.

Suggestions:

- In `--role pub`, if `forward` is still 0 after a subscriber is known to have
  joined, warn explicitly — something like "publisher never received
  forward=1; the relay may still hold a prior registration of this track".
- Consider defaulting the namespace to something unique per process run, or
  documenting prominently that a fresh namespace is required per run.

The workaround is to mint a fresh namespace per run, which is what our harness
now does.

## Issue 5 — usability items

- **Reaching `--max-subs` ends the run.** There is no way to ramp to a level
  and hold it there, which is what a steady-state confirm needs. The workaround
  is to set `--max-subs` just above `--start-subs` and `-j` longer than the
  run duration, which is obscure. A `--hold` flag would be clearer.
- **stdout is block-buffered when redirected.** Launching without `-u` makes
  the tool look hung for minutes — the banner does not even appear. Consider
  line-buffering or flushing the periodic report line.
- **`pkill -f aiomoqt.tools.adaptive_bench` matches the invoking shell.** Not
  the tool's fault, but worth a line in the docs, since the obvious way to stop
  a run kills the terminal that launched it.

## Suggested order of work

1. Default `--max-queued-bytes` / `--max-inflight-bytes` (Issue 2) — cheapest,
   and may move the ceiling on its own.
2. Warn on `forward=0` (Issue 4) — small change, large diagnostic payoff.
3. Symbolic decode of picoquic error codes (Issue 3).
4. Investigate per-transport native threads (Issue 1) — the structural item,
   and the one that decides whether this tool can drive a C++ relay to its
   actual limit.

## Context

None of this implicates the relay. Through every collapse above, the relay
stayed at 15 of its 48 cores with zero receive-buffer errors, remained healthy
for the full 1,880-second session, and returned to a clean idle state
afterwards. The measurements were client-bound throughout.

That is exactly why this is worth fixing: as an independent, non-C++ cross-check
on relay behaviour, aiomoqt is valuable, and it currently runs out of headroom
before the relay under test does.

---

# Addendum — single-connection throughput ceiling (2026-08-28)

Measured on two Linode perf boxes (8-core EPYC 7713, 15 GB) in the same
datacenter, 0.379 ms native RTT. Both boxes already carried tuned sysctls
(rmem/wmem 32 MB, netdev_max_backlog 65536, fq_codel 65536 flows); NIC rings
are at their virtio maximum of 256/256.

## Finding A — `--forward 1` is accepted by moqx, contrary to the flag's help

The `--forward` help says "NOT supported by the spec: relays reject the
unsolicited uni streams." Against moqx that is not what happens. Publisher
running with no subscriber at all, before/after relay counters:

| relay counter | before | after | delta |
|---|---|---|---|
| `quicBytesRead_total` | 2,702,652,750 | 4,301,031,029 | +1.598 GB |
| `subPublishSuccess_total` | 2 | 3 | +1 |
| `subSubscriptionStreamOpened_total` | 80 | 127 | +47 |
| `quicStreamsReset_total` | 15 | 15 | **0** |

The publisher sent 47,641 x 32,768 B = 1.561 GB; the relay read 1.598 GB with
zero resets. So the data is fully ingested with no subscriber present.

Useful for benchmarking, because it decouples publisher measurement from
having to stand up subscribers. Worth confirming whether it is intended
relay behaviour — a publisher can push bytes into a relay that nobody is
consuming.

## Finding B — a single connection tops out near 880 Mbps, and it is not cwnd

Every axis was varied independently. The ceiling did not move:

| varied | range | result |
|---|---|---|
| parallel subgroup streams `-P` | 1 - 16 | 844 - 872 Mbps, flat |
| object size `-s` | 8 KB - 256 KB | 851 - 892 Mbps, flat |
| emulated RTT (netem) | 0 - 50 ms | 880 - 937 Mbps, flat |
| relay in path | yes / no | 857 vs 868 Mbps |
| `--cc-algo` | bbr / bbr1 | 1001 / 939 Mbps |

Publisher CPU sat at 75-89% of **one** core throughout.

**It is not congestion-window limited.** A cwnd-bound flow degrades as RTT
rises, since throughput ~ cwnd/RTT. Throughput was flat from 0 to 50 ms while
BDP grew to 5.57 MB. bbr beats bbr1 by ~7%, so CC is not irrelevant, but it is
not the clamp.

**It is not per-object cost either.** Bitrate stayed flat while object size
moved 32x, so obj/s fell inversely; and loopback sustains 60,904 obj/s at
4 KB, far above anything the wire runs needed.

## Finding C — the clamp is per-packet cost

Packet rate is the invariant. From the bbr run, 1001.38 Mbps over 88,786 pps
is 1,410 B/packet, i.e. already at path MTU. Across all wire configurations
pps stayed in a narrow 78,000-89,000 band and bitrate simply tracked
pps x MTU.

Loopback control (same stack, no NIC, `lo` MTU 65536 vs `eth0` 1500):

| objsize | loopback Mbps | loopback obj/s | wire Mbps |
|---|---|---|---|
| 4096 | 2002 | 60,904 | - |
| 8192 | 1970 | 30,007 | 851 |
| 16384 | 1823 | 13,895 | 879 |
| 32768 | 1956 | 7,459 | 857 |
| 65536 | 1960 | 3,738 | 892 |
| 131072 | 1925 | 1,836 | 852 |

Loopback is only ~2.2x the wire, not the ~40x a pure packet-count limit would
predict against a 43x larger MTU. So the cost is CPU per packet through the
aiopquic/picoquic path, roughly halved when the NIC and driver drop out.

**The lever is GSO / send batching** — fewer syscalls per packet — not cwnd,
socket buffers, NIC rings, or object size. `datagram_max_payload = 1200`
applies only to DATAGRAM frames and is not in play for subgroup streams.

## Finding D — the p99 tail is structural, not network

On a 0.379 ms link the wire runs showed p50 8 ms but p99 1,476 ms and max
2,436 ms. On loopback the tail collapses from 964 ms at 32 KB objects to
6.3 ms at 64 KB. A tail that large on a sub-millisecond path, responding to
object size rather than to the network, points at object/group handling
inside the tool rather than at queueing on the path.

## Reproducing

Harness in `aws/` (works against any host pair):
`start-pubserver.sh`, `run-sub.sh`, `bdp-sweep.sh`, `obj-sweep.sh`,
`pub-sweep.sh`, `conc-pub.sh`, `loopback-sweep.sh`.

---

# Addendum 2 — kernel baseline, OpenSSL, and two build gotchas (2026-08-28)

## The kernel floor: aiopquic is at 74% of raw UDP, not 10%

iperf3 3.9 on loopback, on the load box used for the aiomoqt loopback runs:

| test | bitrate | packets/s |
|---|---|---|
| UDP `-l 1400` (QUIC-sized) | 2.65 Gbps sender | **236,800** |
| UDP `-l 65000` | 45.3 Gbps | 87,133 |
| TCP (GSO-assisted) | 33.0 Gbps | - |

aiomoqt loopback at the same packet size does ~1.97 Gbps, i.e. **74% of what
raw kernel UDP achieves with the same size datagrams**. iperf3's own receiver
dropped 19% at 236k pps — even it could not drain that packet rate.

The `-l 65000` row is the mechanism in one line: 87k pps of 65 KB is 45.3 Gbps
while 236k pps of 1400 B is 2.65 Gbps. Same machine, 17x the throughput from
fewer, larger packets. TCP reaches 33 Gbps because GSO does that in the kernel.

So the ~880 Mbps wire ceiling is **the per-packet path, and it is mostly not
aiopquic's** — there is at most ~35% to win from everything above the kernel
combined. GSO / sendmmsg batching remains the only lever with real headroom.

## OpenSSL 3.5.5 vs 3.0.2: +6.8%, reproducible

QUIC does an AEAD seal plus header protection per packet, so libcrypto's
per-operation cost is per-packet cost, and 3.0.x carries provider/fetch
overhead that 3.5 recovers. Built 3.5.5 from source, rebuilt the native stack
against it, same binary A/B on loopback (`-Q --draft 16 -s 32768 -P 4`):

| rep | 3.5.5 | 3.0.2 |
|---|---|---|
| 1 | 2092.36 | 1972.64 |
| 2 | 2120.42 | 1956.42 |
| 3 | 2068.71 | 1951.00 |
| mean | **2093.8** | 1960.0 |

**+6.8%**, with no overlap between the groups. Worth taking — it is free — but
it does not move the order of magnitude, consistent with the kernel-floor
result above.

Ubuntu 22.04 ships 3.0.2, so any benchmark on a stock 22.04 box is paying
this. Recommend documenting a supported "build against a current OpenSSL"
path, since `OPENSSL_ROOT_DIR` already exists for it.

## Gotcha 1 — `_build_info` silently degrades to "unknown"

`aiomoqt.versions` reported `picotls: unknown` and `picoquic: ... (unknown)`.
Not a missing submodule. The source tree had been rsynced with its original
ownership while the build ran as a different user, so every git call failed:

```
fatal: detected dubious ownership in repository at '.../third_party/picotls'
```

setup.py caught the failure and wrote `unknown` into `_build_info.py`.
`PICOQUIC_VERSION` survived only because it is parsed from a header rather
than from git. One `git config --global --add safe.directory '*'` fixes it, and
the revisions then resolve properly (`picotls master (bfa67875)`,
`picoquic 1.1.51.1 (0dc8ba8b)`).

Suggestion: have setup.py warn on stderr when a submodule revision cannot be
read, rather than writing `unknown` silently. Provenance that fails quietly is
worse than no provenance, because benchmark results get published with it.

## Gotcha 2 — a custom OpenSSL is not picked up at runtime

Two separate problems, both worth a line in the docs:

1. **The native build dir caches the resolved OpenSSL.** `./build.sh --force`
   with `OPENSSL_ROOT_DIR` set still linked the system 3.0.2 — CMake printed
   `root: /usr/local/openssl-3.5.5` and then `OpenSSL_VERSION: 3.0.2`, because
   `third_party/picoquic/build` was left from an earlier configure. The dir has
   to be removed; `--force` is not enough.
2. **SONAME collision at runtime.** Both libraries are `libcrypto.so.3`, and
   Python's own `ssl`/`hashlib` load the system one first, so the extension
   resolves to whatever is already mapped. `readelf -d` on the built extension
   showed **no RPATH/RUNPATH** despite `OPENSSL_ROOT_DIR` being exported for
   the install, so setup.py's RPATH-baking did not trigger. Until that is
   fixed, a custom OpenSSL needs `LD_LIBRARY_PATH=<prefix>/lib64` at run time,
   and `aiomoqt.versions` is the way to confirm which one is actually loaded.

Also note `build.sh --force` exits 2 at its relink step with
`No virtual environment found` unless `VIRTUAL_ENV` is exported — uv needs it.

## Correction — revisions differ between the two testbeds

The AWS runs (2026-08-26) were built from aiopquic `477b5f2`. The Linode runs
(2026-08-28) were rsynced later and are aiopquic `35e9402` / aiomoqt
`ad161083`. Do not read the two sets as one series.

With the build fixed, `aiomoqt.versions` now reports this rather than `0.0.0`:

```
aiomoqt:   0.10.7.dev58+gad161083a
aiopquic:  0.3.12.dev21+g35e940283
  - picoquic:  1.1.51.1 (0dc8ba8b) [2026-07-30]
  - picotls:   master (bfa67875) [2026-04-20]
  - openssl:   OpenSSL 3.5.5 (/usr/local/openssl-3.5.5/lib64/libcrypto.so.3)
```

Three separate caches had to be cleared to get there, each failing silently:
`setuptools_scm` missing under `--no-build-isolation` (version 0.0.0), git
`safe.directory` on an rsynced tree (submodule revs `unknown`), setuptools'
`build/` dir (no RUNPATH baked, so the extension resolved to the system
libcrypto via SONAME collision), and `third_party/picoquic/build` (native libs
still linked against the old OpenSSL). `build-aiomoqt.sh` now handles all four.

---

# CORRECTION (2026-08-29) — the wire numbers were link-limited

Addendum 1's wire figures and the causal story built on them are **withdrawn**.
The Linode pair used for those runs has a ~1 Gbps link, which we had never
characterised.

iperf3, 1400 B datagrams, between the same two hosts:

| direction | UDP | TCP |
|---|---|---|
| relay box -> load box | 1.06 Gbps (94,256 pps) | 462 Mbps |
| load box -> relay box | 901 Mbps received, **66% loss** | 231 Mbps |
| 4 parallel UDP flows | **1.12 Gbps total** | - |

Parallel flows do not help, so it is a link/egress cap. Against it:

- aiopquic's 880 Mbps was **88% of link capacity**
- native `picoquicdemo -a perf` reached 1,023 Mbps, i.e. **~the link**
- "flat across RTT, object size, stream count and CC" was a saturated link, not
  a per-packet ceiling

Also withdrawn: the **2.4-2.6 Gbps aggregate** from 4-8 concurrent publishers.
That was sender-side self-reported bytes on a link dropping 66%, never verified
receiver-side.

## What replaces it

Loopback is the only valid stack measurement on those hosts. Same 1440 B MTU
throughout — loopback does **not** use a larger datagram, which was the obvious
alternative explanation and was checked (`max_mtu_sent: 1440` on both paths):

| stack | pps | vs raw UDP |
|---|---|---|
| iperf3 raw UDP | 236,800 | - |
| picoquic native C | 199,566 | **84%** |
| aiopquic (Python) | ~171,000 | **72%** |

So QUIC costs ~19% over raw `sendto` and the Python binding a further ~14%.
aiopquic is at **86% of native C picoquic**. The earlier claim that per-packet
cost in the Python transport was the ceiling does not hold.

Issues 1-5 in the original write-up (thread-per-subscription, unbounded receive
queues, the stateless-reset cascade, `forward=0` reading as a subscribe failure,
and the usability items) were all measured at the AWS testbed on a 150 Gbps path
and **stand unchanged**. Only the single-connection throughput analysis is
withdrawn.

## The lesson worth keeping

Characterise the path before attributing anything to the stack: iperf3 UDP at
1400 B, both directions, receiver-side, before any QUIC measurement. See
`OCI-READINESS.md`.
