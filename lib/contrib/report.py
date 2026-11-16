"""Contribution run analysis. Every time is host CLOCK_REALTIME in ns; all namespaces share it.

TS lanes are matched PCR by PCR (end-to-end delay of each PCR from the source group to an output) and
video PES by PTS (keyframes apart); the LOC lane is matched frame by frame on a hash of the slice
data. The measurement window opens join_s after the first source datagram. Output breaks are gaps of
break_ms or more at an output; late-or-lost at budget D counts units missing, or later than the first
live-edge delay plus D (what a playout locked at the live edge with buffer D would miss). Taps stamp
CLOCK_MONOTONIC; a run matched against a wall-clock log (the LOC lane) whose clock probe saw a
wall-clock step above CLOCK_STEP_MS is marked clock_disturbed.
"""
import csv
import json
from pathlib import Path
import re
import statistics

from fabric.report import pct, r3

SKMEM_DROPS = re.compile(r'skmem:\([^)]*\bd(\d+)\)')
NS = 1_000_000_000


def rows(path):
    p = Path(path)
    if not p.is_file():
        return []
    with p.open() as f:
        return list(csv.DictReader(line for line in f if not line.startswith('#')))


def jsonl(path):
    p = Path(path)
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(errors='replace').splitlines():
        i = line.find('{')
        if i < 0:
            continue
        try:
            out.append(json.loads(line[i:]))
        except ValueError:
            continue
    return out


def stats(values):
    if not values:
        return dict(n=0, min=None, p50=None, p99=None, p999=None, max=None)
    return dict(n=len(values), min=r3(min(values)), p50=r3(pct(values, 50)), p99=r3(pct(values, 99)),
                p999=r3(pct(values, 99.9)), max=r3(max(values)))


def pcrs(run, name, counts=None):
    """{(pid, pcr): first t_ns} of a tap; counts, when given, gets {(pid, pcr): non-null packets so far}."""
    out = {}
    for r in rows(run / f'{name}-pcr.csv'):
        k = (int(r['pid']), int(r['pcr']))
        if k not in out:
            out[k] = int(r['t_ns'])
            if counts is not None and r.get('n'):
                counts[k] = int(r['n'])
    return out


def frames(run, name, by):
    out = {}
    for r in rows(run / f'{name}-frames.csv'):
        k = r[by]
        if k and k not in out:
            out[k] = dict(key=int(r['key']), size=int(r['size']), t_first=int(r['t_first_ns']),
                          t_last=int(r['t_last_ns']), broken=int(r['broken']))
    return out


def window(run, meta):
    """[first source datagram + join_s, + seconds), on the wall clock of the source tap."""
    s = json.loads((run / 'in-summary.json').read_text()) if (run / 'in-summary.json').is_file() else {}
    first = s.get('first_ns')
    if first is None:
        return None, None, s
    w0 = first + int(meta['join_s'] * NS)
    return w0, w0 + int(meta['seconds'] * NS), s


def match_units(ins, outs, w0, w1):
    """[(t_in, t_out or None)] for input units in the window, in input order."""
    return sorted(((t, outs.get(k)) for k, t in ins.items() if w0 <= t < w1), key=lambda x: x[0])


SETTLE_NS = 3 * NS
# A wall-clock step larger than this makes a run's delay figures unreliable: units in flight across it
# carry the step.
CLOCK_STEP_MS = 20.0


def live_anchor(units):
    """(t_in, t_out) of the least-delayed unit in the 2 s after a 3 s settle from the first arrival:
    the live-edge delay a receiver locks to once a join burst has played out (as playout.py does)."""
    arrived = [u for u in units if u[1] is not None]
    if not arrived:
        return None
    t0 = min(o for _, o in arrived) + SETTLE_NS
    near = [u for u in arrived if t0 <= u[1] < t0 + 2 * NS] or arrived
    return min(near, key=lambda u: u[1] - u[0])


def late_or_lost(units, anchor_delay_ns, budgets_ms):
    out = {}
    total = len(units)
    for d in budgets_ms:
        limit = anchor_delay_ns + d * 1e6
        bad = sum(1 for t_in, t_out in units if t_out is None or t_out - t_in > limit)
        out[str(d)] = r3(100 * bad / total) if total else None
    return out


