"""MPEG-TS parsing shared by the contribution taps and the playout stage: packets, PSI, PCR, PES and
video access units. Standard library only."""
import hashlib
import struct

TS = 188
NULL_PID = 0x1FFF
PCR_HZ = 27_000_000
PCR_WRAP = (1 << 33) * 300
VIDEO_TYPES = {0x01: 'mpeg1v', 0x02: 'mpeg2v', 0x1B: 'h264', 0x24: 'hevc'}


def pcr_of(pkt):
    """The PCR (27 MHz) of a packet, or None."""
    if not pkt[3] & 0x20 or pkt[4] < 7 or not pkt[5] & 0x10:
        return None
    b = pkt[6:12]
    base = (b[0] << 25) | (b[1] << 17) | (b[2] << 9) | (b[3] << 1) | (b[4] >> 7)
    return base * 300 + (((b[4] & 1) << 8) | b[5])


def pts_of(pes):
    """The PTS (90 kHz) of a PES packet's header, or None."""
    if len(pes) < 14 or pes[0:3] != b'\x00\x00\x01' or not pes[7] & 0x80:
        return None
    p = pes[9:14]
    return ((p[0] >> 1) & 7) << 30 | p[1] << 22 | (p[2] >> 1) << 15 | p[3] << 7 | p[4] >> 1


def pes_payload(pes):
    if len(pes) < 9 or pes[0:3] != b'\x00\x00\x01':
        return b''
    return pes[9 + pes[8]:]


def nal_units(es):
    """NAL unit bodies of an Annex-B byte string."""
    out, i, n = [], es.find(b'\x00\x00\x01'), len(es)
    while 0 <= i < n:
        j = es.find(b'\x00\x00\x01', i + 3)
        body = es[i + 3:j if j >= 0 else n]
        out.append(body.rstrip(b'\x00') if j >= 0 else body)
        i = j
    return [u for u in out if u]


def video_info(codec, es, want_hash=True):
    """(key frame, hash of the coded picture or ''). For H.264 and HEVC the hash covers slice data
    only, so it survives remuxing and parameter sets or delimiters being added or dropped."""
    if codec in ('h264', 'hevc'):
        units = nal_units(es)
        if codec == 'h264':
            vcl = [u for u in units if 1 <= (u[0] & 0x1F) <= 5]
            key = any((u[0] & 0x1F) == 5 for u in units)
        else:
            vcl = [u for u in units if (u[0] >> 1) & 0x3F < 32]
            key = any(16 <= (u[0] >> 1) & 0x3F <= 23 for u in units)
        return key, hashlib.sha1(b''.join(vcl)).hexdigest()[:16] if want_hash else ''
    i = es.find(b'\x00\x00\x01\x00')
    key = i >= 0 and i + 5 < len(es) and (es[i + 5] >> 3) & 7 == 1
    return key, hashlib.sha1(es).hexdigest()[:16] if want_hash else ''


