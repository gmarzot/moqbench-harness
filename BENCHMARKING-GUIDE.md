# MoQ Relay Benchmarking and Production Readiness Guide

How to measure a MoQ relay, and how to decide whether it is ready to carry
production traffic.

Concepts come first, operational detail second, and the low-level mechanics
live in the appendices. If you are setting up a new testbed, **read Part I §2
before doing anything else** — most wasted benchmarking effort comes from
skipping it.

Numbers in this guide are labelled. **Measured** means we took it on a named
host pair and it is reproducible from this repo. **Guidance** means it is
ordinary engineering judgement, not something we measured. Protocol references
are dated, because the drafts move.

---

# Part I — What you are measuring

## 1. The question a relay benchmark answers

A relay's job is fan-out: accept a track once, deliver it to many subscribers,
on time. So the useful capacity number is:

> **The largest number of subscribers the relay sustains while holding an
> end-to-end latency objective, with bounded loss and no subscription churn it
> did not cause.**

Not aggregate throughput. Throughput as a headline rewards exactly the wrong
behaviour: a relay that accepts every subscriber and serves them all badly
scores higher than one that serves a smaller number correctly. Any relay can
hit a big Gbps figure if you let it buffer without limit and never look at
delivery time.

State the objective before the run, not after. A usable form:

> p95 end-to-end object latency under *L* ms, loss under *X*%, zero
> relay-initiated subscription resets, sustained for *T* minutes.

Three parts of that matter more than they look:

**p95 or p99, not mean.** Media breaks on tails. A mean of 30 ms with a p99 of
1.5 s is a bad experience, and we have measured exactly that shape.

**Over delivered objects.** If the relay drops or resets a subscriber, its
objects leave the latency sample and the percentile *improves*. Latency must be
read together with delivery and reset counts or it rewards shedding.

**Sustained.** A 60 s confirm is optimistic. We have watched a load that passed
60 s drift from 2.7 to 1.8 Gbps over the following minutes. Confirms run for
minutes; readiness runs for hours.

## 2. The measurement hierarchy

Measure in this order. Each layer is the ceiling for everything above it, and
an unmeasured lower layer will masquerade as an upper-layer result.

```
  4. relay        ← what you actually want to know
  3. stack        QUIC implementation, MoQT implementation
  2. host         cores, NIC, kernel, packet rate
  1. path         link capacity, RTT, loss
```

**The attribution rule: never attribute to the stack what you have not
excluded in the path.**

This is not theoretical. We spent a full day building a detailed causal story
about QUIC per-packet cost, GSO, congestion control and Python overhead, all
measured against a 1 Gbps link nobody had characterised. The load generator was
at 88% of link capacity the whole time. The "ceiling" being explained did not
exist. The signature that looked most diagnostic — throughput flat across RTT,
object size, stream count and congestion control — was simply a saturated link.

### 2.1 Preflight

On every new host pair, before any relay or load-generator run:

```bash
# receiver
iperf3 -s -p <port>

# sender — run BOTH directions, they are not symmetric
iperf3 -c <peer> -p <port> -u -b 0 -l 1400 -t 15       # QUIC-sized datagrams
iperf3 -c <peer> -p <port> -u -b 0 -l 1400 -t 15 -P 4  # does it scale with flows?
iperf3 -c <peer> -p <port> -t 10                       # TCP reference

# and a loopback control on each host, to separate host cost from path cost
iperf3 -c 127.0.0.1 -p <port> -u -b 0 -l 1400 -t 10
```

Rules that follow:

- **`-l 1400` is the only UDP number comparable to QUIC.** A QUIC packet must
  fit the path MTU. `iperf3 -u -l 65000` reports huge figures (we measured
  41–45 Gbps on loopback) that QUIC cannot reach by construction, because
  loopback MTU is 64 KB and a real path will fragment. TCP's figure benefits
  from TSO/GSO in the kernel. Neither is a QUIC target.
- **Read the receiver line.** UDP has no congestion control, so the sender line
  reports what it pushed, not what arrived. We once reported a 2.4–2.6 Gbps
  aggregate that was sender-side self-report across a link dropping 66%.
- **Record the ceiling next to every later number.** Anything within ~85% of it
  is "link-limited", not a stack result.
- **Check `max_mtu_sent`** before concluding that loopback is fast because its
  MTU is larger. In our runs picoquic used 1440 B on both loopback and wire, so
  that explanation did not apply.

### 2.2 What good looks like

Measured, all at 1400 B datagrams:

| path | UDP pps | UDP Gbps |
|---|---|---|
| laptop loopback, 16-core WSL2 | 305,318 | 3.42 |
| Linode box loopback, 8-core EPYC 7713 | 236,800 | 2.65 |
| Linode box ↔ box wire | 94,256 | 1.06 |

The Linode pair is capped around 1 Gbps egress on the 8-core plan, so it cannot
be used for throughput work above that at all. Advertised NIC bandwidth is no
guide to a path either: keep relay and load generators in one region for
capacity work, and use cross-region only when you want realistic RTT.

---

# Part II — Designing a test

## 3. Topologies

Each topology exercises a different part of the relay. A capacity number is
meaningless without saying which one produced it.

### 3.1 Fan-out — one publisher, N subscribers

The relay's core job and the default capacity test. One track in, N
subscriptions out, all on the same track.

Exercises: per-subscriber stream setup, the forwarder, send-side batching,
per-connection state, scheduler fairness.

Scale the subscriber count, hold the track shape fixed, and find where the
latency objective breaks. This produces the headline "max healthy subscribers".

### 3.2 Fan-in — N publishers, few subscribers

Ingest and aggregation. N distinct tracks arriving, each with its own
connection, flow control and object cadence.

Exercises: accept path, per-publisher state, namespace/track registration,
cache insertion, upstream-facing scheduling.

Fan-in is routinely under-tested and is where conferencing and contribution
workloads actually live. It also surfaces registration bugs that fan-out never
touches — see §13.3 on stale registrations.

### 3.3 Mesh and relay chains

Relay-to-relay forwarding, where a relay is both subscriber and publisher.
Exercises hop latency accumulation, loop prevention, and whether per-hop
buffering compounds. Hop count belongs in the result label.

### 3.4 Churn — join and leave rates

Steady-state subscriber counts hide the cost of *becoming* a subscriber.
Churn tests hold total subscribers roughly constant while cycling membership.

Measure:

- **Subscribe latency** — SUBSCRIBE sent to first object delivered, as a
  distribution, under load.
- **Join rate ceiling** — joins/second the relay accepts before subscribe
  latency or existing subscribers' delivery degrade.
- **Teardown cost** — whether leaving subscribers release memory and streams
  promptly, or whether state accumulates.

Churn is where thundering-herd behaviour appears, and where a reconnect storm
after a failure either resolves or compounds (§6.4).

### 3.5 Soak — long-running

Everything above, held for hours. Capacity tests find the knee; soak tests find
the things that only appear with time:

- memory growth, RSS per subscriber trending up
- file descriptor and stream leaks
- cache growth and eviction behaviour
- latency drift as fragmentation or queue depth accumulates
- clock steps that look like relay events (§8.4)

