#!/usr/bin/env python3
"""Turn one-second /proc snapshots into rates; reject missing/restarted relay identity."""
import csv
import json
from pathlib import Path
import sys


def cpu(text):
    result = {}
    for line in text.splitlines():
        key, *fields = line.split()
        if key.startswith('cpu'):
            v = list(map(int, fields))[:8]  # guest time is already included in user/nice
            result[key] = (sum(v), v[3] + v[4])
    return result


def net(text):
    return {line.split(':')[0].strip(): list(map(int, line.split(':')[1].split()))
            for line in text.splitlines() if ':' in line}


def udp(text):
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith('Udp:'):
            return dict(zip(line.split()[1:], map(int, lines[i+1].split()[1:])))
    return {}


def rates(a, b, hz):
    dt = b['monotonic'] - a['monotonic']
    ac, bc = cpu(a['cpu']), cpu(b['cpu'])
    busy = {k: 100 * (1 - (bc[k][1] - ac[k][1]) / max(1, bc[k][0] - ac[k][0]))
            for k in ac.keys() & bc.keys()}
    an, bn = net(a['netdev']), net(b['netdev'])
    interfaces = (an.keys() & bn.keys()) - {'lo'}
    au, bu = udp(a['snmp']), udp(b['snmp'])
    ap, bp = a.get('process'), b.get('process')
    same = bool(ap and bp and ap['pid'] == bp['pid'] and ap['start_ticks'] == bp['start_ticks']
                and bp.get('state') != 'Z')
    row = dict(t_wall=b['t_wall'], interval_s=dt, host_busy_pct=busy['cpu'],
               hottest_cpu_pct=max(v for k, v in busy.items() if k != 'cpu'),
               cpus_over_90_pct=sum(v >= 90 for k, v in busy.items() if k != 'cpu'),
               process_cores=(bp['ticks']-ap['ticks'])/hz/dt if same else '',
               process_rss_bytes=bp['rss_bytes'] if bp else '',
               net_tx_mbps=sum(bn[k][8]-an[k][8] for k in interfaces)*8/dt/1e6,
               net_rx_mbps=sum(bn[k][0]-an[k][0] for k in interfaces)*8/dt/1e6)
    for key in ('InErrors', 'RcvbufErrors', 'SndbufErrors', 'InDatagrams', 'OutDatagrams'):
        row['udp_' + key + '_delta'] = bu.get(key, 0) - au.get(key, 0)
    row['nic_allowance_exceeded_delta'] = (sum(b.get('nic_allowance', {}).values())
                                           - sum(a.get('nic_allowance', {}).values()))
    return row


def allowance_deltas(samples):
    """Whole-run growth of each NIC allowance counter; {} when none were recorded."""
    seen = [s['nic_allowance'] for s in samples if s.get('nic_allowance')]
    return {k: seen[-1].get(k, 0) - seen[0][k] for k in seen[0]} if seen else {}


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
