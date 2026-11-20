"""Why a contribution run was late: late stretches at each budget D, each with one cause.

Late units are the ones report.late_or_lost counts, grouped into stretches of consecutive units. Each
stretch takes the first cause of CAUSES that fits, with the numbers that decided it:

  outage                   it overlaps an outage, or the recovery after one
  sender-below-media-rate  the sender's WAN egress fell behind the source: its backlog covers FIT of
                           the stretch's median excess delay
  queueing                 one-way WAN delay above its floor covers FIT of that excess
  repair                   WAN losses in the stretch or just before it
  head-of-line             (MoQT) units whose own data was not lost waited behind a repaired loss
  unexplained              nothing fits, or what would decide it was not recorded

Taps, qlog and the clock probe's offsets run on CLOCK_MONOTONIC; pcaps, faults, qdisc and SRT stats on
CLOCK_REALTIME. When the taps stamp CLOCK_REALTIME too (meta wall_clock: two VMs, or a wall-clock lane),
only qlog is moved, by its first Initial against the src pcap. Standard library only.
"""
import bisect
import datetime
import gzip
import json
from pathlib import Path
import re
import statistics
import struct

from contrib import report
from fabric.report import pct, r3

NS = report.NS
MS = 1_000_000
CAUSES = ('outage', 'sender-below-media-rate', 'queueing', 'repair', 'head-of-line', 'unexplained')
SHORT = {'outage': 'outage', 'sender-below-media-rate': 'sender', 'queueing': 'queue', 'repair': 'repair',
         'head-of-line': 'hol', 'unexplained': 'unexpl'}
FIT = 0.5               # share of a stretch's median excess delay a delay cause must account for
BIN_NS = 100 * MS       # egress bins for the sender backlog
ARRIVAL_SLACK = 0.98    # sender backlog arrivals run this much below the source rate, so overhead noise never accrues
RECOVERY_S = 1.0        # an outage's recovery lasts at least this, and at least RECOVERY_RTTS round trips
RECOVERY_RTTS = 10
COARSE_NS = NS // 2     # uncertainty of the source-start clock anchor; widens outage windows
PAD_NS = 50 * MS        # half a qdisc sample period, added around delay windows


# ---------------------------------------------------------------- inputs

def opener(path):
    p = Path(path)
    for cand in (p, p.with_name(p.name + '.gz')):
        if cand.is_file():
            return cand
    return None


def read_bytes(path):
    with (gzip.open(path, 'rb') if str(path).endswith('.gz') else open(path, 'rb')) as f:
        return f.read()


def pcap(path):
    """(t_ns, src ip, dst ip, sport, dport, udp length, payload prefix) of each IPv4/UDP packet of an Ethernet pcap."""
    d = read_bytes(path)
    if len(d) < 24:
        return
    magic = d[:4]
    end = '<' if magic in (b'\xd4\xc3\xb2\xa1', b'\x4d\x3c\xb2\xa1') else '>'
    frac = 1 if magic in (b'\x4d\x3c\xb2\xa1', b'\xa1\xb2\x3c\x4d') else 1000
    hdr = struct.Struct(end + 'IIII')
    i = 24
    while i + 16 <= len(d):
        sec, sub, incl, _ = hdr.unpack_from(d, i)
        i += 16
        pkt = d[i:i + incl]
        i += incl
        if len(pkt) < 42 or pkt[12:14] != b'\x08\x00' or pkt[23] != 17:
            continue
        ihl = (pkt[14] & 15) * 4
        u = 14 + ihl
        if len(pkt) < u + 8:
            continue
        sport, dport, ulen = struct.unpack_from('>HHH', pkt, u)
        yield sec * NS + sub * frac, pkt[26:30], pkt[30:34], sport, dport, ulen, pkt[u + 8:]


def ip4(s):
    return bytes(int(x) for x in s.split('.'))


QLOG_REF = re.compile(r'"reference_time"\s*:\s*"?(\d+)')
QLOG_UNITS = re.compile(r'"time_units"\s*:\s*"(\w+)"')
QLOG_TYPE = re.compile(r'"vantage_point"\s*:\s*\{[^}]*"type"\s*:\s*"(\w+)"')
QLOG_PEER = {'client': re.compile(r'"datagram_sent",\s*\{[^}]*"addr_to"\s*:\s*\{\s*"ip_v4"\s*:\s*"([\d.]+)"'),
             'server': re.compile(r'"datagram_received",\s*\{[^}]*?"addr_from"\s*:\s*\{\s*"ip_v4"\s*:\s*"([\d.]+)"')}


def qlog_head(text):
    """(reference time ns, vantage type, peer ip, ns per time unit) of a picoquic qlog."""
    ref, units, vt = QLOG_REF.search(text), QLOG_UNITS.search(text[:4096]), QLOG_TYPE.search(text[:4096])
    if not ref or not vt:
        return None
    scale = 1000 if not units or units[1] == 'us' else MS
    peer = QLOG_PEER.get(vt[1])
    m = peer.search(text, 0, 1 << 20) if peer else None
    return int(ref[1]) * scale, vt[1], m[1] if m else None, scale