A relay that passes a 3-minute capacity test and fails a 6-hour soak is not
ready, and only the soak will tell you.

## 4. Track shapes

The track shape determines the result more than most people expect. **Always
state it next to any per-core or per-subscriber number.**

### 4.1 The parameters

| parameter | effect |
|---|---|
| object size | dominates Mbps/core; see §4.2 |
| object rate | per-object processing cost; frame cadence |
| group size | GOP length; affects join latency and cache granularity |
| subgroup streams (`P`) | parallelism within a track; head-of-line isolation |
| priority / group order | what the relay drops first under pressure |
| delivery timeout | whether stale objects are shed (§13.1) |

### 4.2 Object size dominates per-core throughput

**Measured**, moqx, identical hardware, 240 subscribers held constant, object
size swept 1 KB → 66 KB:

> **149 → 821 Mbps per core.**

Near-linear below 4 KB, where per-object cost dominates; flat above 33 KB,
where per-byte cost dominates; knee at 8–16 KB.

A 5.5× swing from payload shape alone. This is why a per-core figure without a
stated object size is not a measurement, and it reconciles otherwise
contradictory published numbers — a relay quoting ~1.7 Gbps/core on large
objects is not faster than one quoting 327 Mbps/core on small ones.

### 4.3 Realistic profiles

**Guidance.** Benchmark shapes should resemble something that ships:

| profile | object size | rate | notes |
|---|---|---|---|
| 1080p30 video | ~8 KB P-frames, ~64 KB I-frames | 30/s | GOP 150 ≈ 5 s |
| low-latency video | smaller objects, more streams | 30–60/s | shorter GOP |
| audio | ~400 B–1 KB | 50/s | dominated by per-object cost |
| synthetic fan-out | fixed 4–32 KB | 30/s | easiest to reason about |

Mixing audio and video tracks is more realistic and much harder to attribute;
run fixed synthetic shapes for capacity, realistic mixes for readiness.

### 4.4 Streams versus datagrams

Subgroup streams and object datagrams have different cost and loss behaviour.
Datagrams cap at roughly one packet of payload (~1150 B usable), so object rate
equals packet rate, which makes them a clean instrument for isolating
per-packet cost. Streams carry arbitrary object sizes but add stream state and
head-of-line blocking within a subgroup.

Run the same object size both ways where the tooling allows it — aiomoqt's
`loopback_bench -D` exists for exactly this A/B.

## 5. Transport variables

These change results enough that they belong in the result label, not a
footnote.

### 5.1 Raw QUIC versus WebTransport over HTTP/3

MoQT runs over either. `moqt://` is raw QUIC with the MoQT ALPN; `https://`
is WebTransport over H3.

**Guidance.** WebTransport adds an H3 CONNECT exchange at session setup and a
capsule/stream-framing layer on every stream. Expect the difference to appear
mainly in connection setup and per-stream overhead rather than steady-state
bulk throughput — but measure it rather than assuming, because it also changes
which code path in the relay you are exercising, and browsers can only reach
the WebTransport path.

Run capacity tests on raw QUIC for the cleanest signal, and run at least one
WebTransport pass before claiming production readiness, because that is what
browser clients will use.

### 5.2 Draft version

