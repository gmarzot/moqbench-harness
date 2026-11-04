# Graviton2 sizing preset: one relay and two load generators, 100 Gbps each, in
# one cluster placement group. Account, profile, key and egress CIDR belong in
# the ignored moqbench-aws.local.tfvars (from my-account.local.tfvars.example).

region            = "us-west-2"
availability_zone = "us-west-2a"
arch              = "arm64"

relay_instance_type = "c6gn.16xlarge"

client_count         = 2
client_instance_type = "c6gn.16xlarge"

probe_count = 0