def playout_breaks(units, anchor, buffer_ms, break_ms):
    """Breaks of an in-order playout anchored at anchor=(t_in, t_out) with a fixed buffer: each unit
    leaves at its due time, or on arrival when late, never before the unit ahead of it; a missing
    unit leaves nothing."""
    t_in0, t_out0 = anchor
    times, last = [], None
    for t_in, t_out in units:
        if t_out is None:
            continue
        due = t_out0 + (t_in - t_in0) + buffer_ms * 1e6
        last = max(due, t_out, last if last is not None else due)
        times.append(last)
    return breaks_of(times, break_ms)


def breaks_of(times, break_ms):
    gaps = [(b - a) / 1e6 for a, b in zip(times, times[1:]) if (b - a) / 1e6 >= break_ms]
    return dict(count=len(gaps), longest_ms=r3(max(gaps)) if gaps else 0.0, total_s=r3(sum(gaps) / 1000))


def tap_breaks(run, name, w0, w1, break_ms):
    """Output gaps of break_ms or more in the window, and the longest stretch without one."""
    gaps = []
    for r in rows(run / f'{name}-gaps.csv'):
        a, b = int(r['t_before_ns']), int(r['t_after_ns'])
        if b >= w0 and a <= w1 and (b - a) / 1e6 >= break_ms:
            gaps.append((a, b))
    edges = [w0] + [x for g in gaps for x in g] + [w1]
    clear = max((edges[i + 1] - edges[i] for i in range(0, len(edges) - 1, 2)), default=w1 - w0)
    ms = [(b - a) / 1e6 for a, b in gaps]
    return dict(count=len(ms), longest_ms=r3(max(ms)) if ms else 0.0, total_s=r3(sum(ms) / 1000),
                break_free_s=r3(max(clear, 0) / 1e9))


def ts_counts(run, name, t0, t1):
    rs = [r for r in rows(run / f'{name}-sec.csv') if t0 <= int(r['t_s']) * NS < t1]
    return dict(packets=sum(int(r['packets']) for r in rs) - sum(int(r['nulls']) for r in rs),
                cc_errors=sum(int(r['cc_errors']) for r in rs), datagrams=sum(int(r['datagrams']) for r in rs),
                bytes=sum(int(r['bytes']) for r in rs))


def output_metrics(run, meta, name, ins, in_counts, in_frames, w0, w1, budgets):
    """Delay, loss, breaks and late-or-lost for one output tap."""
    break_ms = meta['break_ms']
    out_counts = {}
    outs = pcrs(run, name, out_counts)
    units = match_units(ins, outs, w0, w1)
    delays = [(o - i) / 1e6 for i, o in units if o is not None]
    missing = sum(1 for _, o in units if o is None)
    res = dict(pcrs=len(units), pcr_missing=missing, pcr_lost_pct=r3(100 * missing / len(units)) if units else None,
               delay_ms=stats(delays), breaks=tap_breaks(run, name, w0, w1, break_ms))
    period = statistics.median([(b - a) for (a, _), (b, _) in zip(units, units[1:])]) / 1e9 if len(units) > 2 else 0.02
    res['programme_lost_s'] = r3(missing * period)
    if units:
        first = live_anchor(match_units(ins, outs, 0, w1))
        if first:
            res['anchor_delay_ms'] = r3((first[1] - first[0]) / 1e6)
            res['late_or_lost_pct'] = late_or_lost(units, first[1] - first[0], budgets)
    if in_frames:
        res['frames'] = frame_metrics(in_frames, frames(run, name, 'pts'), w0, w1)
    res['ts'] = ts_residual(ins, in_counts, outs, out_counts, w0, w1)
    res['ts']['cc_errors'] = ts_counts(run, name, w0, w1 + 5 * NS)['cc_errors']
    return res, units


