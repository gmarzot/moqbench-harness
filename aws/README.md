> For an AWS testbed driven by `./bin/testbed.sh` like OCI, see `aws/main.tf` and the
> README's "AWS testbed" section. The scripts below are the older hand-run setup.
# AWS c7gn.12xlarge harness (aiomoqt load generator)

Drives a moqx relay from aiomoqt's `adaptive_bench` split across two hosts.
Keep run journals and results local.

Two hosts you provide: a relay and a client. Everything stays under
`~/gmarzot-bench/` on each box. `run-bench.sh` takes `RELAY_PUB`, `RELAY_PRIV`
and `CLIENT` from the environment; **public IPs change on stop/start**.

## One-time setup

```bash
scp *.sh ubuntu@<relay>:gmarzot-bench/
scp *.sh ubuntu@<client>:gmarzot-bench/
ssh ubuntu@<relay>  '~/gmarzot-bench/build-aiomoqt.sh'   # ~10 min, compiles picoquic
ssh ubuntu@<client> '~/gmarzot-bench/build-aiomoqt.sh'
ssh ubuntu@<relay>  '~/gmarzot-bench/tune-client.sh'     # run on BOTH boxes
ssh ubuntu@<client> '~/gmarzot-bench/tune-client.sh'
ssh ubuntu@<relay>  '~/gmarzot-bench/start-relay.sh'
```

`build-aiomoqt.sh` uses a uv-managed CPython 3.14 because the boxes have no
Python headers and no `ensurepip`. The sources must already be rsynced to
`~/gmarzot-bench/src/{aiopquic,aiomoqt}` from the dev branches
(`gmarzot-0.4.0`, `gmarzot-0.11.0`) — read-only, nothing is pushed back.

## Running

```bash
./run-bench.sh <mbps> <objsize> <start> <step> <max> <slo_ms> <dur_s> [join] [stagger]

./run-bench.sh 5 20800 200 200 15000 512 300 1 0.005   # fast fan-out ramp
./run-bench.sh 5 20800 6000 200 6200 512 300 600 0.005 # hold at 6000
```

`run-bench.sh` mints a fresh namespace per run. That is required, not
cosmetic: the relay refuses a re-registration of a track it still holds, and a
reused name silently yields `rx=0` with the publisher stuck at `forward=0`.

To hold a level rather than climb, set `max` just above `start` and `join` to
a value longer than the run — reaching `--max-subs` ends the run.

## Reading a run

- subscriber table + CSV: `~/gmarzot-bench/logs/sub-<ns>-<mbps>mbps.{log,csv}`
- publisher: `~/gmarzot-bench/logs/pub-<ns>.log`
- relay: `~/gmarzot-bench/logs/relay.log`, admin on `:19701` (`/info`,
  `/metrics`, `/state`)

Watch the **actual** subscriber count, not the target — the controller keeps
commanding more after the client has stopped keeping up.

## Gotchas

- `pkill -f aiomoqt...` matches the invoking command line too; keep kills
  inside script files, never inline over ssh.
- Python block-buffers stdout to a file; every launcher passes `-u`.
- `stop-bench.sh` may need several minutes to be reachable once the client
  box is thrashing; `killall -9 python` over a long `ConnectTimeout` works.
