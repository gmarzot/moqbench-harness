#!/usr/bin/env python3
"""Bounded, read-only /proc sampler; JSONL counters retain their units and identity."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.error
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


def default_interface():
    """The interface of the first default route, or '' without one."""
    try:
        route = subprocess.run(['ip', '-o', '-4', 'route', 'show', 'to', 'default'],
                               capture_output=True, text=True, timeout=5).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return ''
    return route[4] if len(route) > 4 else ''


def allowance_counters(text):
    """The *_allowance_exceeded counters in ethtool -S output: packets a cloud NIC shaped."""
    counters = {}
    for line in text.splitlines():
        name, _, value = line.partition(':')
        if name.strip().endswith('_allowance_exceeded'):
            counters[name.strip()] = int(value)
    return counters


def snapshot(pid, comms=(), nic=''):
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
    if nic:
        try:
            stats = subprocess.run(['ethtool', '-S', nic], capture_output=True, text=True, timeout=1)
            result['nic_allowance'] = allowance_counters(stats.stdout)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            result['errors'].append(f'ethtool: {exc}')
    return result


def fetch(url, timeout=0.5):
    """(status, body) of one GET; status 0 when the server did not answer."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read(4194304).decode(errors='replace')
    except urllib.error.HTTPError as exc:
        return exc.code, ''
    except (OSError, ValueError) as exc:
        return 0, str(exc)


def nft_counters(table):
    """{chain: {comment: [bytes, packets]}} for the counter rules of an inet table."""
    run = subprocess.run(['sudo', '-n', 'nft', '-j', 'list', 'table', 'inet', table],
                         capture_output=True, text=True, timeout=2)
    if run.returncode:
        raise OSError(run.stderr.strip() or f'nft exit {run.returncode}')
    out = {}
    for item in json.loads(run.stdout).get('nftables', []):
        rule = item.get('rule')
        if not rule or 'comment' not in rule:
            continue
        for expr in rule.get('expr', []):
            if 'counter' in expr:
                c = expr['counter']
                out.setdefault(rule['chain'], {})[rule['comment']] = [c['bytes'], c['packets']]
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid', type=int, default=0)
    p.add_argument('--duration', type=int, required=True)
    p.add_argument('--stop-file', type=Path, required=True)
    p.add_argument('--metrics-url', default='')
    p.add_argument('--comm', action='append', default=[],
                   help='also sum CPU, RSS and threads of processes with this comm (repeatable)')
    p.add_argument('--json-url', action='append', default=[], metavar='NAME=URL',
                   help='also fetch this control URL every --json-interval; a body is kept only when it changed')
    p.add_argument('--json-interval', type=float, default=5.0)
    p.add_argument('--nft', default='', metavar='TABLE',
                   help='also read the counter rules of this inet table every sample')
    p.add_argument('--interval', type=float, default=1.0)
    args = p.parse_args()
    next_metrics = 0
    json_urls = dict(u.split('=', 1) for u in args.json_url)
    json_last, next_json = {}, 0
    nic = default_interface() if shutil.which('ethtool') else ''
    print(json.dumps(dict(type='metadata', hostname=os.uname().nodename,
                          clock_ticks=os.sysconf('SC_CLK_TCK'), pid=args.pid,
                          comm=sorted(args.comm))), flush=True)
    end = time.monotonic() + args.duration
    while time.monotonic() < end and not args.stop_file.exists():
        start = time.monotonic()
        sample = snapshot(args.pid, args.comm, nic)
        if args.metrics_url and start >= next_metrics:
            try:
                with urllib.request.urlopen(args.metrics_url, timeout=0.5) as response:
                    sample['metrics'] = response.read(1048576).decode()
            except (OSError, ValueError) as exc:
                sample['metrics_error'] = str(exc)
            next_metrics = start + 5
        if json_urls and start >= next_json:
            sample['json'], sample['json_status'] = {}, {}
            for name, url in json_urls.items():
                status, body = fetch(url)
                sample['json_status'][name] = status
                if body != json_last.get(name):
                    sample['json'][name] = body
                    json_last[name] = body
            next_json = start + args.json_interval
        if args.nft:
            try:
                sample['nft'] = nft_counters(args.nft)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                sample['errors'].append(f'nft: {exc}')
        print(json.dumps(sample), flush=True)
        time.sleep(max(0, args.interval - (time.monotonic() - start)))
    print(json.dumps(dict(type='end', t_wall=time.time())), flush=True)


if __name__ == '__main__':
    main()