def ts_residual(ins, in_counts, outs, out_counts, w0, w1):
    """Non-null TS packets between the first and the last PCR of the window that both taps saw,
    counted at each tap: 1 - out/in is the residual loss (duplicates net off)."""
    both = sorted(k for k in ins if w0 <= ins[k] < w1 and k in outs and k in in_counts and k in out_counts)
    if len(both) < 2:
        return dict(in_packets=None, out_packets=None, residual_loss_pct=None)
    a, b = both[0], both[-1]
    if ins[a] > ins[b]:
        a, b = b, a
    n_in = in_counts[b] - in_counts[a]
    n_out = out_counts[b] - out_counts[a]
    return dict(in_packets=n_in, out_packets=n_out, residual_loss_pct=r3(100 * (1 - n_out / n_in)) if n_in else None)


def frame_metrics(in_frames, out_frames, w0, w1):
    """Per video frame: delay from the source's first byte to the output's first byte (key frames and
    others apart), and to the output's last byte."""
    key, other, whole, missing, broken, n = [], [], [], 0, 0, 0
    for k, f in in_frames.items():
        if not w0 <= f['t_first'] < w1:
            continue
        n += 1
        o = out_frames.get(k)
        if o is None:
            missing += 1
            continue
        if o['broken']:
            broken += 1
        (key if f['key'] else other).append((o['t_first'] - f['t_first']) / 1e6)
        whole.append((o['t_last'] - f['t_first']) / 1e6)
    return dict(frames=n, missing=missing, broken=broken, missing_pct=r3(100 * (missing + broken) / n) if n else None,
                key_delay_ms=stats(key), other_delay_ms=stats(other), complete_delay_ms=stats(whole))


def frame_log(run):
    """{sha1: frame} from the subscriber's frame logs, one per connection."""
    out = {}
    for p in sorted(run.glob('frames-*.jsonl')):
        for r in jsonl(p):
            if 'sha1' in r and 'arrival_ns' in r:
                t = int(r['arrival_ns'])
                out.setdefault(r['sha1'], dict(key=int(bool(r.get('key'))), size=r.get('size'), t_first=t,
                                               t_last=t, broken=0))
    return out


def loc_metrics(run, meta, w0, w1, budgets):
    """The LOC lane: frames matched on hash from the source tap to the subscriber's frame log (or, on
    an older subscriber, to an H.264 tap on its elementary-stream pipe)."""
    ins = frames(run, 'in', 'hash')
    logged = list(run.glob('frames-*.jsonl'))
    outs = frame_log(run) if logged else frames(run, 'raw', 'hash')
    res = frame_metrics(ins, outs, w0, w1)
    units = sorted(((f['t_first'], outs[k]['t_last'] if k in outs else None) for k, f in ins.items()
                    if w0 <= f['t_first'] < w1), key=lambda x: x[0])
    first = live_anchor(sorted(((f['t_first'], outs[k]['t_last'] if k in outs else None) for k, f in ins.items()
                                if f['t_first'] < w1), key=lambda x: x[0]))
    if first:
        res['anchor_delay_ms'] = r3((first[1] - first[0]) / 1e6)
        res['late_or_lost_pct'] = late_or_lost(units, first[1] - first[0], budgets)
        res['playout'] = {str(b): playout_breaks(units, first, b, meta['break_ms']) for b in meta['playout_ms']}
    return res


def impairment(meta, run):
    """What the emulator applied: per qdisc packets and drops, and outage drops."""
    final = meta.get('emulator_final') or {}
    devs = {ln['wan_b']: 'fwd' for ln in meta['topology']['links']}
    devs.update({ln['wan_a']: 'rev' for ln in meta['topology']['links']})
    out = {}
    by_dev = {}
    for q in final.get('qdisc', []):
        d = devs.get(q['dev'])
        if d:
            by_dev.setdefault(q['dev'], {})[q['handle']] = q
    for dev, qs in sorted(by_dev.items()):
        name = f'{devs[dev]}{dev[-1]}'
        root, cap = qs.get('1:'), qs.get('20:')
        if not root:
            continue
        child = qs.get('10:') or cap or {}
        child_drops = child.get('drops') or 0
        loss_drops = (root['drops'] or 0) - child_drops
        offered = (root['packets'] or 0) + (root['drops'] or 0)
        out[name] = dict(offered=offered, netem_drops=loss_drops, cap_drops=(cap or {}).get('drops', 0),
                         netem_loss_pct=r3(100 * loss_drops / offered) if offered else 0.0,
                         cap_drop_pct=r3(100 * (cap or {}).get('drops', 0) / offered) if offered and cap else 0.0)
    nft = final.get('nft', {})
    out['nft'] = nft
    out['outage_drops'] = nft.get('outage', {}).get('packets')
    out['events'] = jsonl(run / 'faults.jsonl')
    return out


