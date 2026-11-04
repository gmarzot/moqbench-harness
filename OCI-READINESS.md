# OCI deployment and benchmark readiness

OCI is one optional cloud backend for the harness. No particular tenancy,
compartment or relay implementation is assumed.

## Before provisioning

- Configure the target tenancy, compartment, region, availability domain, image,
  authentication profile, SSH key and allowed source addresses in an ignored
  `oci/envs/<environment>.local.tfvars` file.
- Verify permissions, current shape availability, quotas, costs and permission
  to generate the planned traffic. Keep those environment-specific findings local.
- Inspect the Terraform plan and its target-identity guard before applying it.
- Confirm the guest architecture and runtime libraries match the relay artifact.

## Before attributing a limit to the relay

Characterize both directions of every client/relay path with the same MTU used
by the benchmark. Read receiver results and loss, not just offered sender rate.
Measure each path separately and all clients concurrently. Keep intra-region and
cross-region tests separate: CPU count and advertised NIC bandwidth do not
establish achievable end-to-end throughput or packets per second.

```bash
# Receiver, on the benchmark port:
iperf3 -s -p 4533
# Sender, using a configured peer address:
iperf3 -c <peer> -p 4533 -u -b 0 -l 1400 -t 15
iperf3 -c <peer> -p 4533 -u -b 0 -l 1400 -t 15 -P 4
iperf3 -c <peer> -p 4533 -t 10
```

Use bin/bw-baseline.sh for the selected inventory. Verify host firewall and cloud
network rules permit both the control and data protocols required by each tool.
Record application, transport, kernel and NIC drop counters with each run.

## Runtime and tuning

Reapply per-device tuning after boot. Verify effective socket-buffer sizes,
open-file limits, MTU, queue/offload settings, CPU affinity and cgroup limits.
A container benchmark must record its network mode and resource constraints;
match these to the intended deployment before comparing it with a native run.
Keep the publisher and client capacity sufficient to expose the relay's limit.

Use a local loopback control to distinguish per-host CPU cost from a network-path
limit. Use an adaptive search to find a candidate load, then a fixed-population
run to check delivery, latency and stability. Capture a short CPU profile when
reproducing a collapse; average CPU alone cannot identify its cause.

## Retaining evidence

Store deployment identifiers, host addresses, quotas and raw results in ignored
local files. Tracked documentation describes reproducible methods and public
upstream behavior.
