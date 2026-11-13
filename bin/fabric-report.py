#!/usr/bin/env python3
"""Summarize fabric runs: per-subscriber delivery and latency, fault windows, propagation and setup.

  ./bin/fabric-report.py results/fabric/<deployment>-<stamp>[/<test>] ...

Writes summary.json beside each run's meta.json and prints a table; exits 1 when a verdict failed.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from fabric import report  # noqa: E402

if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit(__doc__.strip())
    sys.exit(report.main(sys.argv[1:]))
