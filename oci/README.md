# OCI lab

Terraform and lifecycle for the OCI testbed: one relay (the system under test),
N load generators, and optionally a probe node, on a private VCN. This folder
only creates, sizes and powers the hosts: `./bin/testbed.sh` hands any
environment defined here to `oci/testbed.sh`. That writes the hosts to
`.testbed/inventory.json`, which the commands that run on them
(`testbed-provision.sh`, `testbed-tune.sh`, `bw-baseline.sh`, `relay-*.sh`)
read, so they never depend on OCI.

## Environments

An environment is a definition: where a testbed goes and how big it is. It is
made of two files, applied in this order so the second wins:

| file | tracked | holds |
|---|---|---|
| `envs/<env>.tfvars` | yes | generic shapes, sizing and host counts |
| `envs/<env>.local.tfvars` | no (gitignored) | deployment-specific tenancy/name guard, region, compartment, AD, image, auth profile, SSH key and allowed egress IP |

A tester using their own tenancy needs only the local file.

A deployment is one running instance of an environment, under a name you pick
when you create it:

```bash
./bin/testbed.sh fanout-1 up --env moqbench-testbed      # Terraform shows the plan and asks
./bin/testbed.sh fanout-1 up --env moqbench-testbed -y   # the same, approved without asking
```

The name becomes the deployment's Terraform workspace, the prefix of every
resource it creates (`fanout-1-relay-01`, `fanout-1-vcn`, ...) and its
`deployment` tag. So several deployments can share a compartment, and
Terraform's destroy prompt names exactly the one you made. A deployment
remembers its environment (in `.testbed/deployments/`), so after the first
`up` only the name is needed. For a new deployment without `--env`, the
environment is the one with the deployment's own name, else `TESTBED_ENV`, else
the only one defined. Set `TESTBED_ENV` once in `.env` at the repo root
(gitignored; start from `.env.example`) or in your shell, where it overrides
`.env`.
Always go through `./bin/testbed.sh`, which keeps the workspace and the
environment together; with no arguments it lists both.

| environment | where | relay | load generators | probe |
|---|---|---|---|---|
| `moqbench-testbed` | selected in the ignored local environment file | E5 8 OCPU / 32 GB | 2 x E5 8 OCPU / 32 GB | E5 4 OCPU / 8 GB |

At E5 list price ($0.03 per OCPU-hour, $0.002 per GB-hour) that is about
$1.05/hr for all four hosts. Billing is per second; stop between sessions.

## Credentials

None in this repo, ever. Each tester has a profile in their own
`~/.oci/config`, and the local env file names it:

```hcl
config_file_profile = "my-profile"
auth                = "SecurityToken"   # session login; "APIKey" for an API key
tenancy_ocid        = "ocid1.tenancy..." # the tenancy that profile logs in to
```

`./bin/testbed.sh` uses that same profile, region and auth for its own OCI CLI calls,
so the CLI and Terraform cannot drift apart. With a session profile it checks
the token before every action; tokens last about an hour. Renew with
`oci session refresh --profile <name> --auth security_token`. Settings left out
of both files fall back to `TF_VAR_*` in the shell, so a context helper that
exports those still works.

## Test environment, start to finish

```bash
cp oci/envs/moqbench-testbed.local.tfvars.example oci/envs/moqbench-testbed.local.tfvars
$EDITOR oci/envs/moqbench-testbed.local.tfvars         # profile, target tenancy, compartment, AD, image, SSH key, egress IP

./bin/testbed.sh fanout-1 plan --env moqbench-testbed  # read-only; 10 to add
./bin/testbed.sh fanout-1 up -y                        # create it, no prompt
./bin/testbed-provision.sh && ./bin/testbed-tune.sh && ./bin/relay-smoke.sh
./bin/relay-load.sh adaptive moqx 120                  # see RUNBOOK §5 for the load tools
./bin/testbed.sh fanout-1 stop                         # `start` to resume; disks kept
```

