#!/usr/bin/env python3
"""Sample control URLs and interface counters beside a relay, as JSONL.

  fabric-poll.py [--url NAME=URL ...] [--netdev] --interval S --duration S --out PATH

A URL's body is written only when it changed since its last sample. --netdev
writes this namespace's interface addresses once, then every interface's
rx/tx bytes and packets each interval.
"""
import argparse
import json
import subprocess
import time
import urllib.error
import urllib.request


def fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return r.status, r.read().decode(errors='replace')
    except urllib.error.HTTPError as e:
        return e.code, ''
    except OSError as e:
        return 0, str(e)


def netdev():
    out = {}
    with open('/proc/net/dev') as f:
        for line in f.readlines()[2:]:
            name, rest = line.split(':', 1)
            v = rest.split()
            out[name.strip()] = [int(v[0]), int(v[1]), int(v[8]), int(v[9])]
    return out


def addresses():
    text = subprocess.run(['ip', '-o', '-4', 'addr', 'show'], capture_output=True, text=True).stdout
    return {f[1]: f[3].split('/')[0] for f in (line.split() for line in text.splitlines()) if len(f) > 3}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--url', action='append', default=[])
    p.add_argument('--netdev', action='store_true')
    p.add_argument('--interval', type=float, default=1.0)
    p.add_argument('--duration', type=float, required=True)
    p.add_argument('--out', required=True)
    a = p.parse_args()
    urls = dict(u.split('=', 1) for u in a.url)
    last = {}
    end = time.time() + a.duration
    with open(a.out, 'a', buffering=1) as out:
        if a.netdev:
            out.write(json.dumps({'t': time.time(), 'name': 'ifaddr', 'body': addresses()}) + '\n')
        while time.time() < end:
            tick = time.time()
            for name, url in urls.items():
                t0 = time.time()
                status, body = fetch(url)
                row = {'t': t0, 'dt': time.time() - t0, 'name': name, 'status': status}
                if body != last.get(name):
                    row['body'] = body
                    last[name] = body
                out.write(json.dumps(row) + '\n')
            if a.netdev:
                out.write(json.dumps({'t': time.time(), 'name': 'netdev', 'dev': netdev()}) + '\n')
            time.sleep(max(0.0, a.interval - (time.time() - tick)))


if __name__ == '__main__':
    main()
