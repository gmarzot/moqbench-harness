# aiomoqt as a second load generator

Why bother, given moqperf works: it is an **independent implementation** —
Python on picoquic rather than C++ on mvfst — so it cannot inherit moqperf's
bugs. Given how many of those turned up this week (flow-control ceiling,
truncated subscriber counts, an unmeasurable relay, a delivery-timeout
asymmetry), a second opinion is worth having. Where the two agree, the number is
probably real.

It also does something our harness does not: **closed-loop control**. The
publisher's rate or the subscriber count is mutated live by a controller reading
subscriber-side p90 latency, loss and achieved throughput, rather than by our
climb-bisect-confirm over discrete runs.

## The tool

`aiomoqt/tools/adaptive_bench.py` on the dev branch — 2,166 lines.

| flag | does |
|---|---|
| `--mode bw \| subs` | ramp bitrate or subscriber count |
| `--role both \| pub \| sub` | split the two ends across hosts |
| `-l MS` | p90 latency SLA; the controller backs off after a breach |
| `--mp` | run the role across processes rather than one |
| `--cc-algo` | bbr, bbr1, newreno, cubic, dcubic, prague, fast |
| `-P` | parallel subgroup streams |
| `--report PATH` | per-sample metrics as CSV |
| `--sub-filter` | absolute-range, absolute-start, latest-object, next-group-start |

Positional argument is the relay URL: `moqt://host:port` for raw QUIC,
`https://host:port/path` for WebTransport. Omit it for a loopback self-test.

**Splitting across hosts is subs-mode only.** In that mode the publisher runs at
a fixed rate and the entire control loop lives on the subscriber side. It also
sets `skew_safe_signals` whenever the role is not `both`, deliberately avoiding
signals that depend on synchronised clocks between hosts — the same hazard our
payload-timestamp change took on, handled more carefully.

## Verified working

From the laptop against a moqx relay on Linode, ramping 4 → 40 subscribers:

```
   10s    16Mbps     8    │  16Mbps    8   │  42ms   47ms   3.0ms  │ 0.0%   ramp
   28s    64Mbps    32    │  65Mbps   32   │  51ms   50ms   2.8ms  │ 0.0%   ramp
   34s    80Mbps    40    │  81Mbps   40   │  44ms   48ms   2.6ms  │ 0.0%   max
```

Offered against achieved, p50/p90, jitter, loss, and the controller's state.
It stopped at `--max-subs`, not at any relay limit — 40 × 2 Mbps is nothing.

Its p90 of ~48 ms over a ~45 ms path matches what moqperf reports on the same
path, which is the cross-check that matters: two unrelated stacks agreeing.

```bash
cd <your aiomoqt checkout>
.venv/bin/python -m aiomoqt.tools.adaptive_bench moqt://<relay>:4533 -k \
  --mode subs --start-subs 4 --step-subs 4 --max-subs 40 -l 150 --sub-mbps 2 \
  --interval 3 -t 90 --report /tmp/ab.csv
```

## What blocks putting it on the boxes

The laptop works. Neither Linode box does, and it is not a small gap:

| requirement | status |
|---|---|
| `uv pip install` | fine — the venvs have no `pip`, but uv does not need it |
| dev aiomoqt | **needed** — PyPI 0.10.6 ships `examples/` with no `--role` at all |
| dev aiopquic | **needed** — dev aiomoqt imports `aiopquic.asyncio.dispatch`, absent in 0.3.11 |
| Python 3.14 | the built `.so` files are `cpython-314`; box venvs are 3.13. uv can fetch 3.14 |
| C toolchain | **absent on both boxes** — no `cc`, no `cmake` |
| picoquic | `third_party` submodule, compiled through cython/setuptools |
| prebuilt wheels | none in either `dist/` |

`pip wheel` on the laptop fails inside build isolation with
`AttributeError: ignore_egg_info_in_manifest` — a setuptools/setuptools_scm
version clash, not a problem with the source.

**Order of work:**

1. Install `build-essential` and `cmake` on both boxes.
2. `uv python install 3.14` and make a 3.14 venv, since the extensions are built
   for that ABI.
3. rsync aiopquic **including `third_party`**, then `uv pip install .` — this is
   the long step, picoquic has to compile.
4. rsync aiomoqt and `uv pip install --no-deps .`, or just run it with
   `PYTHONPATH` pointing at the source tree.
5. Split run: `--role pub` on the relay host, `--role sub` on the client host,
   `--mode subs`, `-l` at the latency budget.

The dev branches used were `gmarzot-0.11.0` (aiomoqt) and `gmarzot-0.4.0`
(aiopquic).
