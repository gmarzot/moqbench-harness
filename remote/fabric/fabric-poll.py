#!/usr/bin/env python3
"""Poll control URLs beside a relay and write JSONL: a body only when it changed since that URL's last sample.

  fabric-poll.py --url NAME=URL [--url ...] --interval S --duration S --out PATH
"""
import argparse
import json
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


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--url', action='append', required=True)
    p.add_argument('--interval', type=float, default=1.0)
    p.add_argument('--duration', type=float, required=True)
    p.add_argument('--out', required=True)
    a = p.parse_args()
    urls = dict(u.split('=', 1) for u in a.url)
    last = {}
    end = time.time() + a.duration
    with open(a.out, 'a', buffering=1) as out:
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
            time.sleep(max(0.0, a.interval - (time.time() - tick)))


if __name__ == '__main__':
    main()