def reconnections(run):
    """Receiver restarts the lane's reconnect loop logged."""
    n = 0
    for p in run.glob('*.log'):
        n += sum(1 for line in p.read_text(errors='replace').splitlines() if '[harness]' in line and 'reconnecting' in line)
    return n


def protocol_stats(run, lane):
    out = {}
    if lane == 'srt':
        for side in ('snd', 'rcv'):
            recs = jsonl(run / f'srt-{side}.json')
            tot = {}
            for r in recs:
                for sect in ('send', 'recv'):
                    for k, v in (r.get(sect) or {}).items():
                        if isinstance(v, (int, float)) and k.startswith(('packets', 'bytes')):
                            tot[f'{sect}.{k}'] = tot.get(f'{sect}.{k}', 0) + v
            rtts = [r['link']['rtt'] for r in recs if isinstance(r.get('link'), dict) and 'rtt' in r['link']]
            out[side] = dict(reports=len(recs), totals=tot, rtt_ms_p50=r3(pct(rtts, 50)) if rtts else None)
    elif lane == 'rist':
        for side, key in (('snd', 'sender-stats'), ('rcv', 'receiver-stats')):
            tot = {}
            for r in jsonl(run / f'rist-{side}.log'):
                body = r.get(key)
                if not body:
                    continue
                st = (body.get('flowinstant') or {}).get('stats') if side == 'rcv' else None
                if side == 'snd':
                    for peer in body.get('peers', []):
                        for k, v in (peer.get('stats') or {}).items():
                            if isinstance(v, (int, float)) and k in ('sent', 'retransmitted', 'sent_bytes', 'retransmitted_bytes'):
                                tot[k] = tot.get(k, 0) + v
                elif st:
                    for k, v in st.items():
                        if isinstance(v, (int, float)) and k in ('received', 'missing', 'reordered', 'recovered_total',
                                                                  'lost', 'dropped_late', 'dropped_full', 'duplicates', 'retries'):
                            tot[k] = tot.get(k, 0) + v
            out[side] = tot
    elif lane.startswith('moqt'):
        pub = (run / 'moq2ts-pub.log').read_text(errors='replace') if (run / 'moq2ts-pub.log').is_file() else ''
        srt = [ln for ln in pub.splitlines() if '[srt]' in ln]
        final = [ln for ln in pub.splitlines() if '[final]' in ln]
        out['moq2ts_ingest_last'] = srt[-1].split('[srt]', 1)[1].strip() if srt else None
        out['moq2ts_final'] = final[-1].split('[final]', 1)[1].strip() if final else None
        rx = run / 'moq2ts-receiver.json'
        if rx.is_file():
            try:
                out['moq2ts_receiver'] = json.loads(rx.read_text())
            except ValueError:
                out['moq2ts_receiver'] = rx.read_text()[:500]
        out['relay'] = relay_counters(run)
        out['playout'] = playout_stats(run)
        subs = [jsonl(p) for p in sorted(run.glob('sub-analyze-*.jsonl'))]
        if subs:
            out['sub_media'] = [s[-1] for s in subs if s]
    return out


def relay_counters(run):
    """First-to-last change of the relay's admin counters that describe transport health."""
    samples = [s for s in jsonl(run / 'relay-admin.jsonl') if s.get('name') == 'metrics' and not s['out'].startswith('ERROR')]
    if not samples:
        return None
    want = re.compile(r'(sent|lost|loss|retrans|pto|cwnd|pacer|rtt|drop)', re.I)

    def parse(text):
        vals = {}
        for line in text.splitlines():
            if not line or line[0] == '#':
                continue
            name, _, value = line.rpartition(' ')
            metric = name.split('{', 1)[0]
            if want.search(metric):
                try:
                    vals[metric] = vals.get(metric, 0.0) + float(value)
                except ValueError:
                    pass
        return vals
    first, last = parse(samples[0]['out']), parse(samples[-1]['out'])
    return {k: r3(v - first.get(k, 0.0)) for k, v in sorted(last.items())}