MoQT draft affects wire format, ALPN, and which properties exist. Our
comparison work ran draft-16. `TIMESCALE` as a Track Property is draft-17 and
later; draft-16 never registered it. Mixed-draft testbeds produce confusing
failures, so pin the draft explicitly on both ends — most tools default
inconsistently (aiomoqt's `loopback_bench` defaults to draft 14, for instance).

### 5.3 Congestion control

**Measured.** There is no single congestion controller common to all relay
stacks we compared except CUBIC:

| stack | available |
|---|---|
| mvfst (moxygen/moqx) | BBRv1, BBRv2, CUBIC, … |
| quinn (cf-moq-rs, moq-dev) | **BBRv1 only**, CUBIC |
| quiche (moq-dev) | **BBRv2 only** (`"bbr"` and `"bbr2"` both map to BBRv2), CUBIC |
| picoquic (aiopquic) | bbr, bbr1, newreno, cubic, dcubic, prague, fast |

So a cross-stack comparison either pins CUBIC everywhere — which nobody runs in
production — or accepts that the controllers differ and says so. We chose the
latter and labelled it. Pretending "BBR on both" is a parity claim is wrong;
BBRv1 and BBRv2 are different algorithms.

**Measured**, picoquic on loopback: bbr beat bbr1 by about 7%. Congestion
control matters, but it is rarely the dominant term — if throughput is flat
across RTT, you are not congestion-window limited and CC is not your problem
(§9.2).

## 6. Adverse conditions and fault injection

Capacity under ideal conditions is the easy half. Production asks what happens
when things go wrong, and the useful question is always the same: **does it
degrade, or does it collapse?**

### 6.1 Network impairment

`netem` on the sender's egress, with the real qdisc as a child so netem's own
queue is not the thing under test:

```bash
tc qdisc del dev "$IF" root
tc qdisc add dev "$IF" root handle 1: netem delay 50ms limit 200000
tc qdisc add dev "$IF" parent 1: handle 2: fq_codel limit 200000 flows 65536
```

Dimensions worth sweeping:

| impairment | `netem` | what it finds |
|---|---|---|
| latency | `delay 50ms` | window sizing, BDP handling (§12.3) |
| jitter | `delay 50ms 10ms distribution normal` | pacing, reorder tolerance |
| loss | `loss 0.5%` | recovery, retransmission cost |
| reordering | `reorder 5% 50%` | stream reassembly |
| corruption | `corrupt 0.1%` | AEAD failures, packet discard paths |
| duplication | `duplicate 1%` | dedup correctness |
| bandwidth | `tbf`/`htb` | behaviour at a hard ceiling |

Two traps, both of which have bitten us:

**netem's own queue must hold a full delay-bandwidth product** or netem drops
and you measure the emulator rather than the relay. `limit 200000` is not
decoration.

**Verify the qdisc afterwards from a separate command.** A forgotten 50 ms delay
once stayed on a perf box for days, because the restore ran from an EXIT trap in
a script that was killed by an outer `timeout`. Diff against an untouched peer
host; do not trust the trap.

### 6.2 Protocol and application faults

These need a cooperating load generator or a deliberately broken client.

- **Publisher stalls or disappears** mid-group. Do subscribers get a clean
  `PUBLISH_DONE`, or hang?
- **Slow consumer.** A subscriber that stops reading. Does the relay apply
  backpressure, shed that subscriber, or buffer without limit until the host
  dies? This is the single most important adverse case for a relay.
- **Abrupt disconnect** versus graceful close, mid-group.
- **Subscribe to a nonexistent track** — expect `DOES_NOT_EXIST (0x10)`.
- **Duplicate subscription** — expect `DUPLICATE_SUBSCRIPTION (0x19)`.
- **Track re-registration** while the relay still holds the previous one
  (§13.3).
- **Auth failures** — `UNAUTHORIZED (0x1)`, `EXPIRED_AUTH_TOKEN (0x5)`.
- **Malformed objects**, oversized objects, duplicate subgroups.
- **Draft/version mismatch** at setup.

### 6.3 Resource exhaustion

- **File descriptors.** Each subscriber costs connections and streams. Run with
  a deliberately low `ulimit -n` and confirm the relay rejects cleanly rather
  than crashing or wedging.
- **Memory pressure.** Fill the host and watch whether the relay sheds or is
  OOM-killed.
- **CPU starvation.** Pin the relay to too few cores. This is the condition
  that produced our worst cascade (§9.3).
- **Disk** for cache and logs.

### 6.4 Recovery, not just failure

The interesting measurement is the recovery curve, not the failure point.
After the impairment is removed:

- Does throughput return to the prior level, or settle lower?
- How long until the latency distribution re-converges?
- Do reconnecting subscribers arrive as a **thundering herd** that re-breaks
  the relay? Self-healing clients that all retry at once make recovery harder,
  not easier — back-off belongs in the client.
- Is memory back where it started? If not, you have found a leak that the
  capacity test could not see.

---

# Part III — Metrics

## 7. What to record

Record all of these every run, on **both** hosts. A result missing the client
side is not interpretable.

### 7.1 Delivery

| metric | why |
|---|---|
| offered vs delivered rate | the gap *is* the loss |
| objects delivered / expected | shedding shows up here before it shows in latency |
| subscribers commanded vs **actually attached** | controllers keep asking for more after the client stops keeping up |
| group completion rate | partial groups are often useless to a decoder |

### 7.2 Latency

p50, p95, p99 and max of end-to-end object latency, plus jitter, as a function
of subscriber count and offered load. Section 8 is about how to obtain these
honestly.

Report the distribution, not a single number, and plot it against load — the
knee matters more than any single point.

### 7.3 Loss, by source

"Loss" with no attribution is not actionable. Separate:

| source | where to read it |
|---|---|
| link / path | `iperf3` preflight; receiver vs sender counts |
| NIC / driver | `ethtool -S` drops, `rx_missed`, ring overruns |
| socket buffer | `/proc/net/snmp` Udp `RcvbufErrors`, `SndbufErrors`, `InErrors` |
| qdisc | `tc -s qdisc show` drops, overlimits |
| QUIC | implementation loss/retransmit counters |
| application queue | relay-internal drop counters |
| deliberate shed | `PUBLISH_DONE` with `TOO_FAR_BEHIND (0x6)` |

That last row is the important distinction: **a relay dropping a late object on
purpose is correct behaviour**, and must not be scored the same as a buffer
overrun.

### 7.4 Resources

- **CPU**, total and per-core. Hot-core skew matters: one core pegged while
  others idle is a single-path limit, not CPU exhaustion, and adding cores will
  not help.
- **RSS, and RSS per subscriber over time.** Take it at intervals, not once.
  The derivative is the interesting part — flat per-subscriber RSS means
  bounded state; a rising line means either a leak or unbounded queueing, and
  you will not distinguish them from a single sample. We watched a load
  generator go from 22 GB to 89 GB of 92 GB inside a single run because receive
  queues had no bound.
- **File descriptors and stream counts**, per process.
- **Relay internals** — forwarder counters, streams opened/closed/reset, cache
  size and evictions, active sessions and subscriptions.

### 7.5 Clock health

`chronyc tracking` offset and RMS, before, during and after. See §8.4 — a
latency number without a clock uncertainty budget is not a measurement.

## 8. Latency measurement in MoQT

This deserves its own section because MoQT offers several mechanisms, they are
not equivalent, and the obvious choice is often wrong.

*Protocol references current as of 2026-10-19; these drafts are moving.*

### 8.1 The four mechanisms

| mechanism | where it lives | measures | works across relays |
|---|---|---|---|
| **Object property** — LOC `TIMESTAMP` `0x10` with `TIMESCALE`; generalised by `draft-frindell-moq-timestamp` (`OBJECT_TIMESTAMP`, `TIMESCALE`, `CLOCK_ID`, `TIMESTAMP_ORIGIN`, `TIMESTAMP_MAPPING`) | per object | per-object time | only if the relay forwards the property |
| **Timing track** — MSF §7 Media Timeline, §8 Event Timeline, §9 Log track | separate track | media time, group-level | yes, but see §8.3 |
| **Private extension** — e.g. moxygen moqtest `0xC000` | per object | per-object time | **no** |
| **Payload-embedded** — timestamp in the first bytes of the object | per object | per-object time | **yes, always** |

`draft-frindell-moq-timestamp` explicitly aims to be "a single, general
representation that these and other specifications can reference", and names
latency visibility as a goal. Curley's draft layers transport-level use of the
LOC properties so relays can make age-based decisions. If you are instrumenting
new code, start there.

### 8.2 Relays forward what they recognise

**Measured, and it cost us a relay from the comparison.** moxygen's moqtest
carried its timestamp in a private extension, `kTimestampExtensionType =
0xC000`. The moq-dev relays forward only extension types they recognise, so the
extension was stripped and subscriber-side latency read **zero**.

Zero is not a good result. It is the absence of a measurement wearing the
costume of one, and any capacity number gated on it is meaningless.

The fix was to move the timestamp into the **object payload**. Payload bytes are
opaque to every relay by definition, so all four relays became measurable at
once. It is inelegant — it consumes payload and requires a cooperating
publisher and subscriber — but for cross-implementation benchmarking it is the
only mechanism that cannot be silently dropped.

**Guidance:** use the standard object property when measuring one
implementation you control; use payload-embedded when comparing
implementations, and verify non-zero latency on every relay before trusting a
single number.

### 8.3 Can a timeline track give you a latency profile?

Short answer: **not at the resolution a latency profile needs.** Four reasons,
in severity order:

1. **Resolution is group, not object.** MSF numbers Groups by capture time in
   its log and metrics tracks. A GOP is typically 1–5 s. You cannot recover a
   p95 in the tens of milliseconds from group-granular marks.
2. **A cadence template gives nominal time, not actual.** MSF's timeline
   includes a template for regular cadences — "objects every 33.33 ms from
   T0". Where the timeline expresses cadence rather than stamping each object,
   the production time is *derived*, so computed latency silently absorbs the
   publisher's own scheduling error. That is exactly the jitter a benchmark
   exists to see.
3. **It carries capture/media time, not hand-to-transport time.** Encoder
   queueing, packaging and publish-side buffering sit between capture and the
   object reaching QUIC. Right for glass-to-glass; wrong for attributing
   latency to the relay.
4. **Location-indexed timelines carry no absolute clock.** MSF distinguishes
   wallclock indexing (§8.4.1) from MOQT Location indexing (§8.4.2). The latter
   gives ordering, not time.

One objection that does **not** hold: that the timing track arrives on a
different stream. For post-hoc analysis you join on Location after the run, and
the timing track's own delivery latency is irrelevant. It only bites if you
compute latency live.

**So:** timeline track for media-time context, seek, and group-level
glass-to-glass. Object-scoped timestamp for per-object delivery latency.

### 8.4 Clock discipline and the uncertainty budget

End-to-end latency across hosts subtracts two clocks. The error in that
subtraction is a property of your measurement, and it must be stated.

The draft makes the dependency explicit: with no `CLOCK_ID`, time is POSIX —
wall-clock, so the hosts must be synchronised. A present, non-zero `CLOCK_ID`
identifies a clock with **no defined relationship to wall-clock time**; tracks
sharing it can be compared to each other but not to a subscriber's clock. If
you want end-to-end latency, you need the POSIX form and disciplined clocks.

**Guidance**, typical achievable offsets:

| setup | offset | adequate for |
|---|---|---|
| `systemd-timesyncd` + public pool | 10s of ms, **and unmeasurable** | nothing latency-related |
| chrony + public pool | 1–5 ms | coarse WAN work |
| chrony + cloud link-local NTP | 100–500 µs | cross-region percentiles |
| chrony + PTP hardware clock (`/dev/ptp0`) | tens of µs | anything |
| PTP with NIC hardware timestamping | sub-µs | — |

Note the second column of the first row. `timedatectl` reporting
`NTPSynchronized=yes` means it reached a server, not that you are close to
true time — `timesyncd` does not report offset at all. Hosts in that state cannot
support any sub-millisecond claim until chrony is installed.

Three rules:

- **One-way error is the sum of both hosts' offsets.** Each host within ±0.5 ms
  means up to 1 ms on a one-way measurement. Against a 45 ms cross-region p50
  that is ~2% and fine; against a 0.4 ms loopback it is fatal.
- **Record offset per run, not once.** A step correction mid-soak appears as a
  latency discontinuity that looks exactly like a relay event.
- **Quote the clock budget beside the percentiles.**

When you cannot get the clocks you need, sidestep them:

- **Same-host loopback** — publisher and subscriber on one host share one
  clock, so skew is zero. The right instrument for sub-millisecond work.
- **Publish-and-subscribe-to-self through the relay** — a true round trip, no
  cross-host clock involved. Halve it only where the path is symmetric, which
  on a WAN it usually is not.
- **Co-located reference subscriber** on the publisher host, to isolate
  publish-side plus relay-ingress cost and subtract it.

A load generator may already account for this: aiomoqt sets `skew_safe_signals`
whenever its role is split across hosts, deliberately avoiding control signals
that depend on synchronised clocks.

### 8.5 Where you stamp and what you read

Two choices that change the number materially, and must be stated:

**Stamp point.** At object creation, at enqueue, or at the moment the object is
handed to the transport. Anything queued before the stamp is invisible. For
relay attribution, stamp as late as possible on the publisher.

**Read point.** First byte of the object versus object completion. For a 64 KB
object on a 1 Gbps path those differ by roughly half a millisecond; for large
objects on constrained paths, far more. Object completion is what a decoder
actually waits for.

## 9. Reading the results

### 9.1 Which layer is the limit?

Almost every surprising benchmark result is a misattribution. Use these tells:

| symptom | likely cause |
|---|---|
| client CPU near saturation, relay far below | **client-bound** — the usual case |
| throughput flat across object size, stream count, RTT *and* CC | **link-bound** |
| one relay core pegged, others idle, zero loss | single-path limit, not CPU exhaustion |
| loss rising with `RcvbufErrors` | socket buffer, fix sysctls |
| latency rising with zero loss and flat throughput | standing queue — bufferbloat, not capacity |

**Every measurement we have taken, on every cloud and host pair, was
client-bound, never relay-bound.** Assume you are measuring your load generator
until you have proved otherwise, and refuse any point where the client host
exceeds ~85% busy.

### 9.2 Window-limited or not

If throughput is **flat as RTT rises**, you are not congestion-window limited,
and tuning windows or congestion control will not help. Throughput for a
window-limited flow falls roughly as `cwnd/RTT`.

**Measured:** flat from 0 to 50 ms emulated RTT while BDP grew to 5.6 MB — that
was a saturated link, not a window. Conversely, on a genuine 45 ms path at
multi-Gbps, windows become the dominant term (§12.3).

### 9.3 The collapse signature

Healthy systems degrade: latency rises, throughput plateaus, loss creeps.
Collapse looks different, and we have a measured example:

```
 36s   35Gbps   7000 target │ 35Gbps  6794 actual │ p90 31ms  │ ramp
 43s   36Gbps   7200 target │ 37Gbps  6947 actual │ p90 66ms  │ ramp
 57s   37Gbps   7400 target │ 1.7Gbps  400 actual │ p90 2.8s  │ collapse
 74s   37Gbps   7400 target │ 2.9Gbps  598 actual │ p90 23s   │ collapse
```

Mechanism: CPU-starved workers stopped servicing their event loops long enough
to miss the peer's idle timeout; connections were dropped; on the next packet
each got a stateless reset; then every self-healing subscriber reconnected at
once and the cycle repeated. The run never returned to its previous high-water
mark.

Two lessons. **A cliff is a finding, not a failure of the test** — note the
load at which it occurs, because production will meet it. And **error codes
need decoding**: that run reported `ConnectionTerminated (1054)`, which is
`PICOQUIC_ERROR_CLASS (0x400) + 30` = `PICOQUIC_ERROR_STATELESS_RESET`. A
transport-library code, not a MoQT one, and not obvious from the log line.

---

# Part IV — Running the lab

## 10. Where the load comes from

### 10.1 The load generator is usually the bottleneck

A relay is tuned C++ with years of work behind it. To measure it you need a
load generator that is comparably fast, and that is a second engineering
project nobody budgets for.

**Guidance: plan for 1.5–3× more client capacity than relay capacity.** A
48-core relay against 48 cores of clients is a 1:1 ratio and will measure the
clients. Our most tightly controlled comparison was an accident — a 2-core
relay against 26 cores of clients — precisely because the relay could not help
but be the bottleneck.

Spread clients across multiple hosts. A single host also runs into per-host
limits (fd ceilings, one NIC, one set of RX queues) that are not the relay's.

### 10.2 Tool survey

| tool | origin | model | best for | blind spot |
|---|---|---|---|---|
| `moqperf_test_client` + `scripts/perf/perf-test.sh` | moxygen / moqx | C++, threads, Prometheus textfile output | highest subscriber density per core; the reference implementation | tuple-driven track shape; tuple field 9 is an **inter-object delay in ms**, not a frequency |
| `moqbench_sub`, `moqbench_meeting` | Cisco quicr | ini-driven profiles, GNU `parallel` | realistic conference and fan-out profiles, per-track config | one process per subscriber — fd and PID limits bite early |
| aiomoqt `adaptive_bench` (plus `pub_bench`, `sub_bench`, `loopback_bench`, `pub_server`) | ours | Python on picoquic, closed-loop SLA controller | independent implementation, so it does not inherit the others' bugs; ramps to an SLA rather than a fixed load | roughly thread-per-subscription; receive queues unbounded by default |
| `picoquicdemo -a perf` | picoquic | draft-banks-quic-performance | same-library C control that isolates binding/language cost | not MoQT — QUIC only |
| `secnetperf` (msquic), quinn `perf_client`/`perf_server` | — | draft-banks-quic-performance | cross-stack QUIC reference on the same path | not MoQT |
| `iperf3` | — | UDP/TCP | path ceiling — the preflight (§2.1) | no QUIC, no MoQT |
| interop runner | — | conformance | correctness gate | not a performance tool |

**Use at least two independent load generators for any number you intend to
publish.** The point of building aiomoqt was that it is Python on picoquic
rather than C++ on mvfst, so it cannot share moqperf's bugs. Where the two
agree, the number is probably real; where they disagree, one of them is wrong
and you have learned something either way.

### 10.3 Known load-generator ceilings

**Measured**, aiomoqt `adaptive_bench` on a 48-core aarch64 host, 20,800 B
objects at 5 Mbps per subscriber:

> ~6,950 subscribers / 37 Gbps, p90 31 ms, 0% loss — then collapse (§9.3).

The limiting behaviours are roughly one OS thread per subscription (~202
threads per worker hosting 200 subscriptions) and unbounded receive queues.
Set `--max-queued-bytes` and `--max-inflight-bytes`; they have no defaults.

**Measured**, stack cost on loopback at 1440 B, same host:

| stack | pps | vs raw UDP |
|---|---|---|
| iperf3 raw UDP `sendto` | 236,800 | — |
| picoquic native C | 199,566 | 84% |
| aiopquic (Python binding) | ~171,000 | 72% |

QUIC costs roughly 19% over raw `sendto`; a Python binding adds about 14% more.
Neither is the order-of-magnitude penalty people assume, which is worth knowing
before you rewrite a load generator that is not actually your problem.

## 11. Host tuning

Apply to **both** relay and client hosts. Client-side tuning is skipped
constantly and then shows up as "relay loss".

### 11.1 Sysctls

```bash
sysctl -w net.core.rmem_max=134217728 net.core.wmem_max=134217728 \
          net.core.rmem_default=33554432 net.core.wmem_default=33554432 \
          net.core.netdev_max_backlog=250000 net.core.optmem_max=1048576 \
          net.ipv4.udp_rmem_min=1048576 net.ipv4.udp_wmem_min=262144 \
          net.core.somaxconn=65535
```

`netdev_max_backlog` defaults to 1000, which is drastically undersized at
multi-Gbps. **Measured:** an untuned client box carried 10,559 `RcvbufErrors`
while the tuned relay beside it had zero — the loss looked like the relay's.

Many relays print their recommended sysctls at startup. Use those.

### 11.2 Queue discipline

Use **`fq_codel`**, not `fq`.

`fq` classifies by socket. A relay serves every subscriber from one UDP socket,
so under `fq` all subscribers collapse into a single flow with a shared packet
limit. `fq_codel` hashes the 5-tuple and keeps them separate:

```bash
tc qdisc replace dev "$IF" root fq_codel limit 200000 flows 65536 quantum 1514
```

### 11.3 NIC and interrupts

- **Rings** to hardware maximum: `ethtool -G "$IF" rx <max> tx <max>`. Check the
  ceiling first — virtio NICs often cap at 256, which is not much headroom.
- **Combined queues** to core count: `ethtool -L "$IF" combined <n>`.
- **RSS/RPS/RFS** so receive processing spreads across cores. A single flow maps
  to one RX queue and therefore one core, which caps single-connection receive
  regardless of how many cores the box has.
- **IRQ affinity** — pin NIC IRQs away from the relay's busiest cores.
- **Offloads**: confirm GRO and GSO are on (`ethtool -k`). See Appendix A.

### 11.4 Process and system limits

```bash
ulimit -n 1048576        # relays and load generators both need this
```

Set it in the unit file or wrapper, not just the interactive shell — a
backgrounded process inherits whatever it was started with. `ulimit -n 65536`
is not enough for several thousand subscribers with multiple streams each.

Also: CPU governor to `performance`, and NUMA awareness on multi-socket hosts
(pin the relay and its NIC IRQs to the same node).

### 11.5 Verify, then verify again

Tuning that is not verified afterwards is a guess:

```bash
sysctl -n net.core.rmem_max net.core.netdev_max_backlog net.ipv4.udp_rmem_min
tc qdisc show dev "$IF"
ethtool -g "$IF"; ethtool -k "$IF" | grep -E "generic-(receive|segmentation)"
cat /proc/net/snmp | awk '/^Udp:/{n++; if(n==2) print}'
```

Diff the result against an untouched peer host. Non-persistent `sysctl -w` and
`tc` settings vanish on reboot, so a box that was tuned last week may not be
tuned now — and a box someone else tuned may carry settings you did not expect.

## 12. Relay configuration

### 12.1 Threads and forwarding

- **IO threads** at core count. Verify the relay actually uses them — we found
  a relay config where `use_relay_thread` had to be true before `threads > 1`
  was accepted at all.
- **Local forwarders** (per-subscriber-thread forwarding) where available;
  this is usually the difference between linear and sublinear fan-out scaling.
- **Allocator.** jemalloc with transparent huge pages
  (`MALLOC_CONF=thp:always`) is a measurable win on allocation-heavy fan-out.

Beware wrapper scripts that contradict the compiled defaults. We found a
launcher defaulting send/recv packet limits to 16/256 where the config header
intended 48/64 — and since the wrapper wins, every benchmark ever run through
it used the wrapper's values. The correct values measured ~14% better
Mbps/core with 5× lower p99 on large objects.

### 12.2 Cache

Relay object cache changes both performance and *semantics*. With cache on, a
relay may answer from cache rather than forwarding, and may accept a publish it
would otherwise refuse. Record the cache setting with every result, and run
capacity tests with it off unless caching is the thing under test.

### 12.3 Flow control at high BDP

This is the configuration item most likely to invalidate a cross-region result.

Bandwidth-delay product at 50 ms RTT:

| aggregate rate | BDP |
|---|---|
| 1 Gbps | 6.25 MB |
| 10 Gbps | 62.5 MB |
| 40 Gbps | 250 MB |

mvfst's defaults are **65 KB per stream** and **1 MB per connection**. At 45 ms
RTT, 65 KB per stream caps a single stream near 11 Mbps — which makes a 16
Mbps-per-subscriber profile impossible on any relay, and we hit exactly that
before raising the load client to 64 MB connection / 16 MB stream.

Per-subscriber BDP stays small in fan-out (16 Mbps × 45 ms ≈ 90 KB), so the
windows that matter are the **publisher's ingest connection** and the
**aggregate socket buffers** — not the individual subscriber sessions. Raise
both ends: a tuned client against an untuned relay is still capped.

---

# Part V — Production readiness

## 13. Overload, work rejection, and migration

Capacity tells you where the relay stops working. Readiness is about **how** it
stops — and whether it comes back.

### 13.1 Shedding: dropping the right work

Under overload a relay has three options: buffer, drop, or refuse. Buffering
without bound is the failure mode, because latency grows until everything is
useless and then memory runs out.

MoQT provides the vocabulary for dropping deliberately:

- **`DELIVERY_TIMEOUT`** as a subscribe parameter — an object older than the
  timeout is not worth sending.
- **`PUBLISH_DONE` with `TOO_FAR_BEHIND (0x6)`** — the relay telling a
  subscriber it has fallen too far behind to continue.
- **Publisher priority, subscriber priority and group order** — which objects
  and which subscribers lose first.

Test that shedding is *selective*. A relay that drops the oldest objects for
the slowest subscriber is behaving well; one that resets random sessions, or
degrades every subscriber equally, is not.

**A measured trap for cross-relay comparison.** moxygen enforces
`DELIVERY_TIMEOUT`; cf-moq-rs parses the parameter and defines the reset code
but never sends it. Asking for enforcement therefore *penalised the
implementation that implemented it correctly* — its subscribers were shed while
the other relay's quietly accumulated latency. We disabled delivery timeout for
the comparison and let the latency percentile be the only quality gate. Score
shedding behaviour separately from capacity; do not mix them in one number.

### 13.2 Admission control and rejection

Refusing work cleanly is a feature. The relevant protocol surface
(draft-16 §13.4.2):

| code | meaning | relay use |
|---|---|---|
| `INTERNAL_ERROR 0x0` | generic failure | last resort |
| `UNAUTHORIZED 0x1` | auth | — |
| `TIMEOUT 0x2` | request timed out | overload tell |
| `NOT_SUPPORTED 0x3` | unimplemented | — |
| `DOES_NOT_EXIST 0x10` | no such track | — |
| `DUPLICATE_SUBSCRIPTION 0x19` | already subscribed | — |
| `UNINTERESTED 0x20` | publisher declines | — |

Plus **`MAX_REQUEST_ID`** (§9.5), which bounds outstanding requests and is the
natural throttle for subscribe-rate overload. Watch the value the server
advertises in `SERVER_SETUP` — a low ceiling silently limits join rate before
anything else does.

What to test:

- At capacity, does the relay **reject new subscribes promptly with a specific
  code**, or accept them and degrade everyone?
- Does a rejected request leave state behind? Loop subscribe/reject and watch
  memory and fd counts.
- Does the client back off, or retry immediately and build a storm?

Prompt, specific rejection is strictly better than silent degradation, and it
is the behaviour an operator can build on.

### 13.3 Stale registration — a trap that looks like something else

**Measured, and it cost more time than any other single issue.** If a publisher
re-registers a track the relay still holds, the relay refuses the new PUBLISH.
The publisher sits at `forward=0` forever and never sends. The subscriber side
reports only:

```
  8s  500Mbps  100 │ 500Mbps  0.0bps  0 │ rx=0 (no data)
 14s  500Mbps  100 │ 500Mbps  0.0bps  0 │ ABORT: rx=0
```

Everything looks healthy — session establishes, `PUBLISH_NAMESPACE` gets
`REQUEST_OK`, `PUBLISH` gets `PUBLISH_OK`. The only tell is `forward=0` in the
`PUBLISH_OK`, which is *normal* before any subscriber arrives, so it reads as
fine.

Mitigations: mint a unique namespace per run (our harness does), and have the
publisher warn when `forward` is still 0 after a subscriber is known to have
joined. Relay-side, it is worth asking whether refusal is the right behaviour
or whether a re-registration should supersede.

### 13.4 Migration

Two different things share the word. Test both.

**QUIC connection migration** — the client's address changes (NAT rebinding,
Wi-Fi to cellular) and the connection survives via connection IDs and path
validation. Test by changing the client's source address mid-stream and
measuring the gap in delivery and any loss.

