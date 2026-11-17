#!/usr/bin/env python3
"""Clock probe: a 1 ms timer. Logs every wake-up later than --late-ms (host or hypervisor pauses) and
every step of the wall clock against the monotonic clock larger than --step-ms (time sync stepping
CLOCK_REALTIME, which every tap timestamp uses), as t_ns,kind,ms; then a JSON summary line.

  clockprobe.py --out FILE [--late-ms 5] [--duration S]
"""
import argparse
import json
import signal
import sys
import time


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--out', required=True)
    p.add_argument('--late-ms', type=float, default=5.0)
    p.add_argument('--step-ms', type=float, default=1.0)
    p.add_argument('--duration', type=float, default=0)
    a = p.parse_args(argv)
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    period = 1_000_000
    t0 = time.monotonic_ns()
    end = t0 + int(a.duration * 1e9) if a.duration else None
    k, worst, count, steps = 0, 0.0, 0, 0
    offset = time.time_ns() - time.monotonic_ns()
    with open(a.out, 'w', buffering=1) as f:
        f.write('t_ns,kind,ms\n')
        while not stop and (end is None or time.monotonic_ns() < end):
            k += 1
            due = t0 + k * period
            delay = due - time.monotonic_ns()
            if delay > 0:
                time.sleep(delay / 1e9)
            late = (time.monotonic_ns() - due) / 1e6
            if late > a.late_ms:
                count += 1
                f.write(f'{time.time_ns()},late,{late:.3f}\n')
                k += int(late)
            worst = max(worst, late)
            now_offset = time.time_ns() - time.monotonic_ns()
            if abs(now_offset - offset) > a.step_ms * 1e6:
                steps += 1
                f.write(f'{time.time_ns()},step,{(now_offset - offset) / 1e6:.3f}\n')
                offset = now_offset
        f.write('# ' + json.dumps(dict(ticks=k, late_events=count, worst_ms=round(worst, 3), clock_steps=steps)) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
