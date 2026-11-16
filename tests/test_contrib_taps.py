"""Contribution taps, playout and analysis on synthetic transport streams, without Docker."""
import json
from pathlib import Path
import random
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
sys.path.insert(0, str(ROOT / 'remote' / 'contrib'))
import tslib  # noqa: E402
import tstap  # noqa: E402
import playout  # noqa: E402
from contrib import report  # noqa: E402

VPID, APID, PMT = 256, 257, 4096


def packet(pid, payload=b'', pusi=False, cc=0, pcr=None):
    """One 188-byte packet; an adaptation field carries the PCR and stuffs a short payload."""
    room = 184 - len(payload)
    af = b''
    if pcr is not None or room > 0:
        body = b''
        flags = 0
        if pcr is not None:
            base, ext = divmod(pcr, 300)
            body = bytes([(base >> 25) & 0xFF, (base >> 17) & 0xFF, (base >> 9) & 0xFF, (base >> 1) & 0xFF,
                          ((base & 1) << 7) | 0x7E | (ext >> 8), ext & 0xFF])
            flags = 0x10
        n = room - 1
        if n < 0:
            raise ValueError('payload too long for the adaptation field')
        af = bytes([n]) + (bytes([flags]) + body + b'\xff' * max(0, n - 1 - len(body)) if n else b'')
    afc = (2 if af else 0) | (1 if payload else 0)
    hdr = bytes([0x47, (0x40 if pusi else 0) | (pid >> 8), pid & 0xFF, (afc << 4) | (cc & 0xF)])
    pkt = hdr + af + payload
    assert len(pkt) == 188, len(pkt)
    return pkt


def psi(table_id, body):
    length = len(body) + 5 + 4
    sec = bytes([table_id, 0xB0 | (length >> 8), length & 0xFF, 0, 1, 0xC1, 0, 0]) + body + b'\0\0\0\0'
    return b'\x00' + sec


def pat():
    return packet(0, psi(0, bytes([0, 1, 0xE0 | (PMT >> 8), PMT & 0xFF])), pusi=True)


def pmt(vtype):
    streams = bytes([vtype, 0xE0 | (VPID >> 8), VPID & 0xFF, 0xF0, 0, 0x04, 0xE0 | (APID >> 8), APID & 0xFF, 0xF0, 0])
    return packet(PMT, psi(2, bytes([0xE0 | (VPID >> 8), VPID & 0xFF, 0xF0, 0]) + streams), pusi=True)


def pes(pts, es):
    p = bytes([0x21 | ((pts >> 29) & 0x0E), (pts >> 22) & 0xFF, ((pts >> 14) & 0xFE) | 1, (pts >> 7) & 0xFF,
               ((pts << 1) & 0xFE) | 1])
    return b'\x00\x00\x01\xe0\x00\x00\x80\x80\x05' + p + es


def mpeg2_picture(key, n):
    rng = random.Random(n)
    return b'\x00\x00\x01\x00\x00' + bytes([(1 if key else 2) << 3]) + bytes(rng.getrandbits(8) for _ in range(900))


def h264_au(key, n, params=False, aud=False):
    rng = random.Random(n)
    nal = bytes([0x65 if key else 0x41, 0x88]) + bytes(rng.randrange(1, 256) for _ in range(700))
    units = []
    if aud:
        units.append(b'\x09\xf0')
    if params:
        units += [b'\x67\x42\x00\x1f\xaa', b'\x68\xce\x38\x80']
    units.append(nal)
    return b''.join(b'\x00\x00\x00\x01' + u for u in units)


def stream(frames=50, codec='mpeg2v', drop=None):
    """(bytes, [(pcr, pts, key)]): PAT and PMT, then one PES per frame on the video PID, the first packet
    of each carrying a PCR, at 25 frames per second."""
    vtype = 0x02 if codec == 'mpeg2v' else 0x1B
    out, cc, meta = [pat(), pmt(vtype)], 0, []
    for n in range(frames):
        key = n % 25 == 0
        pts = 90000 + n * 3600
        pcr = pts * 300 - 27_000_000 // 10
        es = mpeg2_picture(key, n) if codec == 'mpeg2v' else h264_au(key, n, params=key, aud=True)
        data = pes(pts, es)
        first = True
        while data:
            take = 176 if first else 184
            chunk, data = data[:take], data[take:]
            pkt = packet(VPID, chunk, pusi=first, cc=cc, pcr=pcr if first else None)
            if drop is None or len(out) != drop:
                out.append(pkt)
            else:
                out.append(None)
            cc = (cc + 1) & 0xF
            first = False
        meta.append((pcr, pts, key))
        out.append(packet(0x1FFF, b'\xff' * 184))
    return b''.join(p for p in out if p), meta