Note that **eBPF connection-ID steering is about migration, not sharding.**
mvfst builds per-worker `SO_REUSEPORT` sockets unconditionally; the kernel
spreads new connections across them by itself. eBPF steering exists so a
migrated connection reaches the worker that already holds its state. Expect
linear multi-core scaling without it, and do not reach for it to fix a fan-out
throughput problem.

**MoQT session migration via `GOAWAY`** (§9.4) — the server signals the client
to establish a new session and migrate its established subscriptions, then
closes with `GOAWAY_TIMEOUT (0x10)` if the peer takes too long. Subscriptions
ended this way carry `GOING_AWAY (0x4)`.

This is the mechanism for graceful restart, rolling upgrade and load
rebalancing, so it deserves a readiness test of its own:

- Issue `GOAWAY` under load. How long until the relay drains?
- Do subscribers re-establish on another relay without a visible gap?
- Is the reconnect spread out, or is it a thundering herd that breaks the
  target relay (§6.4)?
- Does anything leak on the draining side after the last session closes?

A relay that cannot be restarted under load without a visible outage is not
ready, however good its capacity number.

### 13.5 Backpressure versus buffering

The single most important adverse case: **a subscriber that stops reading.**

Follow it through the stack. QUIC flow control should stop the relay sending;
the relay should then either apply backpressure upstream, drop for that
subscriber, or shed it. What must not happen is unbounded growth of a
per-subscriber queue, because that converts one slow client into a host-wide
outage. Run it deliberately, with RSS sampling, and watch which of the four it
does.

