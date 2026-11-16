#!/usr/bin/env python3
"""TS tap: timestamps every datagram (kernel receive time) on one or more UDP ports, or every read of
stdin, and logs PCRs, video frames, output gaps and per-second counts.

  tstap.py --udp NAME=GROUP:PORT [...] --out DIR [--frames] [--hash] [--gap-ms 50] [--duration S]
  tstap.py --stdin NAME --annexb --out DIR      (an H.264 elementary-stream pipe)

Files per NAME in DIR: NAME-pcr.csv (pid,pcr,t_ns, n = non-null packets so far), NAME-frames.csv, NAME-gaps.csv (t_before_ns,
t_after_ns), NAME-sec.csv and NAME-summary.json, written on SIGINT, SIGTERM or at --duration.
"""
import argparse
import json
import os
import select
import signal
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tslib  # noqa: E402

SO_TIMESTAMPNS = 35


class Sink:
    def __init__(self, name, out, frames, hashes, gap_ms, annexb=False):
        self.name, self.out = name, out
        self.f_pcr = open(os.path.join(out, f'{name}-pcr.csv'), 'w', buffering=1 << 16)
        self.f_pcr.write('pid,pcr,t_ns,n\n')
        self.f_fr = open(os.path.join(out, f'{name}-frames.csv'), 'w', buffering=1 << 16)
        self.f_fr.write('pts,key,size,t_first_ns,t_last_ns,broken,hash\n')
        self.f_gap = open(os.path.join(out, f'{name}-gaps.csv'), 'w', buffering=1)
        self.f_gap.write('t_before_ns,t_after_ns\n')
        self.f_sec = open(os.path.join(out, f'{name}-sec.csv'), 'w', buffering=1)
        self.f_sec.write('t_s,datagrams,bytes,packets,nulls,cc_errors\n')
        self.gap_ns = int(gap_ms * 1e6)
        on_frame = self.frame if (frames or annexb) else None
        self.dmx = None if annexb else tslib.Demux(on_pcr=self.pcr, on_frame=on_frame, frames=frames, hashes=hashes)
        self.annexb = tslib.AnnexB(self.frame) if annexb else None
        self.first = self.last = None
        self.datagrams = self.bytes = 0
        self.max_gap_ns = 0
        self.sec, self.sec_counts = None, [0, 0, 0, 0, 0]

    def pcr(self, pid, pcr, t):
        d = self.dmx
        self.f_pcr.write(f'{pid},{pcr},{t},{d.packets - d.nulls}\n')

    def frame(self, r):
        self.f_fr.write(f"{r['pts']},{r['key']},{r['size']},{r['t_first']},{r['t_last']},{r['broken']},{r['hash']}\n")

    def take(self, data, t):
        if self.last is not None:
            gap = t - self.last
            if gap >= self.gap_ns:
                self.f_gap.write(f'{self.last},{t}\n')
            self.max_gap_ns = max(self.max_gap_ns, gap)
        else:
            self.first = t
        self.last = t
        self.datagrams += 1
        self.bytes += len(data)
        sec = t // 1_000_000_000
        if sec != self.sec:
            self.flush_sec()
            self.sec = sec
        if self.annexb is not None:
            self.annexb.feed(data, t)
            self.sec_counts[0] += 1
            self.sec_counts[1] += len(data)
            return
        dmx = self.dmx
        before = (dmx.packets, dmx.nulls, sum(dmx.cc_errors.values()))
        dmx.feed(data, t)
        c = self.sec_counts
        c[0] += 1
        c[1] += len(data)
        c[2] += dmx.packets - before[0]
        c[3] += dmx.nulls - before[1]
        c[4] += sum(dmx.cc_errors.values()) - before[2]

    def flush_sec(self):
        if self.sec is not None:
            self.f_sec.write(f'{self.sec},' + ','.join(map(str, self.sec_counts)) + '\n')
        self.sec_counts = [0, 0, 0, 0, 0]

    def close(self):
        self.flush_sec()
        if self.dmx is not None:
            self.dmx.flush()
        if self.annexb is not None:
            self.annexb.close()
        summary = dict(name=self.name, first_ns=self.first, last_ns=self.last, datagrams=self.datagrams,
                       bytes=self.bytes, max_gap_ms=round(self.max_gap_ns / 1e6, 3))
        if self.dmx is not None:
            d = self.dmx
            summary.update(packets=d.packets, nulls=d.nulls, tei=d.tei, sync_losses=d.sync_losses,
                           cc_errors={str(k): v for k, v in d.cc_errors.items()},
                           pid_counts={str(k): v for k, v in d.counts.items()},
                           video_pid=d.video_pid, video_codec=d.video_codec, pcr_pid=d.pcr_pid,
                           streams={str(k): v for k, v in d.streams.items()})
        for f in (self.f_pcr, self.f_fr, self.f_gap, self.f_sec):
            f.close()
        with open(os.path.join(self.out, f'{self.name}-summary.json'), 'w') as f:
            json.dump(summary, f, indent=1)


def udp_socket(spec):
    host, _, port = spec.rpartition(':')
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    s.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)
    first = int(host.split('.')[0]) if host and host[0].isdigit() else 0
    if 224 <= first <= 239:
        s.bind((host, int(port)))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                     struct.pack('4s4s', socket.inet_aton(host), socket.inet_aton('0.0.0.0')))
    else:
        s.bind((host or '0.0.0.0', int(port)))
    return s


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--udp', action='append', default=[], metavar='NAME=HOST:PORT')
    p.add_argument('--stdin', metavar='NAME')
    p.add_argument('--annexb', action='store_true', help='stdin carries H.264 Annex B, not TS')
    p.add_argument('--out', required=True)
    p.add_argument('--frames', action='store_true', help='log video PES records')
    p.add_argument('--hash', action='store_true', help='hash each video frame (frame matching)')
    p.add_argument('--gap-ms', type=float, default=50.0)
    p.add_argument('--duration', type=float, default=0)
    a = p.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    end = time.monotonic() + a.duration if a.duration else None
    sinks = []
    if a.stdin:
        sink = Sink(a.stdin, a.out, a.frames, a.hash, a.gap_ms, annexb=a.annexb)
        sinks.append(sink)
        fd = sys.stdin.buffer.fileno()
        os.set_blocking(fd, False)
        while not stop and (end is None or time.monotonic() < end):
            r, _, _ = select.select([fd], [], [], 0.2)
            if r:
                try:
                    data = os.read(fd, 1 << 16)
                except BlockingIOError:
                    continue
                if not data:
                    break
                sink.take(data, time.time_ns())
    else:
        socks = {}
        for spec in a.udp:
            name, _, addr = spec.partition('=')
            s = udp_socket(addr)
            socks[s] = Sink(name, a.out, a.frames, a.hash, a.gap_ms)
        sinks = list(socks.values())
        while not stop and (end is None or time.monotonic() < end):
            r, _, _ = select.select(list(socks), [], [], 0.2)
            for s in r:
                while True:
                    try:
                        data, anc, _, _ = s.recvmsg(65536, 64, socket.MSG_DONTWAIT)
                    except BlockingIOError:
                        break
                    socks[s].take(data, tslib.timestamps_cmsg(anc) or time.time_ns())
    for sink in sinks:
        sink.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
