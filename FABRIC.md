# Relay fabrics

A fabric is several relays peered into one mesh and tested as one system: per-hop
latency, how fast names and subscriptions cross it, and what live delivery does
while peers change or links degrade. Phase 0 runs the whole fabric on one Docker
host. The first adapter is moqx with moq-peering.

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
| `adapter` | `moqx-peering` | the relay and its interconnect |
| `image` | `moqx-peering:latest` | relay image |
| `topology` | `line` | `line`, `star`, `ring`, `full` or `custom` (with `links`) |
| `order` | relay nodes in file order | the first node is the ingest relay, the last the egress relay |
| `hub` | first in order | the centre of a star |
| `wire` | `full` | `full` gives every relay pair a link network, so links can be added at runtime; `topology` only the topology's |
| `subnet` | `10.77.0.0/16` | the access network is its first /24, then one /24 per link |
| `threads`, `check_interval_ms`, `draft` | `2`, `5000`, `16` | relay IO threads, peer reconnect interval, client MoQT draft |
| `aiomoqt`, `aiopquic` | releases | client versions in the lab image (`build/lab-node.Dockerfile`) |

Each link is dialed from one side only: the node earlier in `order` dials. Nodes
take an optional `fabric_role` (`edge`, `via`, `stub`) and `cpus` (a cpuset).

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
group), `draft`, `peering_poll_s`, `links` (custom), `suffix`, `note`.

| scenario | measures | verdicts |
|---|---|---|
| `steady` | per-hop latency, jitter and loss: one publisher on the ingest relay, one reference subscriber on every relay | every subscriber gets every object once |
| `propagation` | a PUBLISH_NAMESPACE reaching every relay's `/state`, polled every 2 ms; then subscribers nearest first, cold then warm | the namespace reaches every relay; each subscriber gets every object once |
| `subscribe-setup` | connect to SUBSCRIBE_OK and connect to first object, for a PUBLISH-announced track, farthest relay first, cold then warm | each subscriber gets every object once |
| `peer-churn` | gap, loss, duplicates, recovery and latency before and after each peer change during live delivery | per event: `hitless` (no loss, no duplicates) or `group-loss` (at most one group lost, no duplicates) |

Scenario keys: `mode` (`stream` or `setup`), `publisher`, `subscribers`
(`["all"]` or node names and roles), `pub_ns`, `sub_start_s`, `peering_poll_s`,
`state_poll_ms`, `netdev_s`, `settle_s`, `gap_s`, `warm_s`, `cold_order`,
`namespace_check`, and `[[event]]` tables.

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

The same verbs are commands: `moqbench fabric peer|link|node <deployment> ...`.
Each applied event records host-clock times bracketing it in `faults.jsonl`. An
event `expect` (`hitless`, `group-loss`) sets that event's verdict.
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

## Adapters

`lib/fabric/adapter.py` is the relay-neutral interface: render a node's config
and container, report readiness, add, remove, restart and re-price a peer, and
read peer state, namespaces, raw state and metrics. A verb the relay lacks raises
`Unsupported` and the event is reported as skipped. `lib/fabric/moqx_peering.py`
implements all of it for moqx.

## Limits

One Docker host shares its CPUs, memory and kernel among every relay and client:
results are functional and relative, not capacity. Pin cores with `cpus`. VM
fabrics, other relays' adapters and per-track path visibility are later phases.