class Demux(unittest.TestCase):
    def test_pcr_frames_and_keys(self):
        data, meta = stream(50)
        pcrs, frames = [], []
        d = tslib.Demux(on_pcr=lambda pid, pcr, t: pcrs.append((pid, pcr)), on_frame=frames.append, frames=True)
        pos = 0
        rng = random.Random(1)
        while pos < len(data):
            n = rng.randrange(1, 3000)
            d.feed(data[pos:pos + n], pos)
            pos += n
        d.flush()
        self.assertEqual(d.video_pid, VPID)
        self.assertEqual(d.video_codec, 'mpeg2v')
        self.assertEqual(d.pcr_pid, VPID)
        self.assertEqual(pcrs, [(VPID, m[0]) for m in meta])
        self.assertEqual([f['pts'] for f in frames], [m[1] for m in meta])
        self.assertEqual([f['key'] for f in frames], [int(m[2]) for m in meta])
        self.assertEqual(d.cc_errors, {})
        self.assertEqual(d.nulls, 50)

    def test_continuity_error_breaks_the_frame(self):
        data, _ = stream(10, drop=12)
        frames = []
        d = tslib.Demux(on_frame=frames.append, frames=True)
        d.feed(data, 0)
        d.flush()
        self.assertEqual(sum(d.cc_errors.values()), 1)
        self.assertEqual(sum(f['broken'] for f in frames), 1)

    def test_resync(self):
        data, meta = stream(5)
        pcrs = []
        d = tslib.Demux(on_pcr=lambda pid, pcr, t: pcrs.append(pcr))
        d.feed(b'\x00\x13' + data, 0)
        self.assertEqual(pcrs, [m[0] for m in meta])
        self.assertGreater(d.sync_losses, 0)

    def test_h264_hash_survives_remux(self):
        """The frame hash a TS tap computes equals the one an Annex-B tap computes after a receiver
        drops the delimiter and repeats the parameter sets."""
        data, _ = stream(30, codec='h264')
        ts_frames = []
        d = tslib.Demux(on_frame=ts_frames.append, frames=True, hashes=True)
        d.feed(data, 0)
        d.flush()
        es_frames = []
        a = tslib.AnnexB(es_frames.append)
        for n in range(30):
            a.feed(h264_au(n % 25 == 0, n, params=n % 25 == 0), n)
        a.close()
        self.assertEqual([f['hash'] for f in ts_frames], [f['hash'] for f in es_frames])
        self.assertEqual([f['key'] for f in ts_frames], [f['key'] for f in es_frames])
        self.assertEqual(len({f['hash'] for f in ts_frames}), 30)


