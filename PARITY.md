# Relay parity audit

Read before publishing any cross-relay number. Written 2026-08-23 from the
sources, not from documentation, with file references so each claim can be
rechecked when the code moves.

The comparison is only meaningful if the relays differ in *implementation* and
not in what they were told to do. Some settings can be equalised, some cannot,
and the ones that cannot have to be stated alongside the result.

Sources audited:
- moqx `src/MoqxRelayServer.cpp`, `src/config/Config.h`, `config.bench.yaml`
- cf-moq-rs `moq-native-ietf/src/quic.rs` (branch `fix/draft16-nopri-subgroup-types`)
- moq-dev `rs/moq-native/src/{quic,quinn,quiche}.rs`

---

## 1. Settings compared

| setting | moqx (mvfst) | cf-moq-rs (quinn) | moq-dev (quinn / quiche) |
|---|---|---|---|
| congestion control | bbr2 (`--cc`) | **BBRv1, hardcoded** | BBRv1 default, `--server-quic-congestion-control` |
| max concurrent uni streams | 8192 | **100 (quinn default), hardcoded** | 1024 default, `--server-quic-max-streams` |
| idle timeout | 30 s | **10 s, hardcoded** | 30 s, flag |
| keep-alive | — | 4 s | 5 s, flag |
| MTU discovery | off (`canIgnorePathMTU` false) | **off, hardcoded** | off default, flag |
| GSO | on | quinn default (on) | on default, flag |
| UDP socket buffer | 32 MB | 32 MB via our patch | 32 MB via our patch (new) |
| send batching | per-connection GSO, 48 pkts | quinn internal | quinn / quiche internal |

## 2. Equalised in the harness

- **`--server-quic-max-streams 8192`** on both moq-dev backends, matching what
  moqx advertises. Default was 1024.
- **`--server-quic-congestion-control delay`** (BBR) explicitly, rather than
  relying on the default.
- **GSO left to its default** (on) on both moq-dev backends — see §7 for why
  passing it explicitly is a trap.
- **`--ignore-path-mtu` removed from moqx.** This is the one that mattered
  most and it was our own doing: the flag is not a moqx default
  (`Config.h` sets `canIgnorePathMTU{false}`), and both Rust relays disable MTU
  discovery, so quinn and quiche stay pinned at the 1200-byte initial MTU. With
  the flag set, moqx alone could emit larger datagrams and move the same bytes
  in fewer packets — a provisioning difference that would have read as a relay
  advantage, especially on a 9000-MTU cloud network. `MOQX_PMTU=1` measures the jumbo
  case deliberately, as a separate reported result.

  Parity and realism agree here. A relay's ingest and edge legs cross the
  public internet, so ~1200 bytes is what those paths actually get; the jumbo
  figure is not a suppressed advantage but one that would not survive
  deployment. Measured on the bandwidth flavour, MTU is worth 1.17x
  (1529 vs 1304 Mbps/core) while object size is worth 2.9x (1304 vs 447) — so
  the object shape, not the MTU, is what moves this number.

  Relay-to-relay links are the exception, being a controlled path where the
  MTU is a choice. That deserves its own flavour rather than being folded into
  edge numbers, and the batching economics differ there too since a mesh link
  carries aggregated traffic. Future work.

## 3. Cannot be equalised — state these with any published figure

- **cf-moq-rs allows 100 concurrent unidirectional streams per connection**,
  quinn's default, with no flag. moqx advertises 8192 and moq-dev is set to
  match. If cf hits that ceiling it will show as stalling rather than as a CPU
  limit, so a cf result at high load is a *configuration* ceiling and must be
  labelled as such, not reported as the stack's capacity.
- **cf-moq-rs pins a 10 s idle timeout with a 4 s keep-alive.** Not a
  throughput factor, but it is why cf sessions expire faster in manual testing.
- **Congestion control is not strictly like-for-like.** quinn and quiche both
  implement BBRv1; moqx's `bbr2` is a different algorithm. Run `MOQX_CC=bbr`
  for an algorithm-matched comparison, and report `bbr2` separately as moqx's
  best configuration.

## 4. Deliberately not equalised

- **Thread count** is the same integer for every relay, but it does not buy the
  same thing. mvfst creates one SO_REUSEPORT socket per worker; cf-moq-rs and
  moq-dev/quinn have a single socket and one driver task; moq-dev/quiche shards
  N sockets via `MOQ_QUICHE_SHARDS`. That asymmetry *is* the finding — it is
  what the architectures actually do — so it stays, and gets described rather
  than removed.
- **Local patches** to cf-moq-rs and moq-dev are all fixes for outright
  breakage (a draft-16 decode gap, an unbuildable quiche feature, a missing
  spec-MUST parameter), not tuning. They make the relays work, not go faster.
  See the public-source patches in `patches/` and their commit context.

## 5. Fixed: moq-dev UDP socket buffers

moq-dev set no socket buffer size anywhere, so it ran on the kernel default
while moqx and cf both requested 32 MB — a handicap to moq-dev, not a property
of quinn or quiche. `bind.rs` now has `set_buffer_sizes()`, honouring the same
`MOQ_UDP_BUFFER_BYTES` variable the cf patch uses, applied on both the plain and
SO_REUSEPORT paths. Unset leaves the kernel default alone. Verified with
`ss -ulnpm`: `rb33554432, tb33554432` with it set.

