# Retained measurement data

Kept in-tree rather than under `results/` (which is gitignored as large and
regenerable) because the hosts that produced these may be rebuilt from
Terraform, and these runs cannot be reproduced from them afterwards.

- `linode-fanout.csv` — moqx fan-out ramp, a Linode relay driven from a
  second Linode box, 5 Mbps/sub, 20,800 B objects. Reached 600 subs / 3.1 Gbps at
  p90 63 ms, 0% loss.
- `linode-knee.csv` — the same ramp pushed to 2000 subs. Shows the knee:
  clean to ~700 subs (3.5 Gbps, p90 22-60 ms), then p90 explodes past 1 s at
  800 and collapses. Client-bound, not relay-bound — the relay was at 333% of
  1600% available.

Columns: `t_monotonic,target_mbps,tx_mbps,rx_mbps,mean_ms,p90_ms,loss_pct`.
Read `rx_mbps` (receiver-side), never `tx_mbps`.
