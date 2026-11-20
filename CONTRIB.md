# Contribution transports

`moqbench contrib` sends one live MPEG-TS service over an emulated WAN with each contribution
transport in turn, under the same seeded impairment, and measures what comes out: end-to-end delay
PCR by PCR, residual loss, output breaks at fixed playout buffers, the share late or lost at equal
latency budgets, recovery overhead, and recovery from outages. The lanes are SRT, RIST and MoQT
(moq2ts over moqx, and aiomoqt over moqx).

## Quick start

```bash
docker build -t moqbench-contrib-tools:dev   -f build/contrib-tools.Dockerfile build
docker build -t moqbench-contrib-ffmpeg:n9.0.2 -f build/contrib-ffmpeg.Dockerfile build
docker build -t moqbench-contrib-moq2ts:dev  -f build/contrib-moq2ts.Dockerfile build
docker build -t moqbench-contrib-aiomoqt:dev --build-arg AIOMOQT=git+https://github.com/gmarzot/aiomoqt@gmarzot-msfts \
    -f build/contrib-aiomoqt.Dockerfile build
docker pull ghcr.io/openmoq/moqx:v0.3.7-amd64

./bin/moqbench contrib up docker-contrib-example          # namespaces, veths, routes; prints the checks
./bin/moqbench contrib calibrate docker-contrib-example   # what the emulator applies, per profile
./bin/moqbench contrib run docker-contrib-example example-contrib --scenario dev \
    --lane srt --lane moqt-moq2ts --profile random-loss --variant D=4xRTT --variant relay_cc=bbr2 --repeats 1
./bin/moqbench contrib report results/contrib/<run>       # re-analyze run folders
./bin/moqbench contrib explain results/contrib/<run>      # why each late stretch was late
```

Results land in `results/contrib/<deployment>-<stamp>/<NNN>-<lane>-<profile>-<variant>-r<repeat>/`,
with `suite.json` and `host.json` beside them. `results/` is ignored by git. Each run prints one
line: delay, PCR loss and breaks at its first output (for a MoQT lane, the first playout buffer), and
late or lost at each D from raw arrivals when the lane has them.

## Topology

```
 src ──wa0/va0── wan ──vb0/wb0── dst          one host, one CLOCK_REALTIME
  │  239.79.0.1:5000                 │  239.79.0.2:6000.. (outputs)
  tsp regulate → senders, input tap  receivers → playout → output taps, analyser
```

- Three network namespaces, each held by a container (`mbc-<deployment>-src|wan|dst`), joined by
  plain veth pairs: no bridge, NAT or conntrack. `wan` routes between the two /24s.
- Segmentation offloads are off on every veth, and RPS steers each veth's receive queue to its
  namespace's CPUs, and so does the multicast dummy in each endpoint. Without RPS a sender that
  migrates between CPUs reorders its own packets.
- Each lane process runs in its own container inside one namespace. Its signals reach every process
  of its pipeline, so files flush on stop. Every container carries the label
  `org.moqbench.contrib=<deployment>`, and `down` removes only those.
- Sources fan out on a multicast group over a dummy interface in `src`; outputs do the same in `dst`.
- `dual_path = true` adds a second, independent pair of links (`va1`/`vb1`).

**Placement.** MoQT lanes cross the WAN on one hop of the two:

- `last-mile` (default): publisher and relay in `src`, the subscriber in `dst`. The relay's delivery
  to the receiver is impaired.
- `first-mile`: the relay in `dst`. The publisher's delivery to the relay is impaired.

With `relay = "none"` (first-mile only, aiomoqt LOC and MSFTS lanes) there is no relay: `sub_media`
listens on raw QUIC in `dst` and `pub_media --pub-ns` in `src` dials it across the WAN, one hop with
the sender calling the receiver, as SRT's caller and listener do. The publisher's controller is on
that hop.

SRT and RIST are one hop either way.

## Impairment

All of it lives in `wan`, so no endpoint or relay socket is charged for queued packets. Each
direction has a fixed tree built once per run, on its egress interface:

```
root 1:  netem limit 100000 delay D [slot MIN MAX] rate 1gbit [loss gemodel p r 1-h 1-k] seed S
10:      netem delay 1ms reorder P%       (when the profile reorders)
20:      tbf rate CAP burst 64kb latency 60ms   (CAP = cap x the stream's Ethernet rate)
30:      fq_codel                         (cap_aqm = "fq_codel": replaces tbf's drop-tail FIFO)
```

- **Jitter** uses `slot`. `rate 1gbit` keeps the delay line FIFO, so jitter does not reorder;
  reordering is a separate, small stage.