Both moq-dev binaries were rebuilt and deployed; the previous ones are kept
beside them as `.prev`.

## 6. Validation, 2026-08-23

All four relays at parity settings, 200 subscribers on a 2-core dev pair.
Nothing is near saturation here — this shows the settings work, not capacity:

| relay | Mbps/core | proc cores | p99 |
|---|---|---|---|
| moqx | 447 | 0.38 | 23 ms |
| cf-moq-rs | 353 | 0.51 | 22 ms |
| moq-dev quinn | **646** | **0.25** | 0 ms |
| moq-dev quiche | 394 | 0.43 | 0 ms |

moq-dev/quinn is the most CPU-efficient of the four at this load. That is the
opposite of the assumption behind this whole exercise, and it may well reverse
under fan-out where quinn's single socket has to serve every subscriber — but
it is recorded here so the capacity run tests the claim rather than confirming
a prior.

## 8. Latency is unmeasurable for moq-dev - a private extension ID, not a moq-dev gap

**Any latency-gated result for moq-dev is invalid until this is resolved.**

`moqperf_test_client` derives end-to-end latency from a timestamp the publisher
attaches as an object extension
(`objHeader.extensions.getIntExtension(kTimestampExtensionType)`,
MoQPerfTestClient.cpp:206). Against both moq-dev backends every sample reads
`Latency(interval min/avg/max): 0/0.0/0 ms` - not excellent latency, no
measurement at all, because `latencyObjects` never increments.

Isolated with a controlled swap: the same publisher, the same explicitly
announced namespace, and the same `--delivery_timeout 0`, pointed at **moqx**,
reports **5.6 ms**. So the publisher sends it and moq-dev does not deliver it.

The cause is narrower than "moq-dev drops extensions", which was the first
conclusion here and was wrong. moq-dev does implement object extensions and
specifically the **standard Timestamp Object Property, 0x10**
(`ietf/group.rs`: `PROP_TIMESTAMP`, `encode_object_time`, `decode_object_time`,
`GroupFlags::has_extensions`). moxygen's moqtest instead carries its timestamp
in a **private extension type 0xC000**, chosen to avoid colliding with the test
integer extensions (`moqtest/Utils.h:16`). moq-dev does not carry extension
types it does not recognise, so the private one is lost across the hop.

Two defensible fixes, and they are not mutually exclusive: moqtest could emit
the standard 0x10 timestamp, which every conforming relay would then preserve;
and a relay arguably ought to forward unknown object extensions untouched.
Worth raising in both places, but the harness side is the cheaper fix.

Consequences:

- The capacity search gates on p99 latency. For moq-dev that gate can never
  fire, so it would only ever fail on shedding and would report a capacity far
  above the truth. This would have quietly produced the headline result.
- moq-dev is not delivering the same object payload as the others, so even the
  throughput comparison is not strictly like-for-like.
- Worth reporting upstream on its own merits: draft-16 expects object
  extensions to be forwarded end to end.

Until fixed, do not report a latency-gated capacity number for moq-dev, and say
plainly that its latency is unmeasured rather than zero. The first 50ms run
showed why: moqx and cf converged at the SLO boundary (489ms and 502ms against
a 512ms budget) while moq-dev/quinn ran on to 2062 subscribers because nothing
could stop it. That 2062 is the absence of a measurement, not a capacity.

The workaround now in the harness is a **delivery-rate gate**: the publisher
emits on a fixed clock (one object every OBJ_DELAY_MS), so each subscriber
should receive 1000/OBJ_DELAY_MS objects per second, and a relay that cannot
keep up delivers fewer. It needs no extension, works for every relay, and is
reported as `obj_per_sub`. It is coarser than latency - it catches falling
behind, not queueing delay - so it complements the SLO rather than replacing
it.

## 7a. Four-way, Linode pair, 2026-08-24

600 subscribers, 4 relay threads, fanout flavour (3788 B, ~0.9 Mbps/sub), moqx
tuned with `--send-pkts 48`. Relay and client on the Linode pair,
8 vCPU each, RTT 0.485 ms. Client peaked at 31% busy, so the relay is the limit.

| relay | Mbps/core | proc cores | p99 | hottest core |
|---|---|---|---|---|
| moqx (mvfst) | 353.5 | 1.63 | 121 ms | 29% |
| cf-moq-rs (quinn) | 295.7 | 2.25 | 154 ms | 36% |
| moq-dev (quinn) | **451.0** | **1.19** | unmeasured (§8) | 24% |
| moq-dev (quiche) | 205.0 | 2.71 | unmeasured (§8) | 68% |

moq-dev/quinn is again the most CPU-efficient, matching the 2-core dev-pair result in §6
(quinn 646 vs moqx 447 Mbps/core at 200 subs) on different hardware at a
different load. Two independent observations now point the same way, so this is
not noise - though §8 means moq-dev is doing less work than the others, and how
much of the gap that explains is not yet known.

quiche is the outlier in the other direction: 2.71 cores for the same traffic,
with one core at 68% while the host averaged 35% - the single-core signature
the per-core sampling was added to catch.

## 7. Trap found while wiring this up

`--server-quic-gso true` makes the relay exit instantly with
`Error: No such file or directory (os error 2)`: clap reads the value as a
positional config path rather than the flag's argument. GSO already defaults
on, so the flag is simply omitted. A relay that dies at startup shows up as
`PUBLISH_FAILED`, which reads like a harness fault rather than a bad flag.