## 14. Readiness checklist

Capacity is one line of this list.

**Characterisation**
- [ ] Path ceiling measured in both directions, recorded alongside results
- [ ] Host loopback control measured on relay and client hosts
- [ ] Clock discipline verified; offset recorded per run (§8.4)
- [ ] Both hosts tuned and **verified after the fact** (§11.5)

**Capacity**
- [ ] Max healthy subscribers at a stated SLO, track shape, transport and draft
- [ ] Confirmed with a second, independent load generator
- [ ] Client host below ~85% busy at the reported point
- [ ] Fan-in as well as fan-out
- [ ] Per-core figures quoted **with object size**

**Behaviour under stress**
- [ ] Degrades rather than collapses past the knee; cliff load recorded
- [ ] Sheds selectively, with correct codes
- [ ] Rejects promptly at capacity with specific `REQUEST_ERROR` codes
- [ ] Slow consumer does not cause unbounded memory growth
- [ ] Recovers to prior capacity after impairment is removed
- [ ] No thundering herd on mass reconnect

**Endurance**
- [ ] Multi-hour soak at ~80% of measured capacity
- [ ] RSS per subscriber flat over the soak
- [ ] No fd, stream or cache leak
- [ ] Latency distribution stable end to end

**Operations**
- [ ] `GOAWAY` drains cleanly under load
- [ ] Restart/upgrade under load without visible outage
- [ ] Connection migration survives an address change
- [ ] Metrics expose the loss-source breakdown of §7.3
- [ ] Error codes in logs are specific enough to act on

