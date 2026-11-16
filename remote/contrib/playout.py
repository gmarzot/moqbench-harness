#!/usr/bin/env python3
"""PCR-locked playout of a TS stream at fixed buffers, as a receiver's output stage.

  playout.py (--stdin | --udp HOST:PORT) --out BUFFER_MS:HOST:PORT [...] [--raw HOST:PORT] --log FILE

Packets get a media time from the PCR (interpolated by bytes between PCRs). The clock anchors on the
first PCR, and once more --settle-s after it on the newest packet, so that a join burst (a relay's cached
group) is played out and the buffer then holds BUFFER at the live edge: a packet of media time m leaves
at anchor + (m - m_anchor) + BUFFER, in 7-packet datagrams. After that a late packet leaves at once and
counts as late; the clock is not re-anchored again except on a PCR discontinuity. --raw forwards every packet on arrival, 7 to a datagram. The log has one JSON line per
second: input bytes, and per output the buffer depth, late and sent datagrams.
"""
import argparse
import collections
import json
import os
import signal
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tslib  # noqa: E402

CHUNK = 7
REANCHOR_S = 1.0


def out_socket():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 << 20)
    return s


def addr(spec):
    host, _, port = spec.rpartition(':')
    return host, int(port)


class Clock:
    """Media time of each packet from the PCRs of the first PID that carries one."""

    def __init__(self):
        self.pid = None
        self.last = None          # (pcr, byte offset)
        self.rate = None          # bytes per 27 MHz tick
        self.offset = 0
        self.anchor = None        # (wall ns, pcr)
        self.reanchors = 0
        self.pending = []

    def media_time(self, pkt, now):
        """(media time or None, list of (packet, media time) now resolved)."""
        resolved = []
        pcr = tslib.pcr_of(pkt) if pkt[3] & 0x20 else None
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        if pcr is not None and self.pid is None:
            self.pid = pid
        if pcr is not None and pid == self.pid:
            if self.last is not None:
                ticks = tslib.pcr_delta(self.last[0], pcr)
                nbytes = self.offset - self.last[1]
                if ticks <= 0 or (self.rate and abs(ticks - nbytes / self.rate) > REANCHOR_S * tslib.PCR_HZ):
                    self.anchor = (now, pcr)
                    self.reanchors += 1
                elif nbytes:
                    self.rate = nbytes / ticks
            else:
                self.anchor = (now, pcr)
            self.last = (pcr, self.offset)
            if self.rate and self.pending:
                base = self.offset
                for p, off in self.pending:
                    resolved.append((p, pcr - (base - off) / self.rate))
                self.pending = []
        m = None
        if self.last is not None and self.rate:
            m = self.last[0] + (self.offset - self.last[1]) / self.rate
        else:
            self.pending.append((pkt, self.offset))
        self.offset += tslib.TS
        return m, resolved

    def due_ns(self, m, buffer_ns):
        wall, pcr = self.anchor
        return wall + int(tslib.pcr_delta(pcr, int(m) % tslib.PCR_WRAP) * 1000 / 27) + buffer_ns