class Playout(unittest.TestCase):
    def test_media_time_follows_the_pcr(self):
        clock = playout.Clock()
        data, meta = stream(10)
        times = []
        for i in range(0, len(data), 188):
            m, resolved = clock.media_time(data[i:i + 188], 1_000_000_000)
            times += [rm for _, rm in resolved]
            if m is not None:
                times.append(m)
        self.assertEqual(len(times), len(data) // 188)
        self.assertEqual(times, sorted(times))
        self.assertAlmostEqual(times[-1] - times[0], meta[-1][0] - meta[0][0], delta=0.05 * 27_000_000)
        due = clock.due_ns(meta[1][0], 300_000_000)
        self.assertEqual(due - clock.anchor[0], int((meta[1][0] - clock.anchor[1]) * 1000 / 27) + 300_000_000)


def write_run(d, delays, lost=(), gap_at=None, lane='srt', taps=('out',), step=None):
    """A run folder with source and output taps fed synthetic datagrams: one PCR per frame at 25 fps,
    output delayed per frame, some frames lost, an optional output gap and a backward clock step."""
    data, meta = stream(len(delays))
    run = Path(d)
    sinks = {'in': tstap.Sink('in', d, True, False, 50)}
    for name in taps:
        sinks[name] = tstap.Sink(name, d, True, False, 50)
    t0 = 1_000_000_000_000
    per = len(data) // len(delays)
    out_events = []
    for i in range(len(delays)):
        chunk = data[i * per:(i + 1) * per] if i < len(delays) - 1 else data[i * per:]
        t_in = t0 + i * 40_000_000
        sinks['in'].take(chunk, t_in)
        if i not in lost:
            t_out = t_in + int(delays[i] * 1e6) + (300_000_000 if gap_at is not None and i >= gap_at else 0)
            out_events.append((t_out, chunk))
    for t_out, chunk in sorted(out_events, key=lambda e: e[0]):
        for name in taps:
            sinks[name].take(chunk, t_out if step is None or t_out < step[0] else t_out + step[1])
    for s in sinks.values():
        s.close()
    if step is not None:
        rewrite = run / 'in-pcr.csv'
        lines = rewrite.read_text().splitlines()
        fixed = [lines[0]] + [f'{p},{c},{int(t) + step[1] if int(t) >= step[0] else t},{n}'
                              for p, c, t, n in (ln.split(',') for ln in lines[1:])]
        rewrite.write_text('\n'.join(fixed) + '\n')
    if step is not None:
        (run / 'clock-dst.csv').write_text(f't_ns,kind,ms\n{step[0]},step,{step[1] / 1e6}\n')
    meta = dict(label='001-x', lane=lane, variant={}, repeat=0, placement='last-mile', profile_id='P3', rtt_ms=80,
                source=dict(tier='mpeg2-20m'), seconds=len(delays) * 0.04 - 0.5, join_s=0.2, break_ms=200,
                budgets_ms=[100, 300], playout_ms=[300, 200], taps=list(taps), topology=dict(links=[]),
                emulator_final={})
    (run / 'meta.json').write_text(json.dumps(meta))
    return run


class Analysis(unittest.TestCase):
    def test_delay_loss_and_breaks(self):
        delays = [100.0] * 200
        delays[50] = 250.0
        with tempfile.TemporaryDirectory() as d:
            s = report.analyze(write_run(d, delays, lost={80, 81}, gap_at=150))
        o = s['outputs']['out']
        self.assertEqual(o['pcr_missing'], 2)
        self.assertEqual(o['delay_ms']['p50'], 100.0)
        self.assertGreaterEqual(o['delay_ms']['max'], 400.0)
        self.assertEqual(o['breaks']['count'], 1)
        self.assertGreater(o['breaks']['longest_ms'], 300)
        self.assertGreater(o['breaks']['break_free_s'], 4.5)
        self.assertLess(o['breaks']['break_free_s'], 6.2)
        lol = o['late_or_lost_pct']
        self.assertGreater(lol['100'], lol['300'])
        self.assertAlmostEqual(o['ts']['residual_loss_pct'], 100 * 2 / 200, delta=0.6)
        self.assertEqual(o['frames']['missing'], 2)
        self.assertEqual(s['verdict'], 'OK')

    def test_zero_errors_on_p0(self):
        with tempfile.TemporaryDirectory() as d:
            run = write_run(d, [5.0] * 100)
            m = json.loads((run / 'meta.json').read_text())
            m['profile_id'] = 'P0'
            (run / 'meta.json').write_text(json.dumps(m))
            s = report.analyze(run)
        self.assertEqual(s['verdict'], 'PASS')
        self.assertEqual(s['outputs']['out']['ts']['residual_loss_pct'], 0.0)

    def test_nothing_delivered_fails(self):
        with tempfile.TemporaryDirectory() as d:
            s = report.analyze(write_run(d, [5.0] * 50, lost=set(range(50))))
        self.assertEqual(s['verdict'], 'FAIL')

    def test_backward_clock_step_is_taken_out(self):
        with tempfile.TemporaryDirectory() as d:
            s = report.analyze(write_run(d, [100.0] * 200, step=(1_000_000_000_000 + 4_000_000_000, -60_000_000)))
        self.assertGreaterEqual(s['outputs']['out']['delay_ms']['min'], 99.9)
        self.assertLessEqual(s['outputs']['out']['delay_ms']['max'], 100.1)

    def test_offline_playout_breaks(self):
        units = [(i * 20_000_000, i * 20_000_000 + 100_000_000) for i in range(100)]
        units[50] = (units[50][0], units[50][1] + 400_000_000)
        anchor = units[0]
        self.assertEqual(report.playout_breaks(units, anchor, 300, 200)['count'], 0)
        self.assertEqual(report.playout_breaks(units, anchor, 100, 200)['count'], 1)
        a = report.live_anchor(units)
        self.assertEqual(a[1] - a[0], 100_000_000)


if __name__ == '__main__':
    unittest.main()