**Transport coverage**
- [ ] Raw QUIC and WebTransport/H3 both exercised
- [ ] Target draft version pinned and stated
- [ ] Congestion controller stated, not assumed

---

# Appendix A — Kernel and programmatic acceleration for UDP and QUIC

QUIC lives in userspace and sends small packets, so its cost is dominated by
**per-packet work**: a syscall, a copy, an AEAD seal and header protection,
per packet, at MTU size. Everything below attacks one of those.

Order of practical value for a QUIC relay, highest first.

## A.1 Send-side batching — the main lever

### UDP GSO (`UDP_SEGMENT`)

One `sendmsg` carries a buffer of many MTU-sized packets plus a segment size;
the kernel (and the NIC, via USO) splits it. This is the single largest
available win for a userspace QUIC sender, because it amortises the syscall and
much of the stack traversal across a whole batch.

```c
setsockopt(fd, SOL_UDP, UDP_SEGMENT, &gso_size, sizeof(gso_size));
/* or per-message via cmsg SOL_UDP/UDP_SEGMENT */
```

Caveats: all segments go to **one destination**, so it batches a single
connection, not fan-out across subscribers. Segment size must not exceed path
MTU. Loss of one segment is ordinary packet loss, not batch loss.

**Measured observation:** picoquic implements `UDP_SEGMENT` in `picosocks.c`
and `sockloop.c` — but aiopquic replaces picoquic's socket loop with its own
asyncio transport, so it very likely does not use it. On a link that saturates
first this is unmeasurable; it only becomes visible on a fat path.

