#!/usr/bin/env python3
"""Combine fixed population CSVs using wall-clock-nearest sample windows.

Host clocks must be synchronized, as for the benchmark's one-way latency.
The reported latency is the worst host's reported p90, not a pooled percentile.
"""
import argparse
import bisect
import csv
import math
import json
import re
from pathlib import Path
import statistics


def combine(series, tolerance=5.0):
    times = [[r['t_wall'] for r in rows] for rows in series]
    used = [-1] * len(series)
    for first in series[0]:
        paired = [first]
        indices = []
        for host, rows in enumerate(series[1:], 1):
            j = bisect.bisect_left(times[host], first['t_wall'])
            choices = [k for k in (j - 1, j) if used[host] < k < len(rows)]
            if not choices:
                break
            k = min(choices, key=lambda i: abs(times[host][i] - first['t_wall']))
            if abs(times[host][k] - first['t_wall']) > tolerance:
                break
            paired.append(rows[k])
            indices.append(k)
        if len(paired) != len(series):
            continue
        # Bound the whole set, not each host relative to the anchor.
        if max(r["t_wall"] for r in paired) - min(r["t_wall"] for r in paired) > tolerance:
            continue
        used[1:] = indices
        yield {
            't_wall': max(r['t_wall'] for r in paired),
            'sample_spread_s': max(r['t_wall'] for r in paired) - min(r['t_wall'] for r in paired),
            'target_subs': sum(int(r['target_subs']) for r in paired),
            'active_subs': sum(int(r['active_subs']) for r in paired),
            'rx_mbps': sum(r['rx_mbps'] for r in paired),
            'min_host_delivery_fraction': min(r['rx_mbps'] / r['target_mbps'] if r['target_mbps'] > 0 else 0 for r in paired),
            'worst_host_loss_pct': max(r['loss_pct'] for r in paired),
            'worst_host_p90_ms': max(r['p90_ms'] for r in paired),
            'latency_known': int(all(r['latency_known'] for r in paired)),
        }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('directory', type=Path)
    p.add_argument('--hosts', type=int, required=True)
    p.add_argument('--subs', type=int, required=True)
    p.add_argument('--duration', type=int, required=True)
    p.add_argument('--slo-ms', type=float, default=500)
    p.add_argument('--sample-span', type=float, default=5.0,
                   help='maximum total spread of report timestamps; sums are approximate')
    p.add_argument('--min-delivery', type=float, default=0.95)
    p.add_argument('--min-pass-fraction', type=float, default=0.95)
    p.add_argument('--settle-seconds', type=float, default=10)
    p.add_argument('--min-steady-seconds', type=float, default=30)
    p.add_argument('--output-prefix', default='combined', help='use another prefix to preserve old reports')
    args = p.parse_args()
    if not (0 < args.min_delivery <= 1 and 0 < args.min_pass_fraction <= 1
            and args.sample_span > 0 and args.settle_seconds >= 0 and args.min_steady_seconds >= 0):
        p.error('invalid validation thresholds')
    series = []
    complete = True
    resets = []
    for i in range(args.hosts):
        path = args.directory / f'sub{i}/loadreport.csv'
        if not path.is_file():
            p.exit(1, f'Missing report: {path}\n')
        with path.open() as f:
            rows = [{k: float(v) for k, v in r.items()} for r in csv.DictReader(f)]
        if not rows or any(not math.isfinite(v) for r in rows for v in r.values()):
            p.exit(1, f'Empty or invalid report: {path}\n')
        series.append(sorted(rows, key=lambda r: r['t_wall']))
        log = (args.directory / f'sub{i}.log').read_text()
        complete &= f'duration cap reached ({args.duration}s)' in log
        resets.append(len(re.findall(r'reset by peer', log)))
    rows = list(combine(series, args.sample_span))
    if not rows:
        p.exit(1, f'No overlapping samples within {args.sample_span:g}s; check host clock synchronization.\n')
    with (args.directory / f'{args.output_prefix}.csv').open('w') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    full = [r for r in rows if r['active_subs'] == args.subs]
    steady = [r for r in rows if full and r['t_wall'] >= full[0]['t_wall'] + args.settle_seconds]
    healthy = [r for r in steady if r['active_subs'] == args.subs and r['latency_known']
               and r['worst_host_p90_ms'] <= args.slo_ms
               and r['min_host_delivery_fraction'] >= args.min_delivery
               and r['worst_host_loss_pct'] == 0]
    span = steady[-1]['t_wall'] - steady[0]['t_wall'] if steady else 0
    gaps_ok = all(b['t_wall'] - a['t_wall'] <= 10 for a, b in zip(steady, steady[1:]))
    passed = bool(gaps_ok and complete and span >= args.min_steady_seconds and steady
                  and len(healthy) / len(steady) >= args.min_pass_fraction)
    peak = max(rows, key=lambda r: r['rx_mbps'])
    report = dict(status='PASS' if passed else 'FAIL', complete=complete, gaps_ok=gaps_ok,
                  steady_samples=len(steady), healthy_samples=len(healthy), steady_span_s=span,
                  reset_warning_events_by_host=resets, peak=peak, last=rows[-1],
                  maximum_worst_host_p90_ms=max(r['worst_host_p90_ms'] for r in rows),
                  thresholds=dict(slo_ms=args.slo_ms, min_delivery=args.min_delivery,
                                  min_pass_fraction=args.min_pass_fraction,
                                  settle_seconds=args.settle_seconds,
                                  min_steady_seconds=args.min_steady_seconds,
                                  maximum_sample_span_s=args.sample_span))
    (args.directory / f'{args.output_prefix}.json').write_text(json.dumps(report, indent=2) + '\n')
    last = rows[-1]
    print(f'-- TOTAL fixed load: requested {args.subs} subscribers over {args.hosts} hosts')
    print(f'   Last matched sample: {last["active_subs"]} active, {last["rx_mbps"]:.1f} Mbps received')
    print(f'   Full population: {len(full)}/{len(rows)} matched samples (includes fill period)')
    if full:
        print(f'   At full population: median aggregate Rx {statistics.median(r["rx_mbps"] for r in full):.1f} Mbps')
        print(f'   Steady samples meeting population, delivery, latency and reported-loss checks: {len(healthy)}/{len(steady)}')
    print(f'   Every client reached duration cap: {"yes" if complete else "NO — incomplete run"}')
    print(f'   Peak approximate aggregate Rx: {peak["rx_mbps"]:.1f} Mbps; max reported p90: {report["maximum_worst_host_p90_ms"]:.1f} ms')
    print(f'   Stream-reset warnings: {sum(resets)} events (not unique streams/subscribers)')
    print(f'   Verdict: {report["status"]}; requires >= {args.min_pass_fraction:.0%} healthy steady samples over >= {args.min_steady_seconds:g}s')
    print(f'   Delivery floor: {args.min_delivery:.0%} of target on EVERY host; p90 <= {args.slo_ms:g}ms; reported loss = 0.')
    print(f'   {args.output_prefix}.csv: approximate sample sums, maximum timestamp spread {args.sample_span:g}s; worst host p90 is not pooled.')
    print('   Aggregate checks do not prove per-subscriber delivery; active slots and zero reported loss alone are insufficient.')
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
