"""Run explanation on small synthetic run folders: taps, pcaps, qlog, qdisc samples and faults."""
import gzip
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
from contrib import explain, report  # noqa: E402

NS, MS = 1_000_000_000, 1_000_000
T0 = 1_000 * NS                 # first source datagram, CLOCK_MONOTONIC
OFF = 1_700_000_000 * NS        # CLOCK_REALTIME - CLOCK_MONOTONIC
STEP = 20 * MS                  # one PCR unit every 20 ms
N = 500
BASE = 20 * MS                  # on-time end-to-end delay
UNIT_BYTES = 1000
SRC, DST = '10.79.1.10', '10.79.2.10'


def t_in(i):
    return T0 + i * STEP


def eth_udp(src, dst, sport, dport, payload, ulen=None):
    ulen = ulen or 8 + len(payload)
    ip = struct.pack('>BBHHHBBH4s4s', 0x45, 0, 20 + ulen, 0, 0, 64, 17, 0, explain.ip4(src), explain.ip4(dst))
    return b'\0' * 12 + b'\x08\x00' + ip + struct.pack('>HHHH', sport, dport, ulen, 0) + payload


def write_pcap(path, packets, snap=96):
    """packets: (epoch ns, frame bytes, original length)."""
    out = [struct.pack('<IHHiIII', 0xa1b2c3d4, 2, 4, 0, 0, snap, 1)]
    for t, frame, orig in sorted(packets, key=lambda p: p[0]):
        cap = frame[:snap]
        out.append(struct.pack('<IIII', t // NS, (t % NS) // 1000, len(cap), orig) + cap)
    with gzip.open(path, 'wb') as f:
        f.write(b''.join(out))


def ts_with_pcr(pcr):
    base, ext = divmod(pcr, 300)
    return bytes([0x47, 0x01, 0x00, 0x30, 183, 0x10, (base >> 25) & 0xFF, (base >> 17) & 0xFF, (base >> 9) & 0xFF,
                  (base >> 1) & 0xFF, ((base & 1) << 7) | 0x7E | (ext >> 8), ext & 0xFF]) + b'\xff' * 30


def srt_packet(seq, pcr, retransmitted=False):
    hdr = struct.pack('>IIII', seq, (1 << 31) | (int(retransmitted) << 26), 0, 7)
    return hdr + ts_with_pcr(pcr)


def pcr(i):
    return i * 540_000


class Run:
    """A synthetic run folder; delay[i] is unit i's end-to-end delay in ns, or None when lost."""

    def __init__(self, root, name, lane='srt', variant=None, profile='BX', budgets=(100, 300), rtt_ms=20.0):
        self.dir = Path(root) / name
        self.dir.mkdir(parents=True)
        self.lane = lane
        self.tap = 'out' if lane == 'srt' else 'raw'
        self.delay = [BASE] * N
        self.meta = dict(label=name, lane=lane, variant=variant or ({'D': 100} if lane == 'srt' else {'cc': 'bbr'}),
                         profile_id=profile, repeat=0, placement='first-mile', source=dict(tier='t', mux_bps=400_000),
                         seconds=N * STEP / NS, join_s=0, budgets_ms=list(budgets), break_ms=200, playout_ms=[300],
                         taps=[self.tap], rtt_ms=rtt_ms, wall_clock=False, events_planned=[],
                         topology=dict(links=[dict(k=0, src_ip=SRC, dst_ip=DST, wan_a='va0', wan_b='vb0')]))

    def late(self, lo, hi, delay_ms=None):
        for i in range(lo, hi + 1):
            self.delay[i] = None if delay_ms is None else delay_ms * MS

    def write(self, summary=True):
        d = self.dir
        (d / 'meta.json').write_text(json.dumps(self.meta))
        (d / 'in-summary.json').write_text(json.dumps(dict(first_ns=T0, last_ns=t_in(N), bytes=UNIT_BYTES * N)))
        (d / 'in-pcr.csv').write_text('pid,pcr,t_ns,n\n' + ''.join(f'256,{pcr(i)},{t_in(i)},{i}\n' for i in range(N)))
        (d / f'{self.tap}-pcr.csv').write_text('pid,pcr,t_ns,n\n' + ''.join(
            f'256,{pcr(i)},{t_in(i) + x},{i}\n' for i, x in enumerate(self.delay) if x is not None))
        if summary:
            (d / 'summary.json').write_text(json.dumps(report.analyze(d)))
        return d

    def arrival_log(self):
        """sub_media's wall-clock arrival log: the arrival-log clock anchor."""
        (self.dir / 'arrival-1.jsonl').write_text(''.join(
            json.dumps(dict(pcr=pcr(i), arrival_ns=t_in(i) + x + OFF)) + '\n' for i, x in enumerate(self.delay) if x))

    def faults(self, *recs):
        self.meta['events_planned'] = [dict(t_s=0, action=r['action']) for r in recs]
        (self.dir / 'faults.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in recs))

    def outage(self, lo, hi):
        on, off = (t_in(lo) + OFF) / NS, (t_in(hi) + OFF) / NS
        self.faults(dict(action='outage_on', name='o', direction='fwd', t0=on, t1=on),
                    dict(action='outage_off', name='o', direction='fwd', t0=off, t1=off))

    def qdisc(self, queued):
        """Bottleneck samples every 100 ms; queued(t_mono) gives the tbf's queueing delay in ms."""
        rate = 1_000_000
        lines = []
        for k in range(N * STEP // (100 * MS)):
            t = T0 + k * 100 * MS
            qs = [dict(kind='netem', handle='1:', backlog=0),
                  dict(kind='tbf', handle='20:', backlog=int(queued(t) / 1000 * rate), options=dict(rate=rate))]
            lines.append(json.dumps(dict(t=(t + OFF) / NS, name='fwd', out=json.dumps(qs))))
        (self.dir / 'qdisc-wan.jsonl').write_text('\n'.join(lines) + '\n')

    def stretches(self, d):
        e = json.loads((self.dir / 'explain.json').read_text())
        return [s for s in e['stretches'] if s['D'] == d]


def qlog(path, events, peer=DST, ref_us=T0 // 1000):
    text = json.dumps(dict(qlog_version='draft-00', traces=[dict(
        vantage_point=dict(name='x', type='client'), configuration=dict(time_units='us'),
        common_fields=dict(reference_time=str(ref_us)),
        events=[[0, 'transport', 'datagram_sent', dict(byte_length=1252, addr_to=dict(ip_v4=peer, port_v4=1))]] + events)]))
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, 'wt') as f:
        f.write(text)


def sent(t_ns, pn, frames, ptype='1RTT', size=UNIT_BYTES):
    return [(t_ns - T0) // 1000, 'transport', 'packet_sent',
            dict(packet_type=ptype, header=dict(packet_number=pn, packet_size=size),
                 frames=[dict(frame_type='stream', id=sid, offset=off, length=ln) for sid, off, ln in frames])]


def quic_sender(run, lost=(), resend_after_ms=150, stream=4):
    """A publisher qlog sending one packet per unit at the source rate; lost[k] packets are resent later."""
    ev, pn = [], 0
    for i in range(N):
        t = t_in(i) + MS
        ev.append(sent(t, pn, [(stream, i * UNIT_BYTES, UNIT_BYTES)]))
        if i in lost:
            ev.append([(t - T0) // 1000 + 40_000, 'recovery', 'packet_lost', dict(packet_number=pn)])
            ev.append(sent(t + resend_after_ms * MS, 10_000 + pn, [(stream, i * UNIT_BYTES, UNIT_BYTES)]))
        if i % 10 == 0:
            ev.append([(t - T0) // 1000, 'recovery', 'metrics_updated',
                       dict(cwnd=100_000, bytes_in_flight=5_000, pacing_rate=10_000_000, smoothed_rtt=20_000)])
        pn += 1
    ev.sort(key=lambda e: e[0])
    qlog(run.dir / 'qlog-aiomoqt-pub' / 'c1.client.qlog.gz', ev)


def explained(run):
    explain.main([str(run.dir)])
    return json.loads((run.dir / 'explain.json').read_text())


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def one(self, run, d, cause):
        ss = run.stretches(d)
        self.assertEqual(len(ss), 1, ss)
        self.assertEqual(ss[0]['cause'], cause, ss[0])
        return ss[0]


class Readers(Base):
    def test_pcap_reader_parses_ethernet_ipv4_udp_and_skips_the_rest(self):
        p = self.root / 'x.pcap.gz'
        write_pcap(p, [(OFF + T0, eth_udp(SRC, DST, 1, 2, b'abc' * 40), 162),
                       (OFF + T0 + 1000, b'\0' * 12 + b'\x08\x06' + b'\0' * 28, 42)])
        pkts = list(explain.pcap(p))
        self.assertEqual(len(pkts), 1)
        t, s, d, sp, dp, ulen, pay = pkts[0]
        self.assertEqual((t, s, d, sp, dp, ulen), (OFF + T0, explain.ip4(SRC), explain.ip4(DST), 1, 2, 128))
        self.assertEqual(pay, (b'abc' * 40)[:96 - 42])

    def test_truncated_qlog_keeps_what_was_written(self):
        p = self.root / 'q' / 'c.client.qlog.gz'
        qlog(p, [sent(T0 + k * MS, k, [(4, k, 10)]) for k in range(5)])
        text = gzip.open(p, 'rt').read()
        cut = text[:text.rfind('"packet_sent"') - 5]
        head = explain.qlog_head(cut)
        self.assertEqual(head[:3], (T0, 'client', DST))
        c = explain.Conn(p, cut, head)
        self.assertEqual(len(c.sent), 4)
        self.assertEqual(c.sent[0][0], T0)


class Causes(Base):
    def test_outage_covers_the_stretch_and_the_recovery(self):
        r = Run(self.root, '001-srt')
        r.late(300, 320)
        r.outage(300, 310)
        r.meta['source_started'] = (T0 + OFF) / NS
        r.write()
        e = explained(r)
        s = self.one(r, 100, 'outage')
        self.assertEqual(e['clock']['anchor'], 'source-start')
        self.assertIn('precise clock anchor', e['inputs']['absent'])
        self.assertEqual(s['evidence']['outage_ms'], 200.0)
        self.assertEqual(s['units'], 21)

    def test_outage_takes_priority_over_queueing(self):
        r = Run(self.root, '001-moqt', lane='moqt-aiomoqt-msfts')
        r.late(300, 340, delay_ms=400)
        r.write()
        r.arrival_log()
        r.outage(300, 320)
        r.qdisc(lambda t: 380 if t_in(300) <= t <= t_in(340) else 0)
        explained(r)
        self.one(r, 100, 'outage')

    def test_queueing_from_the_tbf_backlog(self):
        r = Run(self.root, '001-moqt', lane='moqt-aiomoqt-msfts')
        r.late(300, 340, delay_ms=400)
        r.write()
        r.arrival_log()
        r.qdisc(lambda t: 380 if t_in(300) - 50 * MS <= t <= t_in(340) + 50 * MS else 0)
        e = explained(r)
        s = self.one(r, 100, 'queueing')
        self.assertEqual(e['clock']['anchor'], 'arrival-log')
        self.assertEqual(s['evidence']['queue_from'], 'tbf backlog')
        self.assertGreaterEqual(s['evidence']['queue_ms_max'], 380)

    def sender_gap(self, lane, queue=False):
        """The sender's egress stops for 1 s (units 300-349) and then catches up."""
        r = Run(self.root, '001-x', lane=lane)
        r.late(300, 349, delay_ms=600)
        r.write()
        r.arrival_log()
        pkts = []
        for i in range(N):
            t = t_in(i) + 100_000 + OFF
            if 300 <= i < 350:
                t = t_in(350) + (i - 300) * 100_000 + OFF
            pay = srt_packet(i, pcr(i))
            pkts.append((t, eth_udp(SRC, DST, 5000, 6000, pay, ulen=UNIT_BYTES), UNIT_BYTES + 34))
        write_pcap(r.dir / 'src0.pcap.gz', pkts)
        if queue:
            r.qdisc(lambda t: 500 if t_in(300) <= t <= t_in(360) else 0)
        explained(r)
        return r

    def test_sender_below_media_rate_from_the_src_pcap(self):
        r = self.sender_gap('moqt-aiomoqt-msfts')
        s = self.one(r, 100, 'sender-below-media-rate')
        self.assertGreater(s['evidence']['sender_backlog_ms'], 500)
        self.assertEqual(s['evidence']['egress_from'], 'src pcap')

    def test_sender_takes_priority_over_queueing(self):
        r = self.sender_gap('moqt-aiomoqt-msfts', queue=True)
        self.one(r, 100, 'sender-below-media-rate')

    def test_srt_anchor_from_pcr_in_the_capture(self):
        r = self.sender_gap('srt')
        e = json.loads((r.dir / 'explain.json').read_text())
        self.assertEqual(e['clock']['anchor'], 'srt-pcr')
        self.assertLess(abs(e['clock']['offset_ns'] - OFF - 100_000), 1000)

    def srt_with_loss(self, owd_ms=10, queue_from=None):
        """SRT where seq 300's first copy is dropped and its retransmission lands 60 ms later."""
        r = Run(self.root, '001-srt')
        r.late(300, 303, delay_ms=150)
        r.write()
        src, dst = [], []
        for i in range(N):
            t = t_in(i) + 100_000 + OFF
            q = owd_ms + (300 if queue_from is not None and i >= queue_from and i < 310 else 0)
            frame = eth_udp(SRC, DST, 5000, 6000, srt_packet(i, pcr(i)), ulen=UNIT_BYTES)
            src.append((t, frame, UNIT_BYTES + 34))
            if i != 300:
                dst.append((t + q * MS, frame, UNIT_BYTES + 34))
        rt = eth_udp(SRC, DST, 5000, 6000, srt_packet(300, pcr(300), retransmitted=True), ulen=UNIT_BYTES)
        src.append((t_in(300) + 100_000 + 50 * MS + OFF, rt, UNIT_BYTES + 34))
        dst.append((t_in(300) + 100_000 + 60 * MS + OFF, rt, UNIT_BYTES + 34))
        write_pcap(r.dir / 'src0.pcap.gz', src)
        write_pcap(r.dir / 'dst0.pcap.gz', dst)
        explained(r)
        return r

    def test_srt_repair_with_its_latency(self):
        r = self.srt_with_loss()
        s = self.one(r, 100, 'repair')
        ev = s['evidence']
        self.assertEqual((ev['wan_losses'], ev['repaired'], ev['losses_from']), (1, 1, 'pcap srt'))
        self.assertAlmostEqual(ev['repair_ms_p50'], 50.0, places=1)
        self.assertLess(ev['queue_ms_max'], 1)

    def test_queueing_from_pcap_one_way_delay_takes_priority_over_repair(self):
        r = self.srt_with_loss(queue_from=295)
        s = self.one(r, 100, 'queueing')
        self.assertEqual(s['evidence']['queue_from'], 'pcap one-way delay')
        self.assertAlmostEqual(s['evidence']['queue_ms_max'], 300.0, places=0)

    def test_head_of_line_behind_a_repaired_quic_loss(self):
        r = Run(self.root, '001-moqt', lane='moqt-aiomoqt-msfts')
        r.late(300, 306, delay_ms=200)
        r.write()
        r.arrival_log()
        quic_sender(r, lost={300})
        e = explained(r)
        s = self.one(r, 100, 'head-of-line')
        ev = s['evidence']
        self.assertEqual((ev['own_data_lost_units'], ev['waited_behind_loss_units']), (1, 6))
        self.assertEqual(ev['losses_from'], 'qlog')
        self.assertEqual(ev['repair_ms_p50'], 150.0)
        self.assertEqual(ev['qlog']['cwnd_min'], 100_000)
        self.assertEqual(ev['egress_from'], 'qlog bytes sent')
        self.assertIn('qlog (WAN sender)', e['inputs']['present'])

    def test_repair_when_the_late_units_lost_their_own_data(self):
        r = Run(self.root, '001-moqt', lane='moqt-aiomoqt-msfts')
        r.late(300, 302, delay_ms=200)
        r.write()
        r.arrival_log()
        quic_sender(r, lost={300, 301, 302})
        explained(r)
        s = self.one(r, 100, 'repair')
        self.assertEqual(s['evidence']['own_data_lost_units'], 3)

    def test_qlog_from_another_peer_is_not_the_wan_sender(self):
        r = Run(self.root, '001-moqt', lane='moqt-aiomoqt-msfts')
        r.late(300, 306, delay_ms=200)
        r.write()
        r.arrival_log()
        quic_sender(r, lost={300})
        p = r.dir / 'qlog-aiomoqt-pub' / 'c1.client.qlog.gz'
        text = gzip.open(p, 'rt').read().replace(f'"ip_v4": "{DST}"', '"ip_v4": "127.0.0.1"')
        with gzip.open(p, 'wt') as f:
            f.write(text)
        e = explained(r)
        self.assertIn('qlog (WAN sender)', e['inputs']['absent'])
        self.one(r, 100, 'unexplained')


class Totals(Base):
    def test_totals_agree_with_late_or_lost_and_the_summary(self):
        r = Run(self.root, '001-srt', budgets=(60, 100, 300))
        r.late(100, 104)
        r.late(200, 230, delay_ms=150)
        r.late(400, 410, delay_ms=400)
        d = r.write()
        e = explained(r)
        units = report.match_units(report.pcrs(d, 'in'), report.pcrs(d, 'out'), T0, t_in(N))
        want = report.late_or_lost(units, BASE, [60, 100, 300])
        summary = json.loads((d / 'summary.json').read_text())['outputs']['out']['late_or_lost_pct']
        for k, t in e['totals'].items():
            self.assertEqual(t['pct'], want[k])
            self.assertEqual(t['summary_pct'], summary[k])
            self.assertTrue(t['agrees'])
            self.assertEqual(t['late'], sum(s['units'] for s in r.stretches(int(k))))
            self.assertEqual(sum(t['by_cause'].values()), t['late'])
        self.assertEqual([s['units'] for s in r.stretches(100)], [5, 31, 11])
        self.assertEqual([s['units'] for s in r.stretches(300)], [5, 11])

    def test_loc_lane_scores_frames_like_the_summary(self):
        r = Run(self.root, '001-loc', lane='moqt-aiomoqt-loc')
        r.meta['taps'] = []
        d = r.dir
        (d / 'in-frames.csv').write_text('hash,pts,key,size,t_first_ns,t_last_ns,broken\n' + ''.join(
            f'h{i},{i},0,100,{t_in(i)},{t_in(i)},0\n' for i in range(N)))
        (d / 'frames-1.jsonl').write_text(''.join(json.dumps(dict(sha1=f'h{i}', arrival_ns=t_in(i) + BASE + (
            300 * MS if 300 <= i < 305 else 0))) + '\n' for i in range(N) if i != 310))
        (d / 'meta.json').write_text(json.dumps(r.meta))
        (d / 'in-summary.json').write_text(json.dumps(dict(first_ns=T0, last_ns=t_in(N), bytes=UNIT_BYTES * N)))
        (d / 'summary.json').write_text(json.dumps(report.analyze(d)))
        e = explained(r)
        self.assertEqual(e['tap'], 'frames')
        self.assertEqual([s['units'] for s in r.stretches(100)], [5, 1])
        self.assertTrue(all(t['agrees'] and t['summary_pct'] is not None for t in e['totals'].values()))

    def test_missing_inputs_leave_stretches_unexplained_and_say_why(self):
        r = Run(self.root, '001-srt')
        r.late(300, 320, delay_ms=200)
        r.meta['source_started'] = (T0 + OFF) / NS
        r.write()
        e = explained(r)
        s = self.one(r, 100, 'unexplained')
        for name in ('src0.pcap', 'dst0.pcap', 'qdisc-wan.jsonl', 'srt stats', 'precise clock anchor'):
            self.assertIn(name, e['inputs']['absent'])
        self.assertEqual(s['evidence']['undecided'], ['sender-below-media-rate', 'queueing', 'repair'])
        self.assertIsNotNone(s['evidence']['unaccounted_ms'])

    def test_no_clock_anchor_leaves_outage_undecided(self):
        r = Run(self.root, '001-srt')
        r.late(300, 320)
        r.outage(300, 310)
        r.write()
        e = explained(r)
        s = self.one(r, 100, 'unexplained')
        self.assertIsNone(e['clock'])
        self.assertIn('outage', s['evidence']['undecided'])


class Folder(Base):
    def test_profile_table_and_where_srt_beat_a_moqt_arm(self):
        srt = Run(self.root, '001-srt', variant={'D': 100})
        srt.late(300, 302, delay_ms=150)
        srt.write()
        moq = Run(self.root, '002-moqt', lane='moqt-aiomoqt-msfts', variant={'cc': 'bbr'})
        moq.late(300, 306, delay_ms=200)
        moq.write()
        moq.arrival_log()
        quic_sender(moq, lost={300})
        other = Run(self.root, '003-srt', variant={'D': 100}, profile='BY')
        other.write()
        self.assertEqual(explain.main([str(self.root)]), 0)
        md = (self.root / 'explain.md').read_text()
        self.assertIn('## BX', md)
        self.assertIn('## BY', md)
        self.assertIn('| moqt-aiomoqt-msfts cc=bbr | 1 | 1.4 (hol 100) | 0.0 |', md)
        self.assertIn('| srt D=100 | 1 | 0.6 (unexpl 100) |', md)
        self.assertIn('- D=100: SRT 0.6% vs moqt-aiomoqt-msfts cc=bbr 1.4% (+4 late units, +0.8 pts)', md)
        self.assertIn('head-of-line +7', md)
        self.assertIn('No SRT run at latency D for D=300', md)


if __name__ == '__main__':
    unittest.main()