class Output:
    def __init__(self, buffer_ms, dest):
        self.buffer_ns = int(buffer_ms * 1e6)
        self.dest = addr(dest)
        self.sock = out_socket()
        self.queue = collections.deque()
        self.late = self.sent = 0
        self.max_late_ms = 0.0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument('--stdin', action='store_true')
    src.add_argument('--udp', metavar='HOST:PORT')
    p.add_argument('--out', action='append', default=[], metavar='BUFFER_MS:HOST:PORT')
    p.add_argument('--raw', metavar='HOST:PORT')
    p.add_argument('--settle-s', type=float, default=3.0)
    p.add_argument('--log', required=True)
    a = p.parse_args(argv)
    outs = []
    for spec in a.out:
        b, _, dest = spec.partition(':')
        outs.append(Output(float(b), dest))
    raw = (out_socket(), addr(a.raw)) if a.raw else None
    clock = Clock()
    lock = threading.Condition()
    stop = threading.Event()
    stats = dict(rx_bytes=0)
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    def sender():
        while not stop.is_set():
            with lock:
                now = time.time_ns()
                wake = None
                for o in outs:
                    while o.queue and clock.due_ns(o.queue[0][0], o.buffer_ns) <= now:
                        _, data = o.queue.popleft()
                        o.sock.sendto(data, o.dest)
                        o.sent += 1
                    if o.queue:
                        due = clock.due_ns(o.queue[0][0], o.buffer_ns)
                        wake = due if wake is None or due < wake else wake
                timeout = 0.05 if wake is None else max(0.0, min(0.05, (wake - time.time_ns()) / 1e9))
                if timeout > 0.0002:
                    lock.wait(timeout)

    def logger():
        with open(a.log, 'w', buffering=1) as f:
            while not stop.wait(1.0):
                now = time.time_ns()
                with lock:
                    row = dict(t=now / 1e9, rx_bytes=stats['rx_bytes'], reanchors=clock.reanchors, outs={})
                    for o in outs:
                        depth = (clock.due_ns(o.queue[-1][0], o.buffer_ns) - now) / 1e6 if o.queue else 0.0
                        row['outs'][str(o.buffer_ns // 1_000_000)] = dict(
                            depth_ms=round(depth, 2), queued=len(o.queue), late=o.late, sent=o.sent,
                            max_late_ms=round(o.max_late_ms, 2))
                f.write(json.dumps(row) + '\n')

    threading.Thread(target=sender, daemon=True).start()
    threading.Thread(target=logger, daemon=True).start()
    if a.stdin:
        fd = sys.stdin.buffer.fileno()
        reader = lambda: os.read(fd, 1 << 16)  # noqa: E731
    else:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        host, port = addr(a.udp)
        s.bind((host, port))
        if 224 <= int(host.split('.')[0]) <= 239:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                         struct.pack('4s4s', socket.inet_aton(host), socket.inet_aton('0.0.0.0')))
        s.settimeout(0.5)

        def reader():
            try:
                return s.recv(65536)
            except socket.timeout:
                return None
    buf = bytearray()
    chunk, chunk_m, raw_chunk = [], None, []
    settle_at = None
    while not stop.is_set():
        data = reader()
        if data is None:
            continue
        if not data:
            break
        now = time.time_ns()
        stats['rx_bytes'] += len(data)
        buf += data
        pos, n = 0, len(buf)
        ready = []
        while n - pos >= tslib.TS:
            if buf[pos] != 0x47:
                nxt = buf.find(b'\x47', pos + 1)
                pos = nxt if nxt >= 0 else n
                continue
            pkt = bytes(buf[pos:pos + tslib.TS])
            pos += tslib.TS
            if raw is not None:
                raw_chunk.append(pkt)
                if len(raw_chunk) == CHUNK:
                    raw[0].sendto(b''.join(raw_chunk), raw[1])
                    raw_chunk.clear()
            m, resolved = clock.media_time(pkt, now)
            for rp, rm in resolved:
                ready.append((rp, rm))
            if m is not None:
                ready.append((pkt, m))
        del buf[:pos]
        if not ready:
            continue
        with lock:
            if settle_at is None and clock.anchor is not None:
                settle_at = now + int(a.settle_s * 1e9)
            for pkt, m in ready:
                if chunk_m is None:
                    chunk_m = m
                chunk.append(pkt)
                if len(chunk) < CHUNK:
                    continue
                payload = b''.join(chunk)
                if settle_at and now >= settle_at:
                    clock.anchor = (now, int(chunk_m) % tslib.PCR_WRAP)
                    settle_at = 0
                for o in outs:
                    due = clock.due_ns(chunk_m, o.buffer_ns)
                    if due < now and settle_at == 0:
                        o.late += 1
                        o.max_late_ms = max(o.max_late_ms, (now - due) / 1e6)
                    o.queue.append((chunk_m, payload))
                chunk, chunk_m = [], None
            lock.notify()
    stop.set()
    return 0


if __name__ == '__main__':
    sys.exit(main())
