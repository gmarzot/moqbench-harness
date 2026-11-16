#!/usr/bin/env python3
"""Calibration probe: sequenced UDP through the emulator, echoed back, on one host clock.

  probe.py echo --bind 0.0.0.0:7000 --out FILE
  probe.py send --to HOST:7000 --pps 1900 --size 1316 --seconds 60 --out FILE

The echo side logs seq,t_send_ns,t_recv_ns for every packet (kernel receive time) and returns a 24-byte
reply. The sender logs seq,t_send_ns,t_echo_ns for every reply. Forward loss, reverse loss, one-way
delay, RTT and reordering follow from the two files.
"""
import argparse
import signal
import socket
import struct
import sys
import time
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tslib  # noqa: E402

HDR = struct.Struct('!QQ')
REPLY = struct.Struct('!QQQ')


def echo(a):
    host, _, port = a.bind.rpartition(':')
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    s.setsockopt(socket.SOL_SOCKET, 35, 1)
    s.bind((host, int(port)))
    s.settimeout(0.5)
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    with open(a.out, 'w', buffering=1 << 16) as f:
        f.write('seq,t_send_ns,t_recv_ns,t_user_ns\n')
        while not stop:
            try:
                data, anc, _, peer = s.recvmsg(65536, 64)
            except socket.timeout:
                continue
            if len(data) < HDR.size:
                continue
            seq, t_send = HDR.unpack_from(data)
            t_user = time.time_ns()
            t_recv = tslib.timestamps_cmsg(anc) or t_user
            f.write(f'{seq},{t_send},{t_recv},{t_user}\n')
            s.sendto(REPLY.pack(seq, t_send, t_recv), peer)


def send(a):
    host, _, port = a.to.rpartition(':')
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    s.setsockopt(socket.SOL_SOCKET, 35, 1)
    s.setblocking(False)
    dest = (host, int(port))
    pad = b'\0' * max(0, a.size - HDR.size)
    interval = 1e9 / a.pps
    t0 = time.monotonic_ns()
    end = t0 + int(a.seconds * 1e9)
    seq = 0
    with open(a.out, 'w', buffering=1 << 16) as f:
        f.write('seq,t_send_ns,t_echo_ns\n')
        drain_until = None
        while True:
            now = time.monotonic_ns()
            if now < end:
                due = t0 + int(seq * interval)
                if now >= due:
                    s.sendto(HDR.pack(seq, time.time_ns()) + pad, dest)
                    seq += 1
                    continue
            elif drain_until is None:
                drain_until = now + 3_000_000_000
            elif now > drain_until:
                break
            try:
                data, anc, _, _ = s.recvmsg(256, 64)
                if len(data) >= REPLY.size:
                    rseq, t_send, _ = REPLY.unpack_from(data)
                    f.write(f'{rseq},{t_send},{tslib.timestamps_cmsg(anc) or time.time_ns()}\n')
            except BlockingIOError:
                time.sleep(0.0001)
        f.write(f'# sent {seq}\n')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='mode', required=True)
    e = sub.add_parser('echo')
    e.add_argument('--bind', required=True)
    e.add_argument('--out', required=True)
    s = sub.add_parser('send')
    s.add_argument('--to', required=True)
    s.add_argument('--pps', type=float, default=1900)
    s.add_argument('--size', type=int, default=1316)
    s.add_argument('--seconds', type=float, default=60)
    s.add_argument('--out', required=True)
    a = p.parse_args(argv)
    (echo if a.mode == 'echo' else send)(a)
    return 0


if __name__ == '__main__':
    sys.exit(main())
