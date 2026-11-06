#!/usr/bin/env python3
"""Run adaptive_bench with new frame sizes for one --video profile.

usage: adaptive-bench-frames.py <profile> <I> <P> <B> <module or .py> [args...]
Workers fork, so the sizes reach them.
"""
import runpy
import sys

from aiomoqt.track import VideoTrack

profile, i_frame, p_frame, b_frame, target = sys.argv[1:6]
VideoTrack.PROFILES[profile] = dict(i_frame=int(i_frame), p_frame=int(p_frame), b_frame=int(b_frame))
sys.argv = [target, *sys.argv[6:]]
if target.endswith('.py'):
    runpy.run_path(target, run_name='__main__')
else:
    runpy.run_module(target, run_name='__main__')
