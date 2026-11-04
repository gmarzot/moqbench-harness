# Generic sizing preset; all tenancy identifiers belong in the ignored
# moqbench-testbed.local.tfvars (start with its .example template).
# One relay, two clients and one optional probe; local values can override counts.
# Review the plan and ensure your environment permits the intended tests before use.

relay_shape     = "VM.Standard.E5.Flex"
relay_ocpus     = 8
relay_memory_gb = 32

client_count     = 2
client_shape     = "VM.Standard.E5.Flex"
client_ocpus     = 8
client_memory_gb = 32

probe_count     = 1
probe_shape     = "VM.Standard.E5.Flex"
probe_ocpus     = 4
probe_memory_gb = 8
