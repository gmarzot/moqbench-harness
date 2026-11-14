#!/usr/bin/env python3
"""Prefix each line of stdin with the epoch time it was read, in seconds with microseconds."""
import sys
import time

for line in sys.stdin:
    sys.stdout.write(f'{time.time():.6f} {line}')
    sys.stdout.flush()