class Demux:
    """Incremental demux of a TS byte stream. Every packet carries the timestamp of the read or
    datagram it came in. Calls on_pcr(pid, pcr, t), on_frame(record) for each completed video PES and
    on_packet(pid, t) for each packet; counts packets, nulls and continuity errors per PID."""

    def __init__(self, on_pcr=None, on_frame=None, frames=False, hashes=False):
        self.on_pcr, self.on_frame = on_pcr, on_frame
        self.frames, self.hashes = frames, hashes
        self.buf = bytearray()
        self.cc, self.counts, self.cc_errors = {}, {}, {}
        self.packets = self.nulls = self.sync_losses = self.tei = 0
        self.pmt_pid, self.video_pid, self.video_codec, self.pcr_pid = None, None, None, None
        self.streams = {}
        self.pes = None

    def feed(self, data, t):
        buf = self.buf
        buf += data
        pos, n = 0, len(buf)
        while n - pos >= TS:
            if buf[pos] != 0x47:
                nxt = buf.find(b'\x47', pos + 1)
                self.sync_losses += 1
                pos = nxt if nxt >= 0 else n
                continue
            self.packet(bytes(buf[pos:pos + TS]), t)
            pos += TS
        del buf[:pos]

    def packet(self, pkt, t):
        self.packets += 1
        if pkt[1] & 0x80:
            self.tei += 1
            return
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        self.counts[pid] = self.counts.get(pid, 0) + 1
        if pid == NULL_PID:
            self.nulls += 1
            return
        afc = (pkt[3] >> 4) & 3
        cc = pkt[3] & 0xF
        disc = afc & 2 and pkt[4] and pkt[5] & 0x80
        broken = False
        if afc & 1:
            last = self.cc.get(pid)
            if last is not None and not disc:
                if cc == last:
                    return
                if cc != (last + 1) & 0xF:
                    self.cc_errors[pid] = self.cc_errors.get(pid, 0) + 1
                    broken = True
            self.cc[pid] = cc
        if afc & 2 and self.on_pcr is not None:
            pcr = pcr_of(pkt)
            if pcr is not None:
                self.on_pcr(pid, pcr, t)
        if not afc & 1:
            return
        off = 4 + (1 + pkt[4] if afc & 2 else 0)
        if off >= TS:
            return
        payload = pkt[off:]
        pusi = pkt[1] & 0x40
        if pid == 0:
            self.pat(payload, pusi)
        elif pid == self.pmt_pid:
            self.pmt(payload, pusi)
        elif self.frames and pid == self.video_pid:
            self.video(payload, pusi, broken, t)

    @staticmethod
    def section(payload, pusi):
        if not pusi or not payload:
            return None
        sec = payload[1 + payload[0]:]
        return sec if len(sec) >= 8 else None

    def pat(self, payload, pusi):
        sec = self.section(payload, pusi)
        if sec is None or sec[0] != 0:
            return
        end = 3 + (((sec[1] & 0x0F) << 8) | sec[2]) - 4
        for i in range(8, min(end, len(sec) - 3), 4):
            if (sec[i] << 8) | sec[i + 1]:
                self.pmt_pid = ((sec[i + 2] & 0x1F) << 8) | sec[i + 3]
                return

    def pmt(self, payload, pusi):
        sec = self.section(payload, pusi)
        if sec is None or sec[0] != 2:
            return
        end = 3 + (((sec[1] & 0x0F) << 8) | sec[2]) - 4
        self.pcr_pid = ((sec[8] & 0x1F) << 8) | sec[9]
        i = 12 + (((sec[10] & 0x0F) << 8) | sec[11])
        while i + 5 <= min(end, len(sec)):
            st, pid = sec[i], ((sec[i + 1] & 0x1F) << 8) | sec[i + 2]
            self.streams[pid] = st
            if st in VIDEO_TYPES and self.video_pid is None:
                self.video_pid, self.video_codec = pid, VIDEO_TYPES[st]
            i += 5 + (((sec[i + 3] & 0x0F) << 8) | sec[i + 4])

    def video(self, payload, pusi, broken, t):
        if pusi:
            self.flush()
            self.pes = [bytearray(payload), t, t, broken]
        elif self.pes is not None:
            self.pes[0] += payload
            self.pes[2] = t
            self.pes[3] = self.pes[3] or broken

    def flush(self):
        if self.pes is None:
            return
        data, t0, t1, broken = self.pes
        self.pes = None
        pts = pts_of(data)
        es = pes_payload(data)
        if self.on_frame is None or pts is None:
            return
        key, digest = video_info(self.video_codec, bytes(es), self.hashes)
        self.on_frame(dict(pts=pts, key=int(key), size=len(es), t_first=t0, t_last=t1, broken=int(broken),
                           hash=digest))


class AnnexB:
    """Access units of a raw H.264 Annex-B byte stream (a receiver's elementary-stream pipe). Each
    unit carries the time of the read that delivered its last byte."""

    def __init__(self, on_frame):
        self.on_frame = on_frame
        self.buf = bytearray()
        self.au, self.au_t, self.au_vcl = [], None, False

    def feed(self, data, t):
        self.buf += data
        buf = self.buf
        i = buf.find(b'\x00\x00\x01')
        while i >= 0:
            j = buf.find(b'\x00\x00\x01', i + 3)
            if j < 0:
                break
            self.nal(bytes(buf[i + 3:j]).rstrip(b'\x00'), t)
            i = j
        if i > 0:
            del buf[:i]
        if self.au:
            self.au_t = t

    def nal(self, nal, t):
        if not nal:
            return
        kind = nal[0] & 0x1F
        new_pic = kind in (1, 5) and len(nal) > 1 and nal[1] & 0x80
        if self.au_vcl and (kind in (6, 7, 8, 9) or new_pic):
            self.end_au()
        self.au.append(nal)
        self.au_t = t
        self.au_vcl = self.au_vcl or kind in (1, 5)

    def close(self):
        """End of stream: the buffered last NAL ends the last access unit."""
        i = self.buf.find(b'\x00\x00\x01')
        if i >= 0:
            self.nal(bytes(self.buf[i + 3:]).rstrip(b'\x00'), self.au_t)
            self.buf.clear()
        self.end_au()

    def end_au(self):
        if not self.au:
            return
        vcl = [u for u in self.au if 1 <= (u[0] & 0x1F) <= 5]
        if vcl:
            key = any((u[0] & 0x1F) == 5 for u in self.au)
            size = sum(len(u) + 4 for u in self.au)
            self.on_frame(dict(pts='', key=int(key), size=size, t_first=self.au_t, t_last=self.au_t, broken=0,
                               hash=hashlib.sha1(b''.join(vcl)).hexdigest()[:16]))
        self.au, self.au_vcl = [], False


def pcr_delta(a, b):
    """b - a in 27 MHz ticks across a wrap."""
    d = (b - a) % PCR_WRAP
    return d - PCR_WRAP if d > PCR_WRAP // 2 else d


def timestamps_cmsg(ancdata):
    """The SO_TIMESTAMPNS kernel receive time in ns from recvmsg ancillary data, or None."""
    for level, kind, data in ancdata:
        if level == 1 and kind == 35 and len(data) >= 16:
            sec, nsec = struct.unpack('qq', data[:16])
            return sec * 1_000_000_000 + nsec
    return None