def qlog_events(text):
    """The events of a qlog's first trace, as far as it parses: a truncated file keeps what was written."""
    i = text.find('"events"')
    i = text.find('[', i) + 1 if i >= 0 else 0
    if not i:
        return
    dec = json.JSONDecoder()
    n = len(text)
    while i < n:
        while i < n and text[i] in ' \t\r\n,':
            i += 1
        if i >= n or text[i] == ']':
            return
        try:
            ev, i = dec.raw_decode(text, i)
        except ValueError:
            return
        if isinstance(ev, list) and len(ev) >= 4:
            yield ev


class Conn:
    """One QUIC connection's sender-side view from its qlog, on CLOCK_MONOTONIC ns."""

    def __init__(self, path, text, head):
        self.path = path
        self.ref, self.vantage, self.peer, scale = head
        self.sent = []       # (t, pn, size, [(stream, offset, length)])
        self.lost = []       # (t, pn)
        self.metrics = []    # (t, cwnd, bytes_in_flight, pacing_rate, smoothed_rtt), carried forward
        self.cc_changes = []
        self.initial_t = None
        last = dict(cwnd=None, bytes_in_flight=None, pacing_rate=None, smoothed_rtt=None)
        for rel, _, name, data in qlog_events(text):
            t = self.ref + int(rel) * scale
            if name == 'packet_sent':
                frames = [(f['id'], f.get('offset', 0), f.get('length', 0)) for f in data.get('frames') or ()
                          if f.get('frame_type') == 'stream']
                h = data.get('header') or {}
                self.sent.append((t, h.get('packet_number'), h.get('packet_size', 0), frames))
                if self.initial_t is None and data.get('packet_type') == 'initial':
                    self.initial_t = t
            elif name == 'packet_lost':
                self.lost.append((t, data.get('packet_number')))
            elif name == 'metrics_updated':
                last.update({k: v for k, v in data.items() if k in last})
                self.metrics.append((t, last['cwnd'], last['bytes_in_flight'], last['pacing_rate'], last['smoothed_rtt']))
            elif name == 'congestion_state_updated':
                self.cc_changes.append(t)
        self.sent.sort(key=lambda s: s[0])
        self.span = (self.sent[0][0], self.sent[-1][0]) if self.sent else None

    def shift(self, d):
        """Move every time by d ns."""
        self.ref += d
        self.sent = [(t + d, *rest) for t, *rest in self.sent]
        self.lost = [(t + d, pn) for t, pn in self.lost]
        self.metrics = [(t + d, *rest) for t, *rest in self.metrics]
        self.cc_changes = [t + d for t in self.cc_changes]
        if self.initial_t is not None:
            self.initial_t += d
        if self.span:
            self.span = (self.span[0] + d, self.span[1] + d)

    def covers(self, t0, t1):
        return self.span is not None and self.span[0] <= t0 and t1 <= self.span[1]

    def losses(self):
        """(first send, lost at, resend or None, stream) per lost packet that carried stream data."""
        by_pn = {s[1]: s for s in self.sent}
        streams = {}
        for t, _, _, frames in self.sent:
            for sid, off, ln in frames:
                streams.setdefault(sid, []).append((t, off, ln))
        out = []
        for t_lost, pn in self.lost:
            s = by_pn.get(pn)
            if not s or not s[3]:
                continue
            sid, off, _ = max(s[3], key=lambda f: f[2])
            sends = streams[sid]
            k = bisect.bisect_right(sends, (s[0], float('inf'), 0))
            resend = next((t for t, o, ln in sends[k:] if o <= off < o + max(ln, 1)), None)
            out.append((s[0], t_lost, resend, sid))
        return out


def sender_conns(run, meta):
    """Connections whose qlog shows them sending from the src side across the WAN (peer is a dst address)."""
    dst_ips = {ln['dst_ip'] for ln in meta['topology']['links']}
    out = []
    for p in sorted(run.glob('qlog-*/*.qlog*')):
        try:
            text = read_bytes(p).decode('utf-8', 'replace')
        except (OSError, EOFError):
            continue
        head = qlog_head(text)
        if head and head[2] in dst_ips:
            out.append(Conn(p, text, head))
    return out


def srt_stats(run, side):
    """(epoch ns, record) of SRT's per-interval statistics."""
    out = []
    for r in report.jsonl(run / f'srt-{side}.json'):
        try:
            t = datetime.datetime.strptime(r['timepoint'], '%Y-%m-%dT%H:%M:%S.%f%z').timestamp()
        except (KeyError, ValueError):
            continue
        out.append((int(t * NS), r))
    return out


# ---------------------------------------------------------------- clocks

class Clock:
    """CLOCK_REALTIME minus CLOCK_MONOTONIC: one anchor, corrected by the wall-clock steps the clock probe logged."""

    def __init__(self, offset, at_mono, method, steps=()):
        self.offset, self.at, self.method = offset, at_mono, method
        self.steps = sorted((t - offset, int(ms * MS)) for t, ms in steps)
        self.coarse = method == 'source-start'

    def off(self, mono):
        o = self.offset
        for t, d in self.steps:
            if self.at < t <= mono:
                o += d
            elif mono < t <= self.at:
                o -= d
        return o

    def mono(self, epoch):
        return epoch - self.off(epoch - self.offset)

    def epoch(self, mono):
        return mono + self.off(mono)


