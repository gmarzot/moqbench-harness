# Relay fabrics

A fabric is several relays peered into one mesh and tested as one system: per-hop
latency, how fast names and subscriptions cross it, and what live delivery does
while peers change or links degrade. A fabric runs on one Docker host, or with
one relay per VM in a cloud testbed ([VM fabrics](#vm-fabrics)). Adapters
exist for moqx with moq-fabric, for LAPS and for aiomoqt-relay; each relay node
picks its own, so one fabric can mix them ([Mixed fabrics](#mixed-fabrics),
[aiomoqt-relay as a via](#aiomoqt-relay-as-a-via)).

## Quick start

```bash
./bin/moqx-peering-build.sh <deployment> [ref]          # moqx-peering:<sha8> and :latest on the Docker host
./bin/moqbench fabric up <deployment>                     # render, start, wait until every link is connected
./bin/moqbench fabric state <deployment>                  # each relay's peer sessions
./bin/moqbench fabric run <deployment> example-fabric     # the example suite; results/fabric/<deployment>-<stamp>/
./bin/moqbench fabric down <deployment>
```

`deployments/docker-fabric-example.toml` runs on the local Docker. To drive a
Docker host over ssh, copy it to `deployments/<name>.local.toml` (ignored by git)
and set `host = "<user>@<address>"`. The harness needs Python 3.11 or newer
locally, and openssl; the Docker host needs Docker with Compose.

## Model

A docker deployment lists relay nodes and client nodes; its `[fabric]` table says
how the relays peer:

| key | default | meaning |
|---|---|---|
| `adapter` | `moqx-peering` | the relay and its interconnect, for relay nodes that name none (`moqx-peering`, `laps`, `aiomoqt-relay`) |
| `image` | `moqx-peering:latest` for moqx, `peer_image` for aiomoqt-relay | relay image, for relay nodes of that adapter that name none |
| `topology` | `line` | `line`, `star`, `ring`, `full` or `custom` (with `links`) |
| `order` | relay nodes in file order | the first node is the ingest relay, the last the egress relay |
| `hub` | first in order | the centre of a star |
| `wire` | `full` | `full` gives every relay pair a link network, so links can be added at runtime; `topology` only the topology's |
| `subnet` | `10.77.0.0/16` | the access network is its first /24, then one /24 per link |
| `threads`, `check_interval_ms`, `draft` | `2`, `5000`, `18` | relay IO threads, peer reconnect interval, client MoQT draft |
| `dial` | none | `[dialer, listener]` pairs that override which end dials a link |
| `metrics` | none | `[node, peer, metric]`: the session metric a dialing moqx relay starts with on that link; a dialing aiomoqt-relay gets it through peer.metric once the links are up |
| `peer_pip`, `peer_command`, `peer_image` | see [Mixed fabrics](#mixed-fabrics) | peer nodes |
| `aiomoqt`, `aiopquic` | releases | client versions in the lab image (`build/lab-node.Dockerfile`) |

Each link is dialed from one side only: the node earlier in `order` dials,
unless a `dial` entry says otherwise. Nodes take an optional `fabric_role`
(`edge`, `via`, `stub`) and `cpus` (a cpuset); relay nodes also take `adapter`,
`image`, `admin_image` and `node_metric` (the node metric a moqx or aiomoqt-relay
node advertises, default 100).

On the Docker host each relay is two containers, as a pod is. The holder owns
the relay's addresses: one on the access network, one per link network. The
relay runs in the holder's network namespace, so its addresses, qdiscs and peer
links survive killing and restarting the relay. Every link is its own bridge
network, so an impairment on a link interface affects exactly that link and no
client traffic. All networks are internal. The relay's control API listens on
the namespace's loopback; the harness reaches it with `docker exec` through the
holder. Clients reach relays on the access network only.

A per-testbed CA and one EC P-256 leaf per relay are made with openssl under the
ignored `.testbed/fabric/<deployment>/certs/`. Each relay's certificate directory
is writable by the relay.

For moqx, every listener offers drafts 16 and 18, the cache is on, the admin
server binds 127.0.0.1:8000, peering listens on UDP 33434, `node_id` is the node
name, and MoQ Cluster is not configured.

## Scenarios

A fabric suite test names a scenario in `scenarios/fabric/` and a topology:

```toml
[[test]]
type     = "fabric"
scenario = "peer-churn"
topology = "line"
seconds  = 165
```

Test keys: `object_size`, `rate`, `group_size` (default 1000 B, 30/s, 30 per
group), `draft`, `peering_poll_s`, `slo_ms`, `links` (custom), `suffix`, `note`.

| scenario | measures | verdicts |
|---|---|---|
| `steady` | per-hop latency, jitter and loss: one publisher on the ingest relay, one reference subscriber on every relay | every subscriber gets every object once |
| `propagation` | a PUBLISH_NAMESPACE reaching every moqx relay's `/state`, polled every 2 ms, and every other relay's namespaces, sampled once at the end of the settle window; then subscribers nearest first, cold then warm | the namespace reaches every relay; the publisher's relay delivers every object once; remote subscribers of a moqx publisher are XFAIL (see `expect_remote`) |
| `subscribe-setup` | connect to SUBSCRIBE_OK and connect to first object, for a PUBLISH-announced track, farthest relay first, cold then warm | each subscriber gets every object once |
| `peer-churn` | gap, loss, duplicates, recovery and latency before and after each peer change during live delivery | per event: `hitless` (no loss, no duplicates) or `group-loss` (at most one group lost, no duplicates) |
| `egress-fanout` | a load_sim audience on the egress relay beside the reference subscribers; each link's send rate | the audience holds the gate; each link carrying the track carries it once (0.5–1.6 times its payload rate) |
| `edge-scale` | one load_sim audience per relay | every audience holds the gate on its own |
| `late-join` | an MSF publisher (pub_media: catalog and audio), then one late `sub_media --analyze` per relay: time to catalog and to each track's first frame | catalog received, every track delivered without duplicates |
| `metric-flip` | path changes through each relay's own control API: onto the direct link at a group's end and mid-group, then by the transit relay, each followed by a change back | per event: `hitless`; each event is labelled with where in its group the publisher was |
| `datagram-transit-origin` | datagrams published on the transit relay, a subscriber on every relay | every subscriber gets every datagram once |
| `datagram-through-transit` | datagrams from the ingest relay while the transit relay has a local subscriber | every subscriber, the egress relay's included, gets every datagram once |
| `datagram-through-transit-control` | the same with no subscriber on the transit relay | as above |
| `via-sub-churn`, `via-sub-churn-reverse` | four named subscribers joining and leaving, staggered and overlapping, past a via | each is complete from its first object; after the last leaves the via carries nothing and holds no subscribe |
| `via-local-sub` | a subscriber on the via joining and leaving while one past it runs throughout | hitless for the far subscriber at both transitions; the via's subscriber complete; afterwards the via holds no subscribe of its own and still carries the track |
| `via-edge-sub-kill` | SIGKILL of the subscriber past the via, then of the relay past it | at both, `no-gap` for the via's subscriber |
| `via-pair-failover`, `via-pair-failover-reverse`, `via-pair-failover-kill` | the cheaper of two vias on a ring stopped (or killed), then started | only the cheaper via carries the track before and after; the other carries it meanwhile; `group-loss` on the stop, `bounded-gap` on the kill |
| `relay-restart`, `relay-restart-kill` | the egress, transit and ingest relay stopped (or killed) and started again in turn, a reference subscriber on every relay | per event and subscriber: outage, loss and `recovers` (delivery resumes without the client reconnecting); a subscriber on the restarted relay, and every subscriber when the publisher's relay restarts, is SKIP |
| `namespace-tuples` | two publishers on the ingest relay whose namespaces concatenate alike (`<ns>/a/bc`, `<ns>/ab/c`, object sizes 300 B apart), then a subscriber to each on every relay | each subscriber receives only its own track (by object size) |
| `fetch-peer` | a joining FETCH (aiomoqt's join example) on the publisher's relay and on every other relay | FETCH_OK on the publisher's relay; across peering, `expect_across = "fail"` reports a refusal as XFAIL and a success as XPASS |

The gate for an audience: over its steady samples (from the first at the full
population to three before the end), at least 95% have the full population,
at least 90% of the expected receive rate and a p90 at or under the test's
`slo_ms` (default 500), and load_sim exits 0.

Scenario keys: `mode` (`stream`, `setup`, `media`, `fetch` or `tuples`), `publisher`, `subscribers`
(`["all"]` or node names and roles), `pub_ns`, `sub_start_s`, `peering_poll_s`,
`state_poll_ms`, `netdev_s`, `settle_s`, `gap_s`, `warm_s`, `cold_order`,
`namespace_check`, `datagram` (the publisher sends ObjectDatagrams),
`audio_frames_per_group` (media), `fetch_history` and `expect_across` (fetch),
`[[event]]` tables (an event may carry a `label` for the report), and
`[[population]]` tables (`edge`: a node, a role or `each`; `subs`;
`subs_per_proc`) for load_sim audiences on the load hosts.

Stream scenarios may also name subscribers and path expectations:

- `[[subscriber]]`: `name`, `on` (a node or role) and `expect` (`complete`,
  the default, or `none`). `sub.*` events start and end it; it runs as
  `sub-<name>`. Its loss is counted over the time it ran: `complete` passes
  when it received every object from its first to its last, once. Events are
  analysed for a named subscriber only while it runs.
- `[[expect_path]]`: `node`, a window `from_s`-`to_s` (seconds from the
  publisher's start), `label`, and one or more of `carries` (the relay sends the
  track to a peer at half its rate or more), `subscribed` (it holds a fabric
  subscribe for the track, its own or one it forwards) and `own_subscribe` (its
  own, for local viewers), each `true` or `false`. They are read from the
  relay's polled peering state through its adapter (`Adapter.sample`): bytes
  sent to peers for aiomoqt-relay, data streams open on peer sessions for moqx.
  Where the state cannot tell (LAPS, whose state is not polled), `carries` comes
  from the relay's link send rates; a state nothing reports is SKIP.

`[expect_remote]` marks a known defect: when the publisher's relay runs
`publisher_adapter`, subscribers on every other relay are expected to fail
(`expect = "fail"`, with a `defect` description). They report XFAIL, or XPASS
once the defect is fixed; neither counts as a pass, and XPASS fails the run so
the expectation gets updated.

## Faults

An event is `at_s` seconds from the publisher's start, a verb, and nodes by name
or role (`ingest`, `egress`, `transit`):

| verb | arguments | does |
|---|---|---|
| `peer.add`, `peer.remove`, `peer.restart` | `on`, `peer` | through the adapter, from `on` toward `peer` |
| `peer.metric` | `on`, `peer`, `metric` | the session metric `on` uses for `peer` |
| `link.impair` | `on`, `peer`, `netem`, `direction` | `tc netem` on the link's interfaces: both ends, or only `on`'s or `peer`'s sending side |
| `link.down`, `link.clear` | `on`, `peer`, `direction` | 100% loss on the link; remove its impairment |
| `node.kill`, `node.stop`, `node.start` | `on`, `signal`, `grace_s` | the relay container; its namespace stays |
| `node.freeze`, `node.thaw` | `on` | pause and resume the relay's cgroup |
| `sub.start` | `sub` | start a named subscriber, until the run's end |
| `sub.stop`, `sub.kill` | `sub` | SIGINT (it ends and prints its summary) or SIGKILL to a named subscriber |

The same verbs are commands: `moqbench fabric peer|link|node <deployment> ...`.
Each applied event records host-clock times bracketing it in `faults.jsonl`. An
event `expect` sets that event's verdict: `hitless` (no loss, no duplicates),
`group-loss` (at most one group lost, no duplicates), `no-gap` (hitless, and no
inter-arrival gap over `max_gap_ms`, by default twice the object interval) or
`bounded-gap` (delivery resumed within `max_gap_ms`, which it needs) or
`recovers` (delivery resumed without a reconnect, for each subscriber that was
receiving; on a node event, a subscriber of the restarted relay or of the
publisher's relay is SKIP). `subs` limits the verdict to the named subscribers
it lists. Delivery that stops for good while the publisher still sends has no
recovery.
`moqbench fabric check` lists impairments left on any relay namespace and
`--clear` removes them; every run checks before it ends and clears what it finds.

## Measurement

Every publisher and reference subscriber runs in the one client container, so
latency needs no clock synchronization. Each client line is prefixed with the
host time it was read (`remote/fabric/tsline.py`). The tools run with
`--log data=trace`, which logs every object sent and received with its group and
object IDs.

- **Latency**: the subscriber tool's own figures from the object timestamps
  (p50/p95/p99, jitter), and per-object send-to-arrival times from the trace
  lines (p50/p90/p99). The trace figures include the logging path on both sides.
- **Loss and duplicates**: from the trace lines. Missing objects are those the
  publisher sent between a subscriber's first and last arrival that it never
  received; a duplicate is a repeated group and object ID.
- **Events**: for each event, the largest inter-arrival gap in the following ten
  seconds (or until the next event), including an outage that spans the event or
  outlasts the window; the objects sent in that span and never received, with
  their IDs; duplicates; recovery (event start to the end of a gap longer than
  three object intervals, empty if delivery never resumed); median latency
  before and after; and the delivery path before and after.
- **Path**: from interface counters. Every link is its own interface, so each
  relay's send rate on each link is exact; the links carrying at least half the
  track's rate, followed back from a relay, give its paths from the ingest relay.
  More than one path into a relay means it receives the track more than once.
  The summary also reports each directed link's mean send rate.
- **Setup**: connect to SUBSCRIBE_OK and connect to first object, from the
  subscriber's own output.

A run folder holds the client logs, `faults.jsonl`, the pollers' JSONL (a body is
stored only when it changed; `poll-net-*` holds interface counters), each relay's log and final `/peering` and
`/state`, the rendered stack, `meta.json` (images, versions, addresses) and
`summary.json` from `./bin/fabric-report.py`.

Polling a relay's control API is not free and can change what it measures:
`peering_poll_s` sets the rate, and the steady scenario polls every 5 s.

## VM fabrics

An aws or oci deployment with a `[fabric]` table runs one relay per VM. Its relay
nodes, which share one shape, become `relay_count` relays; the first probe is
the reference host (the publisher, every reference subscriber and the local
time source), and the load nodes run the audiences. `deployments/oci-fabric-example.toml`
is three relays, two load hosts and a reference host.

```bash
./bin/moqbench up <deployment>                             # Terraform: relays, load hosts, reference host
AIOMOQT_VERSION=0.15.0rc1 TOOLS=aiomoqt RELAYS=none ./bin/testbed-provision.sh
./bin/testbed-tune.sh                                      # every relay, client and probe
./bin/testbed-chrony.sh                                    # the reference host as everyone's time source
./bin/bw-baseline.sh                                       # every node to relay 1, then every relay pair
./bin/moqx-peering-build.sh <deployment> peering/moq-peering   # on the first relay host
./bin/moqbench fabric deploy <deployment>                  # the image to the other relays
./bin/moqbench fabric run <deployment> example-fabric-vm
```

Relays run the adapter's image with host networking and listen on their private
addresses; peering runs over the same addresses. `moqbench fabric deploy` copies
the image from the first relay host that has it, or from `--from <ssh target>`,
through the machine running the harness.

**Impairments** never touch the NIC's tuned qdiscs. A flower filter on the
peer's address, on the NIC's clsact egress hook (`side = "send"`, the default)
or ingress hook (`side = "receive"`), redirects that peer's packets through an
ifb device (`mbo<k>` or `mbi<k>`) whose root is netem. `direction` picks which
end's traffic is impaired, as on Docker. `moqbench fabric check` lists ifb
devices, mirred filters and netem qdiscs left on any relay; `--clear` removes
them.

**Link rates** come from an nft table on each relay that counts bytes to and
from every other relay.

**Telemetry**: `remote/load-monitor.py` runs on every host for the run. On a
relay it also samples `/peering` every `peering_poll_s` and the nft counters
every sample; the namespace check polls `/state` beside each relay as on Docker.

**Clock**: `./bin/testbed-chrony.sh` makes the reference host a chrony server
disciplined by the provider's link-local time service, and every other host its
client. Every run records `chronyc tracking` on every host at its start and end,
and the summary reports each host's offset and a budget of twice the worst one.
Reference subscribers share the publisher's clock and need none of it; audience
latency on the load hosts does.

## Mixed fabrics

Relays of different implementations peer when they speak the same interconnect;
moqx and LAPS both use moq-fabric. `deployments/docker-mixed-fabric-example.toml`
is a triangle of moqx-a, LAPS and moqx-b on the local Docker, with an aiomoqt peer
node, and `suites/example-mixed-fabric.toml` runs steady delivery, propagation
and a path change through each relay's control API (`metric-flip`).

```bash
./bin/moqx-peering-build.sh docker-mixed-fabric-example
./bin/laps-build.sh docker-mixed-fabric-example [ref]      # laps:<sha8> and laps:builder-<sha8>, and :latest
./bin/moqbench fabric run docker-mixed-fabric-example example-mixed-fabric
```

**LAPS** runs `lapsRelay` with its peers on the command line: `-b 0.0.0.0` (it
binds loopback otherwise), `-p 33435` for clients, `-e <node>` as its peering
label, `--peer_port 33434`, one `--peer` per link it dials, and the node's
certificate. Its clients speak MoQT draft 18 only, so a run refuses any other
draft when a fabric has a LAPS relay. It has no peer-metric flag (a `metrics`
entry on a link LAPS dials is refused; use a `peer.metric` event), checks its
peers every 5 s, and ignores SIGTERM, so its container stops after 2 s. Control
is lapsAdmin (JSON-RPC over MoQ) in a sidecar container in the relay's namespace,
from `admin_image`, by default the image's builder tag (`laps:X` -> `laps:builder-X`),
which carries lapsAdmin. Every call is MoQ traffic that floods the fabric, so the
harness reads LAPS state only to change a peer, once at the end of propagation's
settle window and at the end of a run, checks readiness by its listening port,
and checks a link from its moqx end when it has one. `bin/laps-build.sh` builds
both images from Quicr/laps at a ref, with local patches from the ignored
`patches/local/laps/`; a patched build is tagged `<sha8>-p<count>`. The
datagram scenarios show the difference between a LAPS image with and without a
fix: give the LAPS node the image to test.

**Peer nodes** (`roles = ["peer"]`) join the fabric as aiomoqt peering nodes, not
relays. Each runs `peer_command` in its own container on the access network,
from `peer_image` (`build/fabric-peer.Dockerfile`: the lab image plus `peer_pip`,
by default aiomoqt's 0.15.0 dev branch). The image is built once per `peer_pip`;
remove it to rebuild from a branch that moved. The command is a template:

| field | becomes |
|---|---|
| `{peers}` | `--peer <address>:33434` for each relay in the node's `dials` (default `["ingest"]`) |
| `{node}` | the node's name |
| `{seconds}` | the run's length |
| `{output}` | the JSON report path in the run's results |
| `{namespace}`, `{track}` | the run's track |

The default is `moq-fabric-probe {peers} -p 0 --node-id {node} -t {seconds} -i 5 -o {output}`;
a node's own `command` replaces it, and its own `image` replaces `peer_image`.
`moq-fabric-bench` fits the same template, for example
`moq-fabric-bench {peers} --node-id {node} --role sub -N {namespace} -T {track} -t {seconds} -o {output}`
receives the run's track over peering. The summary reports what each peer node
learned: the relays it saw, how many announces and subscribes passed the hash
check, and whether it stayed a leaf (it sent no NODE_INFO and no learned path
runs through it). For moq-fabric-bench it does so per end (`pub`, `sub`), and adds
each received track's objects, loss, duplicates, payload check, latency
(p50/p90/p99) and header-to-first-object time, and each published track's
object count; a received track is checked for every object once. Peer nodes run
on Docker fabrics only.

## aiomoqt-relay as a via

`deployments/docker-via-example.toml` is a ring of LAPS, two aiomoqt-relay vias
and moqx: `laps`, `via-1` (node metric 100), `moqx`, `via-2` (200).
`suites/example-via.toml` runs the via scenarios on it: tests 1-4 peer only the
line `laps - via-1 - moqx`, tests 5-7 the ring. The controls run the same suite
with moqx as both vias (`docker-via-moqx-example`) and with LAPS
(`docker-via-laps-example`, tests 1-4 only: LAPS takes no node metric), so a
loss is attributed to the via or to the ends.

```bash
./bin/moqx-peering-build.sh docker-via-example
./bin/laps-build.sh docker-via-example
./bin/moqbench fabric run docker-via-example example-via
./bin/moqbench fabric run docker-via-moqx-example example-via
./bin/moqbench fabric run docker-via-laps-example example-via --only 1 --only 2 --only 3 --only 4
```

An aiomoqt-relay node runs from `peer_image` unless it names an `image`:

    aiomoqt-relay --quic --bind 0.0.0.0 --port 4433 --cert /certs/relay.pem --key /certs/relay.key
      --control /fabric/run/control.sock --pause-after 5 --node-id <node> --node-metric <node_metric>
      --peer-port 33434 [--peer <address>:33434 ...]

with one `--peer` per link it dials. `--pause-after 5` withdraws a track's
fabric subscribe 5 s after its last local viewer leaves. The control socket's
folder (`nodes/<node>/run`) is mounted in the relay and in its holder, so the
harness and the pollers reach it from the holder across relay restarts:
`GET /moq-fabric` gives peer state, namespaces and the samples path checks read;
`POST /moq-fabric/peer?op=add|remove|restart|metric&host=H&port=P[&metric=M]`
changes a peer, addressed by the session's host and port as `GET /moq-fabric`
reports them. `op=restart` closes the session and dials again; a relay that
answers 400 to it has no restart, and `peer.restart` is SKIP. aiomoqt-relay
nodes run on Docker fabrics only.

## Adapters

`lib/fabric/adapter.py` is the relay-neutral interface: render a node's config
and container (and a control sidecar when it needs one), report readiness, add,
remove, restart and re-price a peer, and read peer state, namespaces, raw state
and metrics. A verb the relay lacks raises `Unsupported` and the event is
reported as skipped. `lib/fabric/moqx_peering.py` implements it for moqx,
`lib/fabric/laps.py` for LAPS and `lib/fabric/aiomoqt_relay.py` for aiomoqt-relay.

## Limits

One Docker host shares its CPUs, memory and kernel among every relay and client:
results are functional and relative, not capacity. Pin cores with `cpus`.
Further adapters, cross-region fabrics and per-track path visibility are later
phases.
