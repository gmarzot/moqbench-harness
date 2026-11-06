#!/usr/bin/env python3
"""Turn one-second /proc snapshots into rates; reject missing/restarted relay identity."""
import csv
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from telemetry import allowance_deltas, rates  # noqa: E402


def summarize(directory):
    paths = sorted(directory.glob('telemetry-*.jsonl'))
    if not paths:
        print('FAIL: no time-series telemetry collected')
        return 1
    failed = False
    for path in paths:
        try:
            records = [json.loads(line) for line in path.read_text().splitlines()]
            meta = records[0]
            samples = [r for r in records if 'monotonic' in r]
            rows = [rates(a, b, meta['clock_ticks']) for a, b in zip(samples, samples[1:])]
            if not rows or records[-1].get('type') != 'end':
                raise ValueError('empty or truncated telemetry')
            with path.with_suffix('.csv').open('w') as f:
                writer = csv.DictWriter(f, fieldnames=list(rows[0]))
                writer.writeheader(); writer.writerows(rows)
            problems = []
            if any(r['interval_s'] > 3 for r in rows):
                problems.append('sampling gap >3s')
            if any(r.get('errors') for r in samples):
                problems.append('collection errors (see JSONL)')
            if meta['pid'] and any(r['process_cores'] == '' for r in rows):
                problems.append('relay disappeared or process identity changed')
            failed |= bool(problems)
            proc = [r['process_cores'] for r in rows if r['process_cores'] != '']
            print(f'{path.stem}: {len(samples)} samples; peak host CPU {max(r["host_busy_pct"] for r in rows):.1f}%; '
                  f'peak TX {max(r["net_tx_mbps"] for r in rows):.1f} Mbps; '
                  f'max CPUs >=90% {max(r["cpus_over_90_pct"] for r in rows)}; '
                  f'peak process cores {max(proc) if proc else "n/a"}')
            print('  UDP deltas: ' + ', '.join(f'{k}={sum(r["udp_"+k+"_delta"] for r in rows)}'
                                               for k in ('InErrors', 'RcvbufErrors', 'SndbufErrors')))
            allowance = allowance_deltas(samples)
            print('  NIC allowance exceeded: ' + (', '.join(f'{k}={v}' for k, v in sorted(allowance.items()))
                                                  or 'not recorded'))
            if any(allowance.values()):
                print('  WARNING: the cloud NIC shaped traffic; see nic_allowance_exceeded_delta in the CSV')
            metric_errors = sum('metrics_error' in r for r in samples)
            if metric_errors:
                print(f'  WARNING: {metric_errors} metrics scrapes failed; details in JSONL')
            if problems:
                print('  FAIL: ' + '; '.join(problems))
        except (OSError, ValueError, KeyError, IndexError, ZeroDivisionError) as exc:
            failed = True
            print(f'FAIL {path.name}: {exc}')
    print('NIC rates sum non-loopback interfaces; /proc thread, softnet and memory counters remain in JSONL.')
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(summarize(Path(sys.argv[1])))