def playout_stats(run):
    recs = jsonl(run / 'playout.jsonl')
    if not recs:
        return None
    out = {}
    for name in recs[-1].get('outs', {}):
        depths = [r['outs'][name]['depth_ms'] for r in recs if name in r.get('outs', {})]
        last = recs[-1]['outs'][name]
        out[name] = dict(late=last['late'], max_late_ms=last['max_late_ms'], sent=last['sent'],
                         depth_ms_p50=r3(pct(depths, 50)), depth_ms_min=r3(min(depths)) if depths else None)
    out['reanchors'] = recs[-1].get('reanchors')
    return out


def socket_drops(run):
    out = {}
    for ns in ('src', 'dst'):
        drops = {}
        for s in jsonl(run / f'ss-{ns}.jsonl'):
            for line in s.get('out', '').splitlines():
                m = SKMEM_DROPS.search(line)
                if m:
                    key = ' '.join(line.split()[3:5])
                    drops[key] = max(drops.get(key, 0), int(m[1]))
        out[ns] = sum(drops.values())
    return out


def clock_pauses(run):
    """Late 1 ms wake-ups (pauses) and wall-clock steps seen by each endpoint's clock probe."""
    out = {}
    for ns in ('src', 'dst'):
        rs = rows(run / f'clock-{ns}.csv')
        late = [float(r['ms']) for r in rs if r.get('kind') == 'late']
        steps = [float(r['ms']) for r in rs if r.get('kind') == 'step']
        out[ns] = dict(pauses=len(late), worst_pause_ms=r3(max(late)) if late else 0.0, clock_steps=len(steps),
                       largest_step_ms=r3(max(steps, key=abs)) if steps else 0.0)
    return out


def analyser(run):
    p = run / 'analyser.log'
    if not p.is_file():
        return None
    text = p.read_text(errors='replace')
    lines = [ln for ln in text.splitlines() if 'continuity' in ln.lower() or 'pcrverify' in ln.lower()]
    return dict(continuity_lines=sum(1 for ln in lines if 'continuity' in ln.lower()), tail=lines[-6:])


def analyze(run_dir):
    run = Path(run_dir)
    meta = json.loads((run / 'meta.json').read_text())
    w0, w1, insum = window(run, meta)
    budgets = meta.get('budgets_ms') or sorted({200, 300, *[int(m * max(meta['rtt_ms'], 20)) for m in (2, 3, 4, 6)]})
    out = dict(label=meta['label'], lane=meta['lane'], variant=meta['variant'], profile=meta['profile_id'],
               repeat=meta['repeat'], placement=meta['placement'], source=meta['source']['tier'],
               seconds=meta['seconds'], budgets_ms=budgets, exit=meta.get('exit'))
    if w0 is None:
        out['error'] = 'the source tap saw nothing'
        out['verdict'] = 'FAIL'
        return out
    in_counts = {}
    ins = pcrs(run, 'in', in_counts)
    out['source'] = dict(out['source'] if isinstance(out['source'], dict) else {'tier': out['source']},
                         in_datagrams=insum.get('datagrams'), in_cc_errors=insum.get('cc_errors'))
    if meta['lane'] == 'moqt-aiomoqt-loc' and 'raw' not in meta.get('taps', []):
        out['frames'] = loc_metrics(run, meta, w0, w1, budgets)
        main = out['frames']
    else:
        in_frames = frames(run, 'in', 'pts')
        out['outputs'] = {}
        raw_units = None
        for name in meta.get('taps', []):
            res, units = output_metrics(run, meta, name, ins, in_counts, in_frames, w0, w1, budgets)
            out['outputs'][name] = res
            if name == 'raw':
                raw_units = units
        if raw_units:
            first = live_anchor(match_units(ins, pcrs(run, 'raw'), 0, w1))
            if first:
                out['outputs']['raw']['playout'] = {str(b): playout_breaks(raw_units, first, b, meta['break_ms'])
                                                    for b in meta['playout_ms']}
        main = next((out['outputs'][n] for n in meta.get('taps', []) if n != 'raw'), None) or {}
    out['impairment'] = impairment(meta, run)
    out['protocol'] = protocol_stats(run, meta['lane'])
    out['socket_drops'] = socket_drops(run)
    out['reconnections'] = reconnections(run)
    out['clock'] = clock_pauses(run)
    worst = max((abs(c['largest_step_ms']) for c in out['clock'].values()), default=0.0)
    out['clock_disturbed'] = bool(meta.get('wall_clock')) and worst > CLOCK_STEP_MS
    out['analyser'] = analyser(run)
    out['verdict'] = verdict(out, main, meta)
    return out