def clock_steps(run):
    return [(int(r['t_ns']), float(r['ms'])) for r in report.rows(run / 'clock-src.csv') if r.get('kind') == 'step']


def anchor_qlog(conns, src_pkts, meta):
    """A sender connection's first Initial in its qlog against the same packet in the src pcap."""
    dst_ips = {ip4(ln['dst_ip']) for ln in meta['topology']['links']}
    first = next((t for t, _, d, _, _, _, pay in src_pkts if d in dst_ips and pay and pay[0] & 0xF0 == 0xC0), None)
    t_q = min((c.initial_t for c in conns if c.initial_t is not None), default=None)
    return (first - t_q, t_q) if first is not None and t_q is not None else None


def anchor_pcr(src_pkts, ins):
    """SRT carries TS in the clear: a PCR at the front of a datagram leaves the sender just after the input tap saw it."""
    by_pcr = {pcr: t for (_, pcr), t in ins.items()}
    best = None
    for t, _, _, _, _, _, pay in src_pkts:
        ts = pay[16:]
        if len(ts) < 12 or pay[0] & 0x80 or ts[0] != 0x47 or not ts[3] & 0x20 or ts[4] < 7 or not ts[5] & 0x10:
            continue
        b = ts[6:12]
        pcr = ((b[0] << 25) | (b[1] << 17) | (b[2] << 9) | (b[3] << 1) | (b[4] >> 7)) * 300 + (((b[4] & 1) << 8) | b[5])
        if pcr in by_pcr and (best is None or t - by_pcr[pcr] < best[0]):
            best = (t - by_pcr[pcr], by_pcr[pcr])
    return best


def anchor_arrival(run):
    """sub_media's wall-clock arrival log against the raw tap's monotonic time of the same PCR."""
    raw = {pcr: t for (_, pcr), t in report.pcrs(run, 'raw').items()}
    best = None
    for p in sorted(run.glob('arrival-*.jsonl')):
        for r in report.jsonl(p):
            t = raw.get(r.get('pcr'))
            if t is not None and r.get('arrival_ns') and (best is None or r['arrival_ns'] - t > best[0]):
                best = (r['arrival_ns'] - t, t)
    return best


# ---------------------------------------------------------------- units

def scored_units(run, meta):
    """(tap, units, anchor delay ns, the summary's late-or-lost) as report.analyze scores the run's main tap."""
    w0, w1, _ = report.window(run, meta)
    if w0 is None:
        return None
    try:
        summary = json.loads((run / 'summary.json').read_text())
    except (OSError, ValueError):
        summary = {}
    if meta['lane'] == 'moqt-aiomoqt-loc' and 'raw' not in meta.get('taps', []):
        ins = report.frames(run, 'in', 'hash')
        outs = report.frame_log(run) if list(run.glob('frames-*.jsonl')) else report.frames(run, 'raw', 'hash')

        def unit_list(lo):
            return sorted(((f['t_first'], outs[k]['t_last'] if k in outs else None) for k, f in ins.items()
                           if lo <= f['t_first'] < w1), key=lambda x: x[0])
        units, first = unit_list(w0), report.live_anchor(unit_list(float('-inf')))
        tap, scored = 'frames', (summary.get('frames') or {}).get('late_or_lost_pct')
    else:
        taps = meta.get('taps', [])
        tap = 'raw' if 'raw' in taps else (taps[0] if taps else None)
        if tap is None:
            return None
        ins, outs = report.pcrs(run, 'in'), report.pcrs(run, tap)
        units = report.match_units(ins, outs, w0, w1)
        first = report.live_anchor(report.match_units(ins, outs, 0, w1))
        scored = ((summary.get('outputs') or {}).get(tap) or {}).get('late_or_lost_pct')
    if not units or not first:
        return None
    return tap, units, first[1] - first[0], scored, (w0, w1)


def late_index(units, anchor_ns, d):
    """Indices of the units late or lost at D, by report.late_or_lost itself."""
    return [i for i, u in enumerate(units) if report.late_or_lost([u], anchor_ns, [d])[str(d)]]


def stretches_of(idx):
    out = []
    for i in idx:
        if out and out[-1][1] == i - 1:
            out[-1][1] = i
        else:
            out.append([i, i])
    return out


# ---------------------------------------------------------------- evidence