### `sendmmsg` / `recvmmsg`

Multiple messages per syscall. Unlike GSO, `sendmmsg` carries a **per-message
destination address**, so in principle it can batch across many peers — which
is exactly what a fan-out relay wants.

Whether an implementation exploits that is another matter. **Measured
observation:** mvfst's batching is per-connection, because its writer takes one
address per batch. Cross-connection `sendmmsg` — one syscall carrying packets
to many subscribers — is therefore an unexploited lever in the fan-out case,
and would be a change inside mvfst rather than a tuning knob.

### io_uring

Submission and completion rings shared with the kernel, so batches of sends and
receives are queued without a syscall each. Adds zero-copy send
(`IORING_OP_SEND_ZC`) and registered buffers. Larger engineering commitment
than GSO for a smaller incremental win once GSO is in place, but it compounds
with it.

## A.2 Receive side

### UDP GRO (`UDP_GRO`)

The mirror of GSO: the kernel coalesces consecutive datagrams from the same
flow into one `recvmsg`, with the segment size reported via cmsg. Matters most
on the subscriber side and on relay ingest.

```c
setsockopt(fd, SOL_UDP, UDP_GRO, &one, sizeof(one));
```

### RSS, RPS and RFS

A single UDP flow hashes to one RX queue and is processed by one core. That
caps single-connection receive throughput no matter how many cores the host
has — and it is why single-flow benchmarks mislead. Many flows spread across
queues; `ethtool -L` sets queue count, RPS/RFS spread in software where
hardware queues are scarce.

### Busy polling

`SO_BUSY_POLL` and `net.core.busy_poll` trade CPU for latency by polling the
NIC instead of waiting for an interrupt. Useful for latency-critical relays
with cores to spare; wasteful otherwise.

## A.3 Bypassing the stack

### AF_XDP

Userspace gets raw frames from a NIC queue through a shared UMEM, bypassing the
kernel network stack. The highest-throughput option and by far the most work:
you take over a queue, you implement what you need of the stack yourself, and
zero-copy mode requires driver support. Appropriate for a dedicated appliance,
rarely for a general relay.

### XDP / eBPF at the driver hook

Programs that run before `skb` allocation; can drop, redirect or steer. Useful
for DDoS filtering in front of a relay and for connection-ID steering.

### `SO_REUSEPORT` and connection-ID steering

`SO_REUSEPORT` gives each worker its own socket on the same port, with the
kernel spreading new connections by hash. **This is the normal sharding
mechanism and it needs no eBPF** — mvfst builds these sockets unconditionally.

eBPF (`SO_ATTACH_REUSEPORT_EBPF`) replaces the hash with your own selection,
which QUIC needs only so that a **migrated** connection — new address, same
connection ID — reaches the worker holding its state. Expect linear multi-core
scaling without it.

## A.4 Copies, pacing and crypto

### `MSG_ZEROCOPY`

Avoids the userspace-to-kernel copy for large sends, with completion notified
asynchronously on the socket error queue. Worth it above roughly 10 KB per
send, which for QUIC means it pairs with GSO batches rather than single packets.

### Pacing

BBR and similar controllers depend on paced sending. `SO_MAX_PACING_RATE`, or
the `fq` qdisc's pacing, or implementation-internal pacers. Note the tension
with §11.2: `fq` paces but classifies by socket, which is wrong for a relay's
single socket; implementation-level pacing plus `fq_codel` is usually the right
combination.

### Crypto

Per-packet AEAD and header protection are a real share of QUIC's cost, so
crypto acceleration is not a side issue:

- **CPU instructions** — AES-NI, VAES/AVX-512, ARMv8 crypto extensions. Already
  in use via the TLS library; the thing to check is that the library is built to
  use them.
- **Library version matters more than people expect.** **Measured:** OpenSSL
  3.5.5 over 3.0.2 gave a reproducible **+6.8%** on a QUIC loopback workload
  (three reps, no overlap between the groups). OpenSSL 3.0.x carries
  provider/fetch overhead on repeated small operations; 3.5 recovers much of
  it. Ubuntu 22.04 ships 3.0.2, so a stock box pays this.
- **QAT** and similar offload devices exist but are oriented at bulk crypto;
  per-packet QUIC AEAD is a poor fit for an offload with per-request overhead.

## A.5 Hardware offload: what exists, and what does not

This is worth stating plainly because "offload" means something specific here.

**Exists and QUIC benefits:**

| offload | note |
|---|---|
| checksum (tx/rx) | universal |
| **USO** — UDP segmentation offload | what backs `UDP_SEGMENT` |
| **GRO** — receive coalescing | `UDP_GRO` |
| RSS / flow steering | spreads flows across cores |
| hardware timestamping / PTP | useful for the clock budget (§8.4) |

**Exists but TCP-only, so QUIC cannot use it:**

| offload | why not |
|---|---|
| TSO / LRO | TCP segmentation semantics |
| **kTLS offload** | NICs can offload the TLS record layer for TCP. QUIC's crypto is per-packet, interleaved with transport state, and includes header protection over fields the NIC does not parse. There is no equivalent. |

**Does not exist in commodity NICs:** a QUIC *protocol* offload — nothing you
enable with `ethtool` makes the NIC do QUIC. There is no analogue to TSO or
kTLS for QUIC, and this is the main structural reason a userspace QUIC stack
pays more per byte than kernel TCP.

**The frontier: SmartNICs and DPUs.** NVIDIA BlueField, AMD Pensando, Intel IPU
and cloud "accelerated networking" shapes move parts of the datapath onto the
card. The open question — and it is genuinely open, we have not found published
numbers — is whether that acceleration reaches a **userspace QUIC send path**
or only the hypervisor overlay beneath it. If it only accelerates the overlay,
a QUIC relay sees little benefit, because its cost is above that line.

That question is directly testable: run identical relay and load on an
accelerated shape and a non-accelerated shape of the same size, and instrument
both with `perf record` and `strace -c` rather than throughput alone. A null
result is a real finding and worth publishing either way.

