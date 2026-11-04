# Harness status and next steps

This document describes the generic harness. Deployment inventory and
environment-specific results belong in ignored local files, not in this repository.

## Current status

The load generators, AWS and OCI testbeds, sweeps and reporting are working;
local source and reporting checks run without a testbed.

Implemented capabilities:

- Named testbed deployments with per-user, ignored environment settings.
- Independent deployment of public relay implementations and local adapters.
- Adaptive capacity searches and fixed-population delivery validation.
- Per-second host/process/network telemetry and collection of incomplete runs.
- Reproducible moq-dev build manifests and a pinned standalone moqx installer.

## Running a measurement

1. Pin compatible load-generator and transport versions and run local checks.
2. Review the selected environment's plan, resource sizes and access rules.
3. Provision or start only the intended deployment and reapply host tuning.
4. Run a small interoperability check and measure the network path capacity.
5. Search capacity adaptively, then validate selected populations with fixed runs.
6. Evaluate actual delivery and latency together; an active subscription count
   or a completed duration alone is not a capacity result.
7. Preserve diagnostics on failure before stopping the workload.

See RUNBOOK.md for commands, MOQDEV.md for that public relay's build and settings,
and BENCHMARKING-GUIDE.md for measurement methodology. Put raw logs, host addresses
and environment-specific results under ignored results/ or .testbed/, and keep
private relay integrations in ignored relays.local.sh.