- **Events** change the tree in place (`tc qdisc change`, restating every netem parameter). Outages
  add the direction's ingress interface to an nft set that one counted drop rule matches.
- **Bottleneck buffer.** `cap_latency_ms` sizes the tbf FIFO as queueing delay at the cap rate (60 ms
  by default). The bloat profiles deepen it to bloated sizes; with `cap_aqm = "fq_codel"` FQ-CoDel at its
  defaults manages that queue instead.
- **Cross traffic.** A `cross` event runs iperf3 from `src` to a server in `dst` through the same
  tree, so the queue it builds depends on load: a TCP flow (the kernel's controller, or `cc`) fills
  the bottleneck buffer the way a competing upload does. The k-th cross event uses port 5201 + k.
- **Counters.** nft accounting counters count every packet per direction before any qdisc, and cross
  traffic apart. Each run records them every second, with every qdisc's packets, drops and backlog.
- **Reported loss** is the loss applied, not the loss commanded: netem drops, cap drops and outage
  drops are reported apart.
- **Seeds.** The two directions have different seeds; every lane of a repeat sees the same loss
  pattern.

Profiles are `profiles/contrib/<id>.toml`:

| id | name | RTT | jitter (slot) | forward loss | cap | events |
|---|---|---|---|---|---|---|
| `lan` | control LAN | 1 ms | — | — | 2.0× | — |
| `random-loss` | reference WAN | 50 ms | — | 0.5 % random | — | — |
| `regional-clean` | clean regional | 20 ms | 0–2 ms | GE 0.02 % | 2.0× | — |
| `national-managed` | managed national | 60 ms | 0–6 ms | GE 0.1 % | 1.5× | 200 ms loss run, 64 ms link fail |
| `transcontinental` | unmanaged transcontinental | 80 ms | 0–16 ms | GE 0.5 % | 1.5× | 256 ms outage, 8 ms route step |
| `intercontinental-good` | intercontinental, good | 100 ms | 0–10 ms | GE 0.3 % | 1.5× | 128 ms outage, 4 ms step |
| `longhaul-lossy` | long-haul unmanaged | 250 ms | 0–20 ms | GE 1 % | 1.3× | 400 ms outage, 16 ms step |
| `intercontinental-bad` | bad intercontinental | 400 ms | 0–50 ms | GE 3 % | 1.2× | 1.6 s outage and 64 ms step every 2 min; 0.5 s loss runs |
| `cellular-handover` | cellular uplink | 60 ms | 0–20 ms | GE 2 % | 1.5× | handover gap and RTT rise; rate dip to 0.8× |
| `leo-satellite` | LEO satellite | 60 ms | 0–14 ms | GE 0.4 % | 1.5× | 45 ms burst and RTT step every 15 s |
| `outage-ladder` | outage ladder | as `transcontinental` | | | 1.5× | 0.5, 2, 5, 30 s: forward only, then both |
| `bloat-cable` | bloated cable uplink | 30 ms | — | — | 2.5×, 500 ms FIFO | TCP upload 20 s on, 20 s off |
| `bloat-cellular` | cellular uplink, deep buffer | 60 ms | 0–10 ms | — | 1.2×, 1 s FIFO | rate dips to 0.8× (8 s) and 0.5× (5 s) every 30 s |
| `cable-fqcodel` | bloat-cable with FQ-CoDel | 30 ms | — | — | 2.5×, FQ-CoDel | as `bloat-cable` |

- **Profile keys.** `forward`, `reverse`: `delay_ms` (default RTT/2), `slot_ms`, `rate`,
  `loss = { gemodel = [...] }` or `{ random = PCT }`, `reorder_pct`, `cap` or `cap_mbit`,
  `cap_latency_ms`, `cap_aqm` (`fifo` or `fq_codel`), `seed`. `[[event]]` tables have a `kind`:
  - `outage`: `for_ms`, `direction`;
  - `step`: `delta_ms`, `for_s`;
  - `rate`: `factor`, `for_s`;
  - `cross`: `for_s`, `proto` (`tcp`, default, or `udp`), `load` (udp: its rate as a multiple of the
    stream's), `streams`, `cc` (tcp).

  Every event takes `at_s` and an optional `every_s`.
- **Inheritance.** `base` extends another profile.
- **Run length.** `run_s` sets a minimum length for runs on that profile.

## Source

| tier | video | audio | multiplex |
|---|---|---|---|
| `mpeg2-20m` (primary) | 1080p25 MPEG-2, 17 Mb/s CBR, 1 s closed GOP | MPEG-1 Layer II 192 kb/s | 20 Mb/s, null-padded |
| `mpeg2-8m` | 720p25 MPEG-2, 6.5 Mb/s | as above | 8 Mb/s |
| `mpeg2-40m` | 1080p25 MPEG-2, 36 Mb/s | as above | 40 Mb/s |
| `h264-20m` (LOC lane) | 1080p25 H.264, 17 Mb/s CBR, 1 s GOP | AAC 192 kb/s | 20 Mb/s |

- **Fixture.** PAT/PMT every 100 ms and PCR every 20 ms. FFmpeg n9.0.2 encodes `testsrc2` once
  per tier and length into `.testbed/contrib/<deployment>/sources/`.
- **Pacing.** `tsp -P regulate --pcr-synchronous` paces the fixture on its PCR to the source group
  in 1316-byte datagrams.
- **Soak.** The `soak` scenario sets `source_mode = "live"` for runs of hours.
- **LOC lane.** It needs H.264, so it uses `h264-20m` whatever the suite's tier.

## Lanes

| lane | sender (src) | receiver (dst) |
|---|---|---|
| `srt` | `srt-live-transmit` from the source group, caller | `srt-live-transmit` listener to the output group |
| `rist` | `ristsender -p 1` | `ristreceiver -p 1` |
| `moqt-moq2ts` | `tsp` → SRT (latency 100) → `moq2ts-cli --transparent --draft 18 --quic-idle-timeout 3000` → moqx | `moq2ts-receiver --draft 18` → PCR-locked playout |
| `moqt-aiomoqt-loc` | `pub_media --ts udp://@…` (LOC) → moqx | `sub_media --frame-log` (sha1 per frame) |
| `moqt-aiomoqt-msfts` | `pub_media --ts udp://@… --msfts --msfts-packets 7` → moqx | `sub_media --ts-out` → PCR-locked playout |
| `moqt-aiomoqt-dual` | `pub_media` to one aiomoqt-relay over two paths | `sub_media --ts-out` → playout |

**SRT** sets `-chunk:1316` and `lossmaxttl=32` on both ends, and takes its input from UDP, never a
pipe. Its buffers and flight window hold at least (latency + RTT + 1 s) of input at 1.5×, doubled.
`latency` = D.

**RIST** uses `buffer` = D, `rtt-min` = RTT/2 and `rtt-max` = 2 × RTT + 40, a reorder buffer of at
least 25 ms (more than the profile's slot), and `bandwidth` = 1.5 × the multiplex.

**moqx v0.3.7** runs from its release image:

- configuration: `MOQX_CC` (the `relay_cc` sweep, default `bbr2`), 16/256 send and receive packets
  per loop, a 32 MB UDP buffer, 8 threads, cache on, path MTU ignored, `[18, 16]` versions in that
  order, jemalloc with `MALLOC_CONF=thp:always`;
- every MoQT lane uses draft 18;
- admin counters are sampled every second.

**aiomoqt-relay** replaces moqx with `relay = "aiomoqt"` in a test: `moq_interop_relay --quic` from
the aiomoqt image, with `--cc-algo` from `relay_cc` (default `bbr`). The relay-admin sampler runs
only for moqx. `relay = "none"` takes no `relay_cc`; its runs carry the variant key `relay=none`.

**moq2ts** is built from pinned commits against moqxr v0.4.18, and the receiver against moq5.

- **Carriage.** Transparent (`--transparent`, an alias of `--unmodified`): every source packet is
  forwarded unchanged, null packets included, so the output TS equals the source.
- **Ingest.** `ingest = "stdin"` replaces the SRT ingest with `--video /dev/stdin`.

**aiomoqt** lanes probe the image's tools before a run:

- `pub_media --ts udp://@GROUP:PORT` reads the source group directly; without it, `udp2pipe.py`
  feeds `--ts -`.
- `pub_media` runs with `--reconnect`: a session lost in an outage longer than QUIC's idle timeout is
  redialled and published again, as SRT's caller reconnects.
- The LOC lane matches frames on the sha1 that `sub_media --frame-log` writes; the source tap
  hashes each H.264 frame as the LOC payload aiomoqt's demuxer yields. Without `--frame-log` it
  falls back to `--pipe video` into an H.264 tap matched on slice data.
- The MSFTS lane carries the TS packets unmodified (`--msfts`, 7 per object), and `sub_media
  --ts-out` writes them on arrival to the common playout and taps. It reports PENDING until the
  image has `--ts udp://`, `--msfts` and `--ts-out`.
- Each `sub_media` also writes its own `--analyze` report (and, for MSFTS, `--arrival-log`), kept
  as diagnostics.
- Build an image from a branch with `--build-arg AIOMOQT=git+https://github.com/<owner>/aiomoqt@<branch>`.
- MoQT publishers run with `AIOPQUIC_GSO=0`.

**Receivers reconnect.** Each MoQT receiver runs in a loop that reconnects one second after it
exits. Each exit is logged and counted as a reconnection.

**PCR-locked playout** (`remote/contrib/playout.py`) is the MoQT receivers' output stage:

- Each packet gets a media time from the PCR.
- The clock anchors on the first PCR, then once more 3 s later at the live edge, so a relay's cached
  join burst plays out first.
- Each buffer in `playout_ms` (default 300 and 200 ms) is its own output group. A raw output
  forwards packets on arrival.

## Variants and budgets

- **Budgets.** D = each of `rtt_multiples` (2, 3, 4, 6) × max(RTT, `rtt_floor_ms` = 20), plus
  `budgets_ms` (200, 300).
- **SRT and RIST** run once per D, with SRT latency and RIST buffer set to D.
- **MoQT lanes** run once per congestion controller on the impaired hop: `relay_cc` for the relay
  (moqx: bbr2, bbr, cubic; aiomoqt-relay: picoquic's names) in `last-mile`, `pub_cc` for the
  aiomoqt publisher (bbr, bbr1, cubic, c4) in `first-mile` and in the dual-path lane. A test that
  sweeps a controller no lane runs on the impaired hop is rejected. The MSFTS lane also runs once
  per `pub_timing` (`none`, or `mapping` for `--timestamp-mapping`). Each run is scored at every D
  from the raw arrival times.
- **Filters.** `--variant D=4xRTT`, `--variant relay_cc=bbr2`, `--lane` and `--profile` narrow a
  suite.

## Measurement

**Taps** (`tstap.py`) take the kernel receive time (`SO_TIMESTAMPNS`) of every datagram, moved onto
CLOCK_MONOTONIC so that a wall-clock step never reaches a delay. They log:

- every PCR, with the non-null packet count so far;
- every video PES: PTS, key frame, first and last datagram, and whether a continuity error broke
  it;
- output gaps, and per-second counts.

The input tap sits on the source group in `src`; output taps sit on the output groups in `dst`.

| metric | definition |
|---|---|
| end-to-end delay | each PCR's output time minus its input time, matched PCR by PCR; min, p50, p99, p99.9, max |
| frame delay | per video PES matched on PTS (on a slice-data hash for the LOC lane): first byte in to first byte out, key frames and others apart, and to the last byte out |
| PCR lost, programme lost | PCRs of the window never output; × the PCR interval |
| residual TS loss | 1 − output/input non-null packets between the first and last PCR both taps saw |
| output breaks | gaps ≥ `break_ms` (200) at an output, on each playout buffer and offline from raw arrivals; and the longest stretch without one (the soak headline) |
| late or lost at D | units missing, or with delay above the live-edge delay + D |
| wire bytes, goodput | bytes each way on the WAN over the scored seconds (nft, before any qdisc) against the source's; on-time source bytes per forward wire byte at each D (`wire` in `summary.json`; the run line prints both ratios) |
| applied impairment | netem, cap, AQM and outage drops per direction, and cross-traffic packets; the events as applied, with host times |
| cross traffic | iperf3 JSON per flow from the client (`cross-<event>.json`: per-second rate, retransmissions, cwnd, RTT) and the server |
| relay | moqx admin counters, first to last sample (sent, losses, retransmissions, PTO, cwnd-blocked, pacer lag, RTT) |
| publisher | moq2ts objects and SRT ingest loss and drops; aiomoqt `--stats-json`; SRT and RIST JSON statistics |
| receiver | playout buffer depth against target, late packets, reconnections, socket drops (`ss -uamn`) |
| analyser | `tsp -I ip <out> -P continuity -P pcrverify -O drop` on the main output |
| clock | 1 ms timer probe in `src` and `dst`: pauses, and wall-clock steps |

- **Window.** The measurement window opens `join_s` after the first source datagram, for `seconds`.
- **Clock steps.** Taps, the playout and the calibration probe run on CLOCK_MONOTONIC. The LOC
  lane matches against `sub_media`'s wall-clock frame log, so its source tap stamps wall-clock time
  too; such a run is marked `clock_disturbed` when the clock probe sees a wall-clock step above
  20 ms, and its delay figures are then unreliable.
- **Verdicts.** `lan` must read zero errors: nothing missing, no continuity errors, no breaks. Any run
  that delivers nothing fails.

**Captures.** `capture` in a test records what a late frame needs explained:

| `capture` | records |
|---|---|
| `none` (default) | nothing more |
| `headers` | tcpdump on the WAN-facing interface in `src` and `dst` (`src0.pcap`, `dst0.pcap`), 96 bytes per packet, cross traffic left out; picoquic qlog from every aiomoqt endpoint (`qlog-<process>/`, through `AIOPQUIC_QLOG_DIR`); the bottleneck's qdiscs every 100 ms (`qdisc-wan.jsonl`) |
| `full` | as `headers`, with whole packets, and each aiomoqt endpoint's TLS keys (`keys-<process>.log`) so the QUIC captures can be decrypted |

SRT runs unencrypted, so its headers in the capture carry sequence numbers and retransmission flags;
its per-interval statistics are recorded on every run. moqx writes no qlog. qlog records every
packet, which costs the endpoints some CPU. Captures and qlogs are gzipped when the run is collected;
at 20 Mb/s a 5-minute `headers` run keeps a few tens of MB per interface and per qlog, and `full`
adds about 750 MB per interface.

**Explaining a run.** `moqbench contrib explain <results or run folder> ...` finds, at each D, the
units late or lost exactly as the score counts them, groups consecutive ones into stretches, and
gives each stretch the first cause that fits, with the numbers that decided it:

| cause | means | needs |
|---|---|---|
| `outage` | the stretch overlaps an outage or the recovery after it (at least 1 s, or 10 RTTs) | `faults.jsonl` and a clock anchor |
| `sender-below-media-rate` | the sender's WAN egress fell behind the source; its backlog covers half the stretch's median excess delay | `src0.pcap`, or a covering qlog; qlog adds cwnd, bytes in flight and pacing, SRT stats its send buffer |
| `queueing` | one-way WAN delay above its floor covers half that excess | both pcaps (packets matched on their captured bytes), or `qdisc-wan.jsonl` |
| `repair` | WAN losses in the stretch or just before it, with their repair latency | SRT: both pcaps; QUIC: a covering qlog, else both pcaps (losses only) |
| `head-of-line` | MoQT units whose own data was not lost waited behind a repaired loss in their stream | a covering qlog of the WAN sender |
| `unexplained` | nothing fits; lists the causes whose inputs were absent | — |

- **Clocks.** Taps and qlog run on CLOCK_MONOTONIC; pcaps, faults, qdisc and SRT stats on
  CLOCK_REALTIME. The offset comes from the sender's first Initial in qlog and the src pcap, SRT's
  PCRs in the src pcap, or sub_media's arrival log, corrected by the clock probe's steps. Without
  them, the source start time gives a coarse offset that widens outage windows by 0.5 s.
- **Output.** Each run gets `explain.json`: stretches with start, end, units, D, cause and
  evidence, and totals by cause per D, checked against `summary.json`. Each results folder gets
  `explain.md`: per profile, late-or-lost % by arm and D with the share by cause, and, at each D
  where the SRT run at latency D has fewer late units than a MoQT arm, the difference and the
  causes behind it.

## Calibration

`moqbench contrib calibrate` runs a sequenced UDP probe at the stream's packet rate through each
profile (events off) and echoes it back. It reports, against what the profile commands:

- forward and reverse loss, with the expected spread of the Gilbert-Elliott chain over that many
  packets;
- one-way delay p1, p50 and p99;
- RTT;
- reordering, as packets and as events.

It also records the host's socket buffer limits. `net.core.rmem_max` is global, not per namespace;
below 8 MB it is raised with `sudo -n sysctl`, and the value before and after is recorded.

## Scenarios

| scenario | length | join | source |
|---|---|---|---|
| `standard` | 300 s | 10 s | fixture |
| `dev` | 60 s | 10 s | fixture |
| `soak` | 4 h (set `seconds`) | 10 s | live encode; the headline is hours without an output break |

## VM deployments

An `aws` or `oci` deployment with `src` and `dst` nodes is accepted. `render-vm` prints the commands
it would run, which impair each direction on its receiver's ingress through an ifb. Runs are not
enabled.

## Limits

- One host shares its CPUs among the endpoints, the emulator and the relays; pin each namespace with
  `cpus`.
- Laptop numbers are indicative: Docker Desktop and WSL add a VM layer, its timer behaviour and its
  clock steps.
- The LOC lane carries H.264, so it cannot carry the MPEG-2 tiers; it is graded frame by frame.
