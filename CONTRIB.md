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
docker build -t moqbench-contrib-aiomoqt:0.15.0rc1 -f build/contrib-aiomoqt.Dockerfile build
docker pull ghcr.io/openmoq/moqx:v0.3.7-amd64

./bin/moqbench contrib up docker-contrib-example          # namespaces, veths, routes; prints the checks
./bin/moqbench contrib calibrate docker-contrib-example   # what the emulator applies, per profile
./bin/moqbench contrib run docker-contrib-example example-contrib --scenario dev \
    --lane srt --lane moqt-moq2ts --profile REF --variant D=4xRTT --variant relay_cc=bbr2 --repeats 1
./bin/moqbench contrib report results/contrib/<run>       # re-analyze run folders
```

Results land in `results/contrib/<deployment>-<stamp>/<NNN>-<lane>-<profile>-<variant>-r<repeat>/`,
with `suite.json` and `host.json` beside them. `results/` is ignored by git.

## Topology

```
 src ──wa0/va0── wan ──vb0/wb0── dst          one host, one CLOCK_REALTIME
  │  239.79.0.1:5000                 │  239.79.0.2:6000.. (outputs)
  tsp regulate → senders, input tap  receivers → playout → output taps, analyser
```

- Three network namespaces, each held by a container (`mbc-<deployment>-src|wan|dst`), joined by
  plain veth pairs: no bridge, NAT or conntrack. `wan` routes between the two /24s.
- Segmentation offloads are off on every veth, and RPS steers each veth's receive queue to its
  namespace's CPUs. Without RPS a sender that migrates between CPUs reorders its own packets.
- Each lane process runs in its own container inside one namespace. Its signals reach every process
  of its pipeline, so files flush on stop. Every container carries the label
  `org.moqbench.contrib=<deployment>`, and `down` removes only those.
- Sources fan out on a multicast group over a dummy interface in `src`; outputs do the same in `dst`.
- `dual_path = true` adds a second, independent pair of links (`va1`/`vb1`).

**Placement.** MoQT lanes cross the WAN on one hop of the two:

- `last-mile` (default): publisher and relay in `src`, the subscriber in `dst`. The relay's delivery
  to the receiver is impaired.
- `first-mile`: the relay in `dst`. The publisher's delivery to the relay is impaired.

SRT and RIST are one hop either way.

## Impairment

All of it lives in `wan`, so no endpoint or relay socket is charged for queued packets. Each
direction has a fixed tree built once per run, on its egress interface:

```
root 1:  netem limit 100000 delay D [slot MIN MAX] rate 1gbit [loss gemodel p r 1-h 1-k] seed S
10:      netem delay 1ms reorder P%       (when the profile reorders)
20:      tbf rate CAP burst 64kb latency 60ms   (CAP = cap x the stream's Ethernet rate)
```

- **Jitter** uses `slot`. `rate 1gbit` keeps the delay line FIFO, so jitter does not reorder;
  reordering is a separate, small stage.
- **Events** change the tree in place (`tc qdisc change`, restating every netem parameter). Outages
  add the direction's ingress interface to an nft set that one counted drop rule matches.
- **Counters.** nft accounting counters count every packet per direction before any qdisc. Each run
  records them every second, with every qdisc's packets and drops.
- **Reported loss** is the loss applied, not the loss commanded: netem drops, cap drops and outage
  drops are reported apart.
- **Seeds.** The two directions have different seeds; every lane of a repeat sees the same loss
  pattern.

Profiles are `profiles/contrib/<ID>.toml`:

| ID | profile | RTT | jitter (slot) | forward loss | cap | events |
|---|---|---|---|---|---|---|
| P0 | control LAN | 1 ms | — | — | 2.0× | — |
| REF | reference WAN | 50 ms | — | 0.5 % random | — | — |
| P1 | clean regional | 20 ms | 0–2 ms | GE 0.02 % | 2.0× | — |
| P2 | managed national | 60 ms | 0–6 ms | GE 0.1 % | 1.5× | 200 ms loss run, 64 ms link fail |
| P3 | unmanaged transcontinental | 80 ms | 0–16 ms | GE 0.5 % | 1.5× | 256 ms outage, 8 ms route step |
| P4 | intercontinental, good | 100 ms | 0–10 ms | GE 0.3 % | 1.5× | 128 ms outage, 4 ms step |
| P5 | long-haul unmanaged | 250 ms | 0–20 ms | GE 1 % | 1.3× | 400 ms outage, 16 ms step |
| P6 | bad intercontinental | 400 ms | 0–50 ms | GE 3 % | 1.2× | 1.6 s outage and 64 ms step every 2 min; 0.5 s loss runs |
| P7 | cellular uplink | 60 ms | 0–20 ms | GE 2 % | 1.5× | handover gap and RTT rise; rate dip to 0.8× |
| P8 | LEO satellite | 60 ms | 0–14 ms | GE 0.4 % | 1.5× | 45 ms burst and RTT step every 15 s |
| P9 | outage ladder | P3 | P3 | P3 | 1.5× | 0.5, 2, 5, 30 s: forward only, then both |

- **Profile keys.** `forward`, `reverse`: `delay_ms` (default RTT/2), `slot_ms`, `rate`,
  `loss = { gemodel = [...] }` or `{ random = PCT }`, `reorder_pct`, `cap` or `cap_mbit`,
  `cap_latency_ms`, `seed`. `[[event]]` tables have a `kind`:
  - `outage`: `for_ms`, `direction`;
  - `step`: `delta_ms`, `for_s`;
  - `rate`: `factor`, `for_s`.

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
| `moqt-aiomoqt-loc` | `pub_media --ts` (LOC) → moqx | `sub_media` → frame tap, or `--ts-out` → playout |
| `moqt-aiomoqt-msfts` | `pub_media --msfts --msfts-packets 7` → moqx | `sub_media --ts-out` → playout |
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

**moq2ts** is built from pinned commits against moqxr v0.4.18, and the receiver against moq5.

- **Carriage.** Transparent (`--transparent`, an alias of `--unmodified`): every source packet is
  forwarded unchanged, null packets included, so the output TS equals the source.
- **Ingest.** `ingest = "stdin"` replaces the SRT ingest with `--video /dev/stdin`.

**aiomoqt** lanes probe the image's tools before a run:

- `pub_media --ts udp://...` reads the source group directly; without it, `udp2pipe.py` feeds
  `--ts -`.
- `sub_media --ts-out` feeds the playout and the taps. Without it the LOC lane falls back to
  `--pipe video` into an H.264 frame tap.
- The MSFTS lane needs `--msfts` and `--ts-out`, and reports PENDING until the image has them.
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
- **MoQT lanes** run once per congestion controller: `relay_cc` for moqx (bbr2, bbr, cubic), and
  `pub_cc` for the aiomoqt publisher (bbr, bbr1, cubic, c4). Each run is scored at every D from the
  raw arrival times.
- **Filters.** `--variant D=4xRTT`, `--variant relay_cc=bbr2`, `--lane` and `--profile` narrow a
  suite.

## Measurement

**Taps** (`tstap.py`) take the kernel receive time (`SO_TIMESTAMPNS`) of every datagram. They log:

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
| output breaks | gaps ≥ `break_ms` (200) at an output; on each playout buffer, and offline from raw arrivals |
| late or lost at D | units missing, or with delay above the live-edge delay + D |
| overhead | forward and reverse wire bytes (nft, before any qdisc) against the source |
| applied impairment | netem, cap and outage drops per direction; the events as applied, with host times |
| relay | moqx admin counters, first to last sample (sent, losses, retransmissions, PTO, cwnd-blocked, pacer lag, RTT) |
| publisher | moq2ts objects and SRT ingest loss and drops; aiomoqt `--stats-json`; SRT and RIST JSON statistics |
| receiver | playout buffer depth against target, late packets, reconnections, socket drops (`ss -uamn`) |
| analyser | `tsp -I ip <out> -P continuity -P pcrverify -O drop` on the main output |
| clock | 1 ms timer probe in `src` and `dst`: pauses, and wall-clock steps |

- **Window.** The measurement window opens `join_s` after the first source datagram, for `seconds`.
- **Clock steps.** A backward wall-clock step that the probe sees and a tap's own sequence shows is
  taken out of that tap's timeline. A step only the probe sees is the monotonic clock jumping (a host
  pause); the lanes live through it, so it stays in.
- **Verdicts.** P0 must read zero errors: nothing missing, no continuity errors, no breaks. Any run
  that delivers nothing fails.

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