def verdict(out, main, meta):
    """P0 must read zero errors; other profiles only fail on no delivery."""
    frames = main.get('frames')
    delivered = (main.get('delay_ms') or {}).get('n') or (isinstance(frames, int) and frames > (main.get('missing') or 0))
    if not delivered:
        return 'FAIL'
    if meta['profile_id'] == 'P0':
        clean = (main.get('pcr_missing', main.get('missing', 0)) == 0 and not (main.get('ts') or {}).get('cc_errors')
                 and not (main.get('breaks') or {}).get('count'))
        return 'PASS' if clean else 'FAIL'
    return 'OK'


def line(s):
    """One text line per run."""
    v = s.get('variant') or {}
    head = f"{s['label']:<34} {s['lane']:<18} {s['profile']:<4} {json.dumps(v, separators=(',', ':')):<28}"
    if 'error' in s:
        return f"{head} {s['error']}"
    main = None
    if 'outputs' in s:
        name = next((n for n in s['outputs'] if n != 'raw'), None)
        main = s['outputs'].get(name) if name else None
    if main:
        d = main['delay_ms']
        llol = main.get('late_or_lost_pct') or (s['outputs'].get('raw') or {}).get('late_or_lost_pct') or {}
        return (f"{head} delay p50/p99/max {d['p50']}/{d['p99']}/{d['max']} ms, PCR lost {main['pcr_lost_pct']}%, "
                f"breaks {main['breaks']['count']} (longest {main['breaks']['longest_ms']} ms), "
                f"cc {(main.get('ts') or {}).get('cc_errors')}, late|lost@D {llol}  {s['verdict']}"
                f"{'  CLOCK' if s.get('clock_disturbed') else ''}")
    f = s.get('frames') or {}
    return (f"{head} frames {f.get('frames')} missing {f.get('missing_pct')}%, key p50/p99 "
            f"{(f.get('key_delay_ms') or {}).get('p50')}/{(f.get('key_delay_ms') or {}).get('p99')} ms, other "
            f"{(f.get('other_delay_ms') or {}).get('p50')}/{(f.get('other_delay_ms') or {}).get('p99')} ms, "
            f"late|lost@D {f.get('late_or_lost_pct')}  {s['verdict']}{'  CLOCK' if s.get('clock_disturbed') else ''}")


def main(paths):
    status = 0
    for arg in paths:
        p = Path(arg)
        runs = [p] if (p / 'meta.json').is_file() else sorted(m.parent for m in p.glob('*/meta.json'))
        for run in runs:
            s = analyze(run)
            (run / 'summary.json').write_text(json.dumps(s, indent=1) + '\n')
            print(line(s))
            if s['verdict'] == 'FAIL':
                status = 1
    return status


def table(summaries):
    """Runs side by side: delay, loss, breaks and late-or-lost at each budget, per profile."""
    out = []
    for s in sorted(summaries, key=lambda x: (x.get('profile', ''), x.get('lane', ''), x.get('label', ''))):
        if s.get('verdict') == 'PENDING':
            out.append(f"{s['profile']:<4} {s['lane']:<20} {'':<26} PENDING: {s.get('reason')}")
            continue
        out.append(line(s))
    return '\n'.join(out)
