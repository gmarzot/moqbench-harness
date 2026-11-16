#!/usr/bin/env python3
"""Sample commands and HTTP URLs every --interval seconds into JSONL ({t, name, out}).

  sampler.py --cmd NAME=COMMAND [...] --url NAME=URL [...] --interval 1 --out FILE [--duration S]
"""
import argparse
import json
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--cmd', action='append', default=[])
    p.add_argument('--url', action='append', default=[])
    p.add_argument('--interval', type=float, default=1.0)
    p.add_argument('--duration', type=float, default=0)
    p.add_argument('--out', required=True)
    a = p.parse_args(argv)
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    jobs = [('cmd', *s.split('=', 1)) for s in a.cmd] + [('url', *s.split('=', 1)) for s in a.url]
    t0 = time.monotonic()
    k = 0
    with open(a.out, 'w', buffering=1) as f:
        while not stop and (not a.duration or time.monotonic() - t0 < a.duration):
            for kind, name, what in jobs:
                t = time.time()
                if kind == 'cmd':
                    r = subprocess.run(what, shell=True, capture_output=True, text=True, timeout=10)
                    out = r.stdout
                else:
                    try:
                        with urllib.request.urlopen(what, timeout=2) as resp:
                            out = resp.read().decode(errors='replace')
                    except (urllib.error.URLError, OSError) as exc:
                        out = f'ERROR {exc}'
                f.write(json.dumps(dict(t=t, name=name, out=out)) + '\n')
            k += 1
            delay = t0 + k * a.interval - time.monotonic()
            if delay > 0:
                time.sleep(delay)
    return 0


if __name__ == '__main__':
    sys.exit(main())