class Evidence:
    """Everything a run recorded that can decide a cause, on CLOCK_MONOTONIC."""

    def __init__(self, run, meta, units, window):
        self.run, self.meta, self.units, self.window = run, meta, units, window
        self.moqt = meta['lane'].startswith('moqt')
        self.rtt_ns = int(meta.get('rtt_ms', 0) * MS)
        self.present, self.absent = [], []
        links = meta['topology']['links']
        self.src_ips = {ip4(ln['src_ip']) for ln in links}
        self.dst_ips = {ip4(ln['dst_ip']) for ln in links}
        src_pkts, dst_pkts = [], []
        for ln in links:
            for side, acc in (('src', src_pkts), ('dst', dst_pkts)):
                p = opener(run / f'{side}{ln["k"]}.pcap')
                if p:
                    acc.extend(x for x in pcap(p) if x[1] in self.src_ips and x[2] in self.dst_ips)
                    self.present.append(p.name)
                else:
                    self.absent.append(f'{side}{ln["k"]}.pcap')
        src_pkts.sort(key=lambda x: x[0])
        dst_pkts.sort(key=lambda x: x[0])
        self.conns = sender_conns(run, meta) if self.moqt else []
        if self.moqt:
            (self.present if self.conns else self.absent).append('qlog (WAN sender)')
        self.clock = self.make_clock(src_pkts)
        self.source_bps = self.source_rate()
        self.match(src_pkts, dst_pkts)
        self.lag = self.backlog(src_pkts)
        self.qdisc = self.qdisc_delay()
        self.outages = self.outage_windows()
        self.losses = self.wan_losses(src_pkts, dst_pkts)
        self.srt = {side: [(self.clock.mono(t), r) for t, r in srt_stats(run, side)] if self.clock else []
                    for side in ('snd', 'rcv')} if meta['lane'] == 'srt' else {}
        if meta['lane'] == 'srt':
            (self.present if self.srt.get('snd') or self.srt.get('rcv') else self.absent).append('srt stats')
        self.timeline = self.stream_timeline()
        self.in_t = [u[0] for u in units]

    def make_clock(self, src_pkts):
        # Offset from the sender's first Initial (qlog vs src pcap), else SRT PCRs in the src pcap, else the arrival log.
        if self.meta.get('wall_clock'):
            a = anchor_qlog(self.conns, src_pkts, self.meta) if self.conns and src_pkts else None
            for c in self.conns if a else ():
                c.shift(a[0])
            if self.conns and not a:
                self.conns = []
                self.absent.append('qlog clock anchor')
            return Clock(0, 0, 'wall-clock')
        steps = clock_steps(self.run)
        if self.conns and src_pkts:
            a = anchor_qlog(self.conns, src_pkts, self.meta)
            if a:
                return Clock(a[0], a[1], 'qlog-initial', steps)
        if self.meta['lane'] == 'srt' and src_pkts:
            a = anchor_pcr(src_pkts, report.pcrs(self.run, 'in'))
            if a:
                return Clock(a[0], a[1], 'srt-pcr', steps)
        a = anchor_arrival(self.run)
        if a:
            return Clock(a[0], a[1], 'arrival-log', steps)
        first = (json.loads((self.run / 'in-summary.json').read_text()) if (self.run / 'in-summary.json').is_file()
                 else {}).get('first_ns')
        if self.meta.get('source_started') and first:
            self.absent.append('precise clock anchor')
            return Clock(int(self.meta['source_started'] * NS) - first, first, 'source-start', steps)
        self.absent.append('clock anchor')
        return None

    def source_rate(self):
        """Source bytes per ns from the input tap."""
        try:
            s = json.loads((self.run / 'in-summary.json').read_text())
            return s['bytes'] / (s['last_ns'] - s['first_ns'])
        except (OSError, ValueError, KeyError, ZeroDivisionError):
            mux = (self.meta.get('source') or {}).get('mux_bps')
            return mux / 8 / NS if mux else None

    def match(self, src_pkts, dst_pkts):
        """One-way delay of each forward packet seen at both ends, matched on its captured payload prefix."""
        self.owd, self.drops = [], []
        if not src_pkts or not dst_pkts or not self.clock:
            return
        n = min(len(p[6]) for p in src_pkts[:1000] + dst_pkts[:1000])
        seen = {}
        for t, _, _, sp, dp, ln, pay in dst_pkts:
            seen.setdefault((sp, dp, ln, pay[:n]), t)
        lo, hi = dst_pkts[0][0], dst_pkts[-1][0]
        for t, _, _, sp, dp, ln, pay in src_pkts:
            t_dst = seen.get((sp, dp, ln, pay[:n]))
            if t_dst is not None and t_dst >= t:
                self.owd.append((self.clock.mono(t), t_dst - t))
            elif lo <= t <= hi - NS:
                self.drops.append(self.clock.mono(t))
        if self.owd:
            self.owd_floor = pct([d for _, d in self.owd], 1)
            self.owd_t = [t for t, _ in self.owd]

    def backlog(self, src_pkts):
        """Sender backlog (ns of media) per BIN_NS bin: arrivals at the source rate, departures as WAN egress (src
        pcap, else qlog bytes sent), overhead taken as the median ratio of egress to source over 1 s."""
        if not self.source_bps:
            return None
        w0, w1 = self.window
        if src_pkts and self.clock:
            dep, how = [(self.clock.mono(t), ln) for t, _, _, _, _, ln, _ in src_pkts], 'src pcap'
        elif self.conns and any(c.covers(w0 + NS, w1 - NS) for c in self.conns):
            dep, how = [(t, size) for c in self.conns for t, _, size, _ in c.sent], 'qlog bytes sent'
        else:
            return None
        nb = int((w1 - w0) // BIN_NS) + 1
        e = [0] * nb
        for t, ln in dep:
            if w0 <= t < w1:
                e[int((t - w0) // BIN_NS)] += ln
        per_s = NS // BIN_NS
        ratios = [sum(e[i:i + per_s]) / (self.source_bps * NS) for i in range(0, nb - per_s, per_s)]
        k = statistics.median(ratios) if ratios else 1.0
        if k <= 0:
            return None
        a = ARRIVAL_SLACK * k * self.source_bps * BIN_NS
        b, lag = 0.0, []
        for x in e:
            lag.append(b / (k * self.source_bps))
            b = max(0.0, b + a - x)
        return dict(lag_ns=lag, overhead=k, source=how, egress=e)

    def qdisc_delay(self):
        """Bottleneck queueing delay per sample: the tbf's (and its AQM child's) backlog at the tbf rate."""
        out = []
        recs = [r for r in report.jsonl(self.run / 'qdisc-wan.jsonl') if r.get('name') == 'fwd']
        if not recs:
            self.absent.append('qdisc-wan.jsonl')
            return out
        self.present.append('qdisc-wan.jsonl')
        if not self.clock:
            return out
        for r in recs:
            try:
                qs = json.loads(r['out'])
            except (ValueError, TypeError):
                continue
            tbf = next((q for q in qs if q.get('kind') == 'tbf'), None)
            if not tbf or not (tbf.get('options') or {}).get('rate'):
                continue
            backlog = sum(q.get('backlog') or 0 for q in qs if q.get('handle') in ('20:', '30:'))
            out.append((self.clock.mono(int(r['t'] * NS)), backlog / tbf['options']['rate'] * 1000))
        return out

    def outage_windows(self):
        """(on, off, direction) of each applied outage."""
        faults = report.jsonl(self.run / 'faults.jsonl')
        planned = [e for e in self.meta.get('events_planned') or [] if e.get('action', '').startswith('outage')]
        if not (self.run / 'faults.jsonl').is_file():
            if planned:
                self.absent.append('faults.jsonl')
            return None if planned else []
        self.present.append('faults.jsonl')
        if not self.clock:
            return None if planned else []
        out, open_ = [], {}
        for f in faults:
            if f.get('action') == 'outage_on':
                open_[f.get('name')] = (f.get('t0') or f.get('t1'), f.get('direction'))
            elif f.get('action') == 'outage_off' and f.get('name') in open_:
                on, d = open_.pop(f['name'])
                out.append((self.clock.mono(int(on * NS)), self.clock.mono(int((f.get('t1') or f['t0']) * NS)), d))
        out += [(self.clock.mono(int(on * NS)), self.window[1], d) for on, d in open_.values()]
        return sorted(out)

    def wan_losses(self, src_pkts, dst_pkts):
        """Forward WAN losses, (first send, repair send or None, repair ms or None, stream or None), by source."""
        out = {}
        if self.meta['lane'] == 'srt' and src_pkts and dst_pkts and self.clock:
            out['pcap srt'] = self.srt_losses(src_pkts, dst_pkts)
        elif self.owd:
            out['pcap'] = sorted((t, None, None, None) for t in self.drops)
        for c in self.conns:
            out.setdefault('qlog', []).extend((t, rs, (rs - t) / MS if rs else None, sid) for t, _, rs, sid in c.losses())
        return out

    def losses_at(self, t0, t1):
        """(source, losses first sent in [t0, t1]); qlog only where its per-packet events cover the span."""
        if 'qlog' in self.losses and any(c.covers(t0, t1) for c in self.conns):
            src = 'qlog'
        else:
            src = next((k for k in self.losses if k != 'qlog'), None)
        if src is None:
            return None, None
        return src, [x for x in self.losses[src] if t0 <= x[0] <= t1]

    def srt_losses(self, src_pkts, dst_pkts):
        """Losses by SRT sequence number: a first copy that never reached dst; repair runs from its due arrival to
        the first retransmitted copy (R flag) at dst."""
        def data(pay):
            if len(pay) < 16 or pay[0] & 0x80:
                return None
            seq, word = struct.unpack_from('>II', pay)
            return seq & 0x7FFFFFFF, (word >> 26) & 1
        first, got = {}, {}
        for t, _, _, _, _, _, pay in src_pkts:
            d = data(pay)
            if d and not d[1]:
                first.setdefault(d[0], t)
        for t, _, _, _, _, _, pay in dst_pkts:
            d = data(pay)
            if d:
                got.setdefault(d[0], []).append((t, d[1]))
        floor = getattr(self, 'owd_floor', self.rtt_ns // 2)
        out = []
        for seq, t in first.items():
            arr = got.get(seq, [])
            if any(not r for _, r in arr):
                continue
            rep = min((ta for ta, r in arr if r), default=None)
            out.append((self.clock.mono(t), self.clock.mono(rep - floor) if rep else None,
                        (rep - t - floor) / MS if rep else None, None))
        return sorted(out)

    def stream_timeline(self):
        """(first send, stream) of every packet carrying a large stream frame, from the sender qlogs."""
        tl = sorted((t, max(fr, key=lambda f: f[2])[0]) for c in self.conns for t, _, _, fr in c.sent
                    if fr and max(f[2] for f in fr) >= 1000)
        self.tl_t = [t for t, _ in tl]
        return tl

    # ---- per stretch

    def lag_at(self, t0, t1):
        if not self.lag:
            return None
        w0 = self.window[0]
        lo, hi = max(0, int((t0 - w0) // BIN_NS)), int((t1 - w0) // BIN_NS)
        vals = self.lag['lag_ns'][lo:hi + 1]
        return max(vals) if vals else None

    def egress_ratio(self, t0, t1):
        if not self.lag:
            return None
        w0 = self.window[0]
        lo, hi = max(0, int((t0 - w0) // BIN_NS)), int((t1 - w0) // BIN_NS) + 1
        e = self.lag['egress'][lo:hi]
        return r3(sum(e) / (self.lag['overhead'] * self.source_bps * BIN_NS * len(e))) if e else None

    def queue_at(self, t0, t1):
        """(max, p50 excess one-way delay ms, source) over [t0, t1] on the sender's clock."""
        if self.owd:
            i, j = bisect.bisect_left(self.owd_t, t0 - PAD_NS), bisect.bisect_right(self.owd_t, t1 + PAD_NS)
            ex = [(d - self.owd_floor) / MS for _, d in self.owd[i:j]]
            if ex:
                return r3(max(ex)), r3(pct(ex, 50)), 'pcap one-way delay'
        if self.qdisc:
            ex = [d for t, d in self.qdisc if t0 - PAD_NS <= t <= t1 + PAD_NS]
            if ex:
                return r3(max(ex)), r3(pct(ex, 50)), 'tbf backlog'
        return None

    def qlog_state(self, t0, t1):
        rows = [m for c in self.conns for m in c.metrics if t0 <= m[0] <= t1]
        if not rows:
            return None
        cw = [m[1] for m in rows if m[1]]
        bif = [m[2] for m in rows if m[2] is not None]
        pace = [m[3] for m in rows if m[3]]
        rtt = [m[4] for m in rows if m[4]]
        limited = [m for m in rows if m[1] and m[2] is not None]
        return dict(cwnd_min=min(cw) if cw else None, cwnd_max=max(cw) if cw else None,
                    bytes_in_flight_max=max(bif) if bif else None,
                    cwnd_limited_share=r3(sum(1 for m in limited if m[2] >= 0.9 * m[1]) / len(limited)) if limited else None,
                    pacing_mbit_min=r3(min(pace) * 8 / 1e6) if pace else None,
                    srtt_ms_max=r3(max(rtt) / 1000) if rtt else None,
                    cc_state_changes=sum(1 for c in self.conns for t in c.cc_changes if t0 <= t <= t1))

    def srt_window(self, t0, t1):
        snd = [r for t, r in self.srt.get('snd', []) if t0 - NS <= t <= t1 + NS]
        rcv = [r for t, r in self.srt.get('rcv', []) if t0 - NS <= t <= t1 + NS]
        if not snd and not rcv:
            return None

        def tot(rs, sect, k):
            return sum((r.get(sect) or {}).get(k) or 0 for r in rs)
        return dict(snd_ms_buf_max=max(((r.get('send') or {}).get('msBuf') or 0 for r in snd), default=None),
                    snd_retransmitted=tot(snd, 'send', 'packetsRetransmitted'), rcv_lost=tot(rcv, 'recv', 'packetsLost'),
                    rcv_dropped=tot(rcv, 'recv', 'packetsDropped'),
                    rtt_ms_max=max((r['link']['rtt'] for r in snd + rcv if isinstance(r.get('link'), dict)
                                    and 'rtt' in r['link']), default=None))

    def unit_stream(self, t):
        k = bisect.bisect_left(self.tl_t, t)
        return self.timeline[k][1] if k < len(self.timeline) else None

    def classify(self, a, b, d):
        """(cause, evidence) of the stretch units[a..b] late at budget d."""
        units = self.units[a:b + 1]
        t0, t1 = units[0][0], units[-1][0]
        anchor = self.anchor
        excess = sorted((o - i - anchor) / MS if o is not None else float(d) for i, o in units)
        need = FIT * statistics.median(excess)
        ev = dict(excess_ms_p50=r3(statistics.median(excess)), excess_ms_max=r3(excess[-1]),
                  lost_units=sum(1 for _, o in units if o is None))
        undecided = []

        # outage
        if self.outages is None:
            undecided.append('outage')
        else:
            rec = max(int(RECOVERY_S * NS), RECOVERY_RTTS * self.rtt_ns)
            wide = COARSE_NS if self.clock and self.clock.coarse else 0
            for on, off, direction in self.outages:
                if t0 <= off + rec + wide and t1 >= on - d * MS - wide:
                    ev.update(outage_ms=r3((off - on) / MS), outage_direction=direction,
                              stretch_from_outage_ms=r3((t0 - on) / MS), recovery_ms=r3(rec / MS),
                              clock_anchor=self.clock.method)
                    return 'outage', ev

        # sender below media rate
        lag = self.lag_at(t0, t1)
        st = self.qlog_state(t0 - NS, t1) if self.conns else None
        sw = self.srt_window(t0, t1) if self.srt else None
        if st:
            ev['qlog'] = st
        if sw:
            ev['srt'] = sw
        if lag is None:
            undecided.append('sender-below-media-rate')
        else:
            ev.update(sender_backlog_ms=r3(lag / MS), egress_vs_source=self.egress_ratio(t0 - NS, t1),
                      egress_from=self.lag['source'])
            if lag / MS >= need:
                return 'sender-below-media-rate', ev
        lag = lag or 0

        # queueing: data input at t leaves the sender about lag later
        q = self.queue_at(t0 + lag, t1 + lag)
        if q is None:
            undecided.append('queueing')
        else:
            ev.update(queue_ms_max=q[0], queue_ms_p50=q[1], queue_from=q[2])
            if q[0] >= need:
                return 'queueing', ev

        # repair
        look = d * MS + 2 * self.rtt_ns
        src, near = self.losses_at(t0 + lag - look, t1 + lag + PAD_NS)
        if src is None:
            undecided.append('repair')
            if self.moqt:
                undecided.append('head-of-line')
        else:
            ev['losses_from'] = src
            if near:
                reps = [x[2] for x in near if x[2] is not None]
                ev.update(wan_losses=len(near), repaired=sum(1 for x in near if x[1] is not None),
                          repair_ms_p50=r3(pct(reps, 50)) if reps else None, repair_ms_max=r3(max(reps)) if reps else None)
                if not self.moqt or src != 'qlog':
                    if self.moqt:
                        ev['undecided'] = ['head-of-line']
                    return 'repair', ev
                own, hol = self.split_hol(a, b, near)
                ev.update(own_data_lost_units=own, waited_behind_loss_units=hol)
                if own >= hol:
                    return 'repair', ev
                return 'head-of-line', ev
            ev['wan_losses'] = 0
        if undecided:
            ev['undecided'] = undecided
        ev['unaccounted_ms'] = r3(statistics.median(excess) - (ev.get('sender_backlog_ms') or 0) - (ev.get('queue_ms_max') or 0))
        return 'unexplained', ev

    def split_hol(self, a, b, near):
        """(units whose own data was lost, units that waited behind a repaired loss in their stream)."""
        # A lost packet holds the unit input just before its send; later same-stream units input before its repair waited.
        floor = getattr(self, 'owd_floor', self.rtt_ns // 2)
        owners = {}
        for t_sent, resend, _, sid in near:
            k = bisect.bisect_right(self.in_t, t_sent) - 1
            if k >= 0:
                owners.setdefault(k, []).append((sid, resend + floor if resend else None))
        own = hol = 0
        for i in range(a, b + 1):
            if i in owners:
                own += 1
                continue
            sid = self.unit_stream(self.in_t[i])
            if any(k < i and s == sid and (arr is None or self.in_t[i] < arr)
                   for k, ls in owners.items() for s, arr in ls):
                hol += 1
        return own, hol


# ---------------------------------------------------------------- run and folder

def explain_run(run_dir):
    run = Path(run_dir)
    meta = json.loads((run / 'meta.json').read_text())
    out = dict(label=meta['label'], lane=meta['lane'], profile=meta.get('profile_id'), variant=meta.get('variant'),
               repeat=meta.get('repeat'), budgets_ms=meta.get('budgets_ms'))
    scored = scored_units(run, meta)
    if not scored:
        out['error'] = 'no scored units'
        return out
    tap, units, anchor, summary_pct, window = scored
    budgets = meta.get('budgets_ms') or sorted(int(k) for k in (summary_pct or {}))
    ev = Evidence(run, meta, units, window)
    ev.anchor = anchor
    out.update(tap=tap, units=len(units), anchor_delay_ms=r3(anchor / MS), budgets_ms=budgets,
               inputs=dict(present=sorted(set(ev.present)), absent=sorted(set(ev.absent))),
               clock=dict(anchor=ev.clock.method, offset_ns=ev.clock.offset,
                          steps=len(ev.clock.steps)) if ev.clock else None)
    if ev.lag:
        out['sender_overhead'] = r3(ev.lag['overhead'])
    if ev.owd:
        out['owd_floor_ms'] = r3(ev.owd_floor / MS)
    totals, stretches = {}, []
    w0 = window[0]
    expected = report.late_or_lost(units, anchor, budgets)
    for d in budgets:
        idx = late_index(units, anchor, d)
        by = dict.fromkeys(CAUSES, 0)
        for a, b in stretches_of(idx):
            cause, evidence = ev.classify(a, b, d)
            by[cause] += b - a + 1
            stretches.append(dict(D=d, start_s=r3((units[a][0] - w0) / NS), end_s=r3((units[b][0] - w0) / NS),
                                  units=b - a + 1, cause=cause, evidence=evidence))
        pct_ = r3(100 * len(idx) / len(units))
        totals[str(d)] = dict(late=len(idx), pct=pct_, by_cause={k: v for k, v in by.items() if v},
                              agrees=pct_ == expected[str(d)] and (summary_pct is None or summary_pct.get(str(d)) == pct_))
        if summary_pct is not None:
            totals[str(d)]['summary_pct'] = summary_pct.get(str(d))
    out['totals'] = totals
    out['stretches'] = stretches
    return out


def arm(e):
    v = {k: x for k, x in (e.get('variant') or {}).items() if k != 'D'}
    return e['lane'] + (' ' + ','.join(f'{k}={x}' for k, x in v.items()) if v else '')


def folder_md(explained, title):
    """explain.md for a results folder: per profile, lanes and arms by D, and where SRT beat a MoQT arm."""
    lines = [f'# Late-or-lost by cause: {title}', '',
             'Each cell: late-or-lost % at D, then the share of it by cause (% of the late units).', '']
    good = [e for e in explained if 'totals' in e]
    for prof in sorted({e['profile'] for e in good}):
        runs = [e for e in good if e['profile'] == prof]
        budgets = sorted({int(d) for e in runs for d in e['totals']})
        arms = {}
        for e in runs:
            key = arm(e) + (f" D={e['variant']['D']}" if 'D' in (e.get('variant') or {}) else '')
            arms.setdefault(key, []).append(e)
        lines += [f'## {prof}', '', '| arm | runs | ' + ' | '.join(f'D={d}' for d in budgets) + ' |',
                  '|---|---|' + '---|' * len(budgets)]
        agg = {}
        for key, es in sorted(arms.items()):
            cells = []
            for d in budgets:
                n = sum(e['units'] for e in es if str(d) in e['totals'])
                late = sum(e['totals'][str(d)]['late'] for e in es if str(d) in e['totals'])
                by = {}
                for e in es:
                    for c, k in (e['totals'].get(str(d)) or {}).get('by_cause', {}).items():
                        by[c] = by.get(c, 0) + k
                agg[(key, d)] = (late, n, by)
                cells.append(cell(late, n, by))
            lines.append(f'| {key} | {len(es)} | ' + ' | '.join(cells) + ' |')
        lines.append('')
        diff = compare(arms, agg, budgets)
        if diff:
            lines += diff + ['']
        notes = sorted({a for e in runs for a in e['inputs']['absent']})
        if notes:
            lines += [f"Inputs absent in some runs: {', '.join(notes)}.", '']
    bad = [e for e in explained if 'totals' not in e]
    if bad:
        lines += ['Not explained: ' + ', '.join(f"{e['label']} ({e.get('error')})" for e in bad), '']
    return '\n'.join(lines)


def cell(late, n, by):
    if not n:
        return '—'
    parts = ', '.join(f'{SHORT[c]} {share(k, late)}' for c, k in sorted(by.items(), key=lambda x: -x[1]) if k)
    return f'{100 * late / n:.1f}' + (f' ({parts})' if parts else '')


def share(k, n):
    v = 100 * k / n
    return '<1' if 0 < v < 1 else str(round(v))


def compare(arms, agg, budgets):
    """Where the SRT run at latency D has fewer late units than a MoQT arm at D."""
    out = []
    srt = {d: k for k in arms for d in budgets if k == f'srt D={d}'}
    moq = [k for k, es in arms.items() if es[0]['lane'].startswith('moqt')]
    missing = [d for d in budgets if d not in srt]
    for d in budgets:
        if d not in srt:
            continue
        s_late, s_n, s_by = agg[(srt[d], d)]
        for k in moq:
            m_late, m_n, m_by = agg[(k, d)]
            if not m_n or not s_n or s_late / s_n >= m_late / m_n:
                continue
            extra = {c: m_by.get(c, 0) - s_by.get(c, 0) for c in CAUSES}
            top = [f'{c} {v:+d}' for c, v in sorted(extra.items(), key=lambda x: -x[1]) if v > 0][:3]
            out.append(f'- D={d}: SRT {100 * s_late / s_n:.1f}% vs {k} {100 * m_late / m_n:.1f}% '
                       f'({m_late - s_late:+d} late units, {100 * (m_late / m_n - s_late / s_n):+.1f} pts); '
                       f"MoQT's dominant causes: {', '.join(top) or 'none above SRT'} "
                       f'(SRT: {fmt_by(s_by)}; {k}: {fmt_by(m_by)})')
    if moq and missing and srt:
        out.append(f"- No SRT run at latency D for D={', '.join(map(str, missing))}.")
    elif moq and not srt:
        out.append('- No SRT run at any of these D.')
    return out


def fmt_by(by):
    return ', '.join(f'{SHORT[c]} {k}' for c, k in sorted(by.items(), key=lambda x: -x[1]) if k) or 'none'


def line(e):
    if 'totals' not in e:
        return f"{e['label']:<34} {e.get('error')}"
    parts = []
    for d, t in e['totals'].items():
        top = max(t['by_cause'].items(), key=lambda x: x[1])[0] if t['by_cause'] else '-'
        parts.append(f"{d}:{t['pct']}%{'' if t['agrees'] else '!'}({SHORT.get(top, top)})")
    return f"{e['label']:<34} {e['lane']:<18} {e['clock']['anchor'] if e.get('clock') else 'no clock':<13} {' '.join(parts)}"


def main(paths):
    status = 0
    for arg in paths:
        p = Path(arg)
        single = (p / 'meta.json').is_file()
        runs = [p] if single else sorted(m.parent for m in p.glob('*/meta.json'))
        explained = []
        for run in runs:
            e = explain_run(run)
            (run / 'explain.json').write_text(json.dumps(e, indent=1) + '\n')
            print(line(e))
            explained.append(e)
            if 'totals' in e and not all(t['agrees'] for t in e['totals'].values()):
                status = 1
        if not single and explained:
            (p / 'explain.md').write_text(folder_md(explained, p.name) + '\n')
            print(f'wrote {p / "explain.md"}')
    return status