## A.6 Diagnosing which of these is your limit

```bash
strace -c -f -p <relay pid>    # is it ~1 sendto per packet? then GSO is the lever
perf record -g -p <relay pid>  # crypto? copies? scheduler? allocator?
ethtool -S "$IF" | grep -iE "drop|miss|error"
ethtool -k "$IF" | grep -E "segmentation|receive-offload"
cat /proc/net/snmp | awk '/^Udp:/{n++; if(n==2) print}'
```

The ordering that matters: confirm the path is not the limit (§2.1), confirm
the host is not the limit (loopback control), *then* look at syscall counts. A
flamegraph of a link-limited run tells you nothing.

---

# Appendix B — Reference measurements

Everything here was measured by us and is reproducible from this repo. Use it
to sanity-check a new testbed, not as a target.

## B.1 Path ceilings, 1400 B UDP datagrams

| path | pps | Gbps | note |
|---|---|---|---|
| laptop loopback, 16-core WSL2 | 305,318 | 3.42 | 0% loss |
| Linode box loopback, 8-core EPYC 7713 | 236,800 | 2.65 | 19% receiver loss at full rate |
| Linode box ↔ box | 94,256 | 1.06 | 8-core plan egress cap; 4 flows do not raise it |

## B.2 Stack cost on loopback, 1440 B

Same host, same MTU on loopback and wire — a larger loopback MTU was checked
for and did not apply.

| stack | pps | vs raw UDP |
|---|---|---|
| iperf3 raw UDP `sendto` | 236,800 | — |
| picoquic native C | 199,566 | 84% |
| aiopquic (Python binding) | ~171,000 | 72% |

## B.3 Relay capacity

| relay | subscribers | aggregate | p90 | cores used | conditions |
|---|---|---|---|---|---|
| moqx/mvfst | **~6,950** | 37 Gbps | 31 ms | 15 of 48 | AWS c7gn.12xlarge pair, same subnet, 20,800 B objects, 5 Mbps/sub |

This row was **client-bound, not relay-bound** — the relay sat at 15 of 48
cores. It is a lower bound on the relay.

Carry the caveats with the numbers: congestion controllers differ across
stacks (§5.3).

## B.4 Object size versus per-core throughput

moqx, fixed hardware, 240 subscribers held constant:

| object size | Mbps/core |
|---|---|
| 1 KB | 149 |
| 66 KB | 821 |

Near-linear below 4 KB, flat above 33 KB, knee at 8–16 KB.

## B.5 Build and library

| change | effect |
|---|---|
| OpenSSL 3.0.2 → 3.5.5 | **+6.8%**, three reps, no overlap |
| send/recv packet limits 16/256 → 48/64 | ~14% better Mbps/core, 5× lower p99 on large objects |
| picoquic bbr1 → bbr | ~7% |

---

# Appendix C — Preflight and harness scripts

In `aws/` (named for where they were first used; they work against any host
pair):

| script | does |
|---|---|
| `tune-client.sh` | sysctls and NIC rings; run on **both** hosts |
| `start-relay.sh` | relay with tuned flags, jemalloc, THP, and the UDP sysctls it recommends |
| `build-aiomoqt.sh` | load generator from source, handling the four silent build caches of Appendix D |
| `run-bench.sh` | one split-host run, fresh namespace per run |
| `pub.sh` / `sub.sh` | publisher and subscriber halves |
| `bdp-sweep.sh` | throughput versus emulated RTT, netem layered over fq_codel |
| `obj-sweep.sh` / `pub-sweep.sh` | object size and parallel stream sweeps |
| `conc-pub.sh` | separates per-connection from per-host limits |
| `loopback-sweep.sh` | same-host control, no NIC in the path |
| `start-pqperf.sh` | `picoquicdemo` QUIC-PERF server, the no-Python control |
| `cleanup-perf.sh` | removes the footprint; **run it, and verify the qdisc afterwards** |

Retained measurement CSVs are in `aws/data/`.

---

# Appendix D — Pitfalls

Each of these cost us real time.

## Measurement

- **An uncharacterised path explains everything and nothing.** A full day of
  QUIC analysis against an unmeasured 1 Gbps link. Preflight first (§2.1).
- **Sender-side counters lie under loss.** A 2.4–2.6 Gbps "aggregate" across a
  link dropping 66%. Read the receiver.
- **Zero latency is not low latency.** A stripped timestamp extension reads as
  0 ms (§8.2).
- **`NTPSynchronized=yes` is not a clock budget.** `timesyncd` does not report
  offset at all (§8.4).
- **Reaching a configured maximum ends a controller run**, which reads as a
  ceiling. Check whether the tool stopped because the *relay* stopped.
- **A 60 s confirm is optimistic**; loads that pass it drift over minutes.

## Protocol and tooling

- **Relays forward only the extensions they recognise.** Payload bytes are the
  only universally opaque carrier.
- **Stale track registration reads as a subscribe failure** (§13.3).
- **Delivery-timeout enforcement is asymmetric** across implementations and
  penalises the correct one (§13.1).
- **`DELIVERY_TIMEOUT`, priorities and group order change what gets dropped** —
  state them.
- **A tuple field named "frequency" may be a delay in milliseconds.** Read the
  source of your load generator's track spec.
- **Tools default to different drafts** — `loopback_bench` defaults to 14. Pin
  it on both ends.
- **Integer truncation in subscriber distribution**: `subscriber_max /
  threads` truncates, so 25, 26, 28 and 31 all attach 24 over 8 threads. Round
  requests to the thread grain.

## Host and build

- **`fq` collapses a relay's subscribers into one flow.** Use `fq_codel`
  (§11.2).
- **`netem` left behind contaminates everyone's results.** An EXIT trap does
  not run when an outer `timeout` kills the script. Verify from a separate
  command and diff against a peer (§6.1).
- **Client-side tuning is skipped and then blamed on the relay** — 10,559
  `RcvbufErrors` on the untuned side, zero on the tuned one.
- **`ulimit -n` set interactively does not reach a backgrounded daemon.**
- **Wrapper script defaults can contradict compiled defaults, and the wrapper
  wins** (§12.1).
- **Four build caches fail silently**: `setuptools_scm` missing under
  `--no-build-isolation` gives version `0.0.0`; git `safe.directory` on an
  rsynced tree gives submodule revisions `unknown`; setuptools' `build/` keeps
  a stale link so no RUNPATH is baked; the native build dir keeps a stale
  OpenSSL. The result is a binary that reports the wrong provenance and links
  the wrong crypto library, with no error anywhere.
- **Two libraries named `libcrypto.so.3` collide.** Python's own `ssl` loads
  the system one first, so a custom OpenSSL needs a baked RUNPATH or
  `LD_LIBRARY_PATH`. Verify with the tool's own version command, which is the
  only reliable report of what actually loaded.

## Process

- **`pkill -f <pattern>` matches the invoking shell** when the pattern appears
  in its own command line. Put kills in script files.
- **Python block-buffers stdout when redirected** — pass `-u` or a run looks
  hung for minutes.
- **Public IPs change on stop/start** for cloud instances.