`./bin/testbed.sh <deployment> output` prints the IPs and selects that
deployment for the other commands. `./bin/testbed.sh <deployment> destroy`
deletes everything that deployment created, and nothing else: not other
deployments, not the compartment.

## Adding your own tenancy

```bash
cp oci/envs/my-tenancy.local.tfvars.example oci/envs/<name>.local.tfvars
$EDITOR oci/envs/<name>.local.tfvars   # profile, tenancy, compartment, AD, image, key, IP
./bin/testbed.sh <name> plan          # a deployment named after the environment
```

The template has the commands to look up each value. A compartment of your own
keeps cleanup to `./bin/testbed.sh <name> destroy`. The four-host layout needs 28 E5 cores
of the tenancy's limit. If the environment should be shared, split it the way
`moqbench-testbed` is: generic sizes in a tracked `<name>.tfvars`,
and all deployment identifiers and personal settings in `<name>.local.tfvars`.

## Guards

- **`expected_tenancy_name`** makes Terraform refuse to plan unless the target
  tenancy has that name. That catches a profile pointed at the wrong tenancy
  before anything is created.
- **`allowed_cidrs`** is the only access from outside the VCN (SSH, relay and
  metrics ports). Inside the VCN everything is open, because that is where the
  benchmark traffic flows. When `expected_tenancy_name` is set, a `/0` is
  refused. Update it if your egress changes
  (`curl -s https://checkip.amazonaws.com`).
- **IMDSv1 is off on every instance.** Nothing here uses the legacy metadata
  endpoints; keep them disabled to support environments that require this.

## Environment prerequisites

Check the compartment's IAM permissions and compute limits before provisioning.
Keep tenancy-specific values outside tracked files. Login sessions must remain
valid for lifecycle API calls.

## Why these shapes

- **x86 on purpose.** The relay and moxygen binaries are x86-64 glibc 2.35
  and run unmodified on Ubuntu 22.04. A binary built on a newer distro will
  not load.
- **OCPU = 1 physical core = 2 vCPU** on x86. Graviton has no SMT, so matching
  48 Graviton vCPU takes 48 OCPU, not 24.
- **E5.Flex sells 1 Gbps per OCPU, capped at 40.** Cores buy bandwidth: the 8
  OCPU relay has 8 Gbps. Two 8 OCPU clients receive 16 Gbps, so the relay's
  NIC binds before theirs do.
- **Intra-region traffic is free**, cross-AD included, so long high-rate runs
  cost nothing in transfer.
- **Stop, don't terminate.** Stopping a standard VM halts compute billing; only
  boot volumes remain. An in-guest `shutdown` does **not** stop billing; only
  the API does (`./bin/testbed.sh <deployment> stop`).

## What cloud-init does

It does the same thing on every host, so relay and clients can never be tuned
differently by accident. It sets 64 MB socket buffers, udp_mem, netdev_budget
1200, backlog 250000, nofile 1048576 and txqueuelen 20000. It also installs
iperf3, `perf`, node_exporter and numactl. The per-device tuning that does not
survive a reboot is `testbed-tune.sh`'s job, and so is the MTU: OCI's DHCP gives VNICs
9000, and `testbed-tune.sh` pins 1500 so nothing measured rides jumbo frames.

## Larger shapes, later

These are queued for the bigger shapes (E6, SmartNIC-accelerated shapes where
offered, the 94 OCPU / 94 Gbps class); none of it applies to the 8 OCPU default:

1. **Availability.** `oci compute shape list` reports
   `networking-bandwidth-in-gbps` per shape per region, so trust it over the docs.
2. **Packets per second.** OCI publishes no PPS limits, and this workload is
   PPS-bound, so PPS may bind long before bandwidth. A `sendmmsg` microbench
   should come first.
3. **SmartNIC vs plain, one variable at a time.** Does the SmartNIC reach a
   userspace QUIC send path at all?
