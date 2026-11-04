#!/usr/bin/env python3
"""Bounded, read-only /proc sampler; JSONL counters retain their units and identity."""
import argparse
import json
import os
from pathlib import Path
import time
import urllib.request


def stat(path):
    raw = path.read_text()
    name, rest = raw[raw.index('(') + 1:].rsplit(')', 1)
    fields = rest.split()  # starts at field 3 (state)
    return dict(name=name, state=fields[0], ticks=int(fields[11]) + int(fields[12]),
                start_ticks=int(fields[19]), rss_bytes=int(fields[21]) * os.sysconf('SC_PAGE_SIZE'),
                cpu=int(fields[36]))


def comm_totals(names):
    """Summed CPU ticks, RSS, threads and count of the processes whose comm is in names."""
    total = dict(ticks=0, rss_bytes=0, threads=0, count=0)
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if (proc / 'comm').read_text().strip() not in names:
                continue
            fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()  # from field 3
        except OSError:
            continue  # The process exited while being read.
        total['ticks'] += int(fields[11]) + int(fields[12])
        total['rss_bytes'] += int(fields[21]) * os.sysconf('SC_PAGE_SIZE')
        total['threads'] += int(fields[17]); total['count'] += 1
    return total


def snapshot(pid, comms=()):
    result = dict(t_wall=time.time(), monotonic=time.monotonic(), errors=[])
    for label, path in [('cpu', '/proc/stat'), ('netdev', '/proc/net/dev'),
                        ('snmp', '/proc/net/snmp'), ('softnet', '/proc/net/softnet_stat'),
                        ('memory', '/proc/meminfo')]:
        try:
            result[label] = Path(path).read_text()
        except OSError as exc:
            result['errors'].append(f'{label}: {exc}')
    if pid:
        try:
            proc = Path('/proc') / str(pid)
            result['process'] = dict(pid=pid, **stat(proc / 'stat'))
            result['threads'] = {}
            for thread in proc.glob('task/*/stat'):
                try:
                    result['threads'][thread.parent.name] = stat(thread)
                except FileNotFoundError:
                    pass  # A short-lived thread can exit while being sampled.
        except OSError as exc:
            result['errors'].append(f'process {pid}: {exc}')
            result['process'] = None
    if comms:
        result['comm'] = comm_totals(set(comms))
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid', type=int, default=0)
    p.add_argument('--duration', type=int, required=True)
    p.add_argument('--stop-file', type=Path, required=True)
    p.add_argument('--metrics-url', default='')
    p.add_argument('--comm', action='append', default=[],
                   help='also sum CPU, RSS and threads of processes with this comm (repeatable)')
    args = p.parse_args()
    next_metrics = 0
    print(json.dumps(dict(type='metadata', hostname=os.uname().nodename,
                          clock_ticks=os.sysconf('SC_CLK_TCK'), pid=args.pid,
                          comm=sorted(args.comm))), flush=True)
    end = time.monotonic() + args.duration
    while time.monotonic() < end and not args.stop_file.exists():
        start = time.monotonic()
        sample = snapshot(args.pid, args.comm)
        if args.metrics_url and start >= next_metrics:
            try:
                with urllib.request.urlopen(args.metrics_url, timeout=0.5) as response:
                    sample['metrics'] = response.read(1048576).decode()
            except (OSError, ValueError) as exc:
                sample['metrics_error'] = str(exc)
            next_metrics = start + 5
        print(json.dumps(sample), flush=True)
        time.sleep(max(0, 1 - (time.monotonic() - start)))
    print(json.dumps(dict(type='end', t_wall=time.time())), flush=True)


if __name__ == '__main__':
    main()
