"""Fabric run analysis: per-subscriber delivery and latency, fault windows, namespace propagation, setup times.

Times are host epoch seconds: the publisher's and subscribers' lines carry the
time tsline read them, faults carry the host times bracketing each action, and
pollers stamp each sample. One host, one clock.
"""
import json
from pathlib import Path
import re
import statistics

from fabric import topology
from fabric.moqx_peering import namespaces_in, peer_rows

SEND = re.compile(r'TRACE: MOQT send: object (\d+)\.(\d+) alias=\d+ (\d+) bytes(?! \(datagram\))')
RECV = re.compile(r'TRACE: MOQT stream\(\d+\): object (\d+)\.(\d+) alias=\d+ \d+ bytes status=(\S+)')
SUMMARY = {
    'objects': re.compile(r'^\s+Objects:\s+([\d,]+)'),
    'lat_p50': re.compile(r'p50=([\d.]+)'), 'lat_p95': re.compile(r'p95=([\d.]+)'),
    'lat_p99': re.compile(r'p99=([\d.]+) ms'), 'jitter_ms': re.compile(r'^\s+Jitter:\s+([\d.]+) ms'),
    'lost': re.compile(r'^\s+Lost:\s+(\d+)'), 'ooo': re.compile(r'^\s+Out-of-order:\s+(\d+)'),
}
INTERVAL = re.compile(r'^\s+(\d+)-(\d+)s\s.*?\s([\d.]+)(us|ms|s) p99:([\d.]+)(us|ms|s)')
UNIT = {'us': 1e-3, 'ms': 1.0, 's': 1e3}


def lines(path):
    """[(epoch, text)] from a tsline-prefixed log."""
    out = []
    for raw in Path(path).read_text(errors='replace').splitlines():
        head, _, text = raw.partition(' ')
        try:
            out.append((float(head), text))
        except ValueError:
            continue
    return out


def pct(values, p):
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def r3(x):
    return None if x is None else round(x, 3)


def sends(path):
    out = {}
    for t, text in lines(path):
        m = SEND.search(text)
        if m:
            out.setdefault((int(m[1]), int(m[2])), t)
    return out


def subscriber(path):
    """Arrivals, markers and the tool's own summary from one sub_bench log."""
    arrivals, markers, summary, intervals = [], {}, {}, []
    for t, text in lines(path):
        m = RECV.search(text)
        if m:
            if m[3] == '0':
                arrivals.append((t, int(m[1]), int(m[2])))
            continue
        if 'Connecting...' in text:
            markers.setdefault('connecting', t)
        elif 'Subscribed to' in text:
            markers.setdefault('subscribed', t)
        elif 'error' in text.lower() and 'TRACE' not in text and 'DEBUG' not in text:
            markers.setdefault('error', text.strip())
        im = INTERVAL.match(text)
        if im:
            intervals.append(dict(t=t, avg_ms=float(im[3]) * UNIT[im[4]], p99_ms=float(im[5]) * UNIT[im[6]]))
        for key, rx in SUMMARY.items():
            sm = rx.search(text)
            if sm and key not in summary:
                summary[key] = float(sm[1].replace(',', ''))
    return dict(arrivals=arrivals, markers=markers, summary=summary, intervals=intervals)


def delivery(sub, sent, t_from=None, t_to=None):
    """Objects, duplicates, missing and per-object latency (ms) for arrivals in [t_from, t_to]."""
    arr = [a for a in sub['arrivals'] if (t_from is None or a[0] >= t_from) and (t_to is None or a[0] <= t_to)]
    seen, dups, lat = {}, 0, []
    for t, g, o in arr:
        if (g, o) in seen:
            dups += 1
            continue
        seen[(g, o)] = t
        if (g, o) in sent:
            lat.append((t - sent[(g, o)]) * 1000)
    out = dict(objects=len(arr), unique=len(seen), dups=dups)
    if seen:
        first, last = min(seen), max(seen)
        expected = [k for k, ts in sent.items() if first <= k <= last
                    and (t_from is None or ts >= t_from - 1) and (t_to is None or ts <= t_to)]
        out.update(first=list(first), last=list(last), missing=sum(1 for k in expected if k not in seen))
    else:
        out.update(first=None, last=None, missing=None)
    out.update(lat_p50=r3(pct(lat, 50)), lat_p90=r3(pct(lat, 90)), lat_p99=r3(pct(lat, 99)),
               lat_mean=r3(statistics.fmean(lat)) if lat else None)
    return out


def gaps(sub, t0, t1):
    """Largest inter-arrival gap (ms) and when it ended, over arrivals in [t0, t1]."""
    ts = sorted(a[0] for a in sub['arrivals'] if t0 - 1 <= a[0] <= t1)
    best = (0.0, None)
    for a, b in zip(ts, ts[1:]):
        if b >= t0 and (b - a) * 1000 > best[0]:
            best = ((b - a) * 1000, b)
    return best


def polls(path):
    """{name: [(t, body-or-None)]}: a sample's body when it changed, else None (unchanged)."""
    out = {}
    if not Path(path).is_file():
        return out
    for raw in Path(path).read_text().splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        out.setdefault(row['name'], []).append((row['t'], row.get('body'), row.get('status')))
    return out


def at(samples, t):
    """The body in force at time t: the last changed body at or before it."""
    body = None
    for ts, b, _ in samples:
        if ts > t:
            break
        if b is not None:
            body = b
    return body


def upstreams(run_dir, order, names, t):
    """{node: peer it receives the track from} at time t, from /peering samples."""
    out = {}
    for n in order:
        body = at(polls(run_dir / f'poll-{n}.jsonl').get('peering', []), t)
        if not body:
            continue
        try:
            rows = peer_rows(json.loads(body), names)
        except ValueError:
            continue
        ups = [r['peer'] for r in rows if r['status'] == 'connected' and (r['publishes'] or 0) > 0]
        out[n] = ups
    return out


def path_to(node, ups, limit=8):
    path = [node]
    while ups.get(path[-1]) and len(path) <= limit:
        nxt = ups[path[-1]][0]
        if nxt in path:
            path.append(nxt + '(loop)')
            break
        path.append(nxt)
    return list(reversed(path))


def analyze(run_dir):
    run_dir = Path(run_dir)
    meta = json.loads((run_dir / 'meta.json').read_text())
    order = meta['order']
    links = [tuple(p) for p in meta['links']]
    ingest = topology.resolve(meta['scenario'].get('publisher', 'ingest'), order)
    hop = topology.hops(links, ingest)
    names = {ip: n for n, ip in meta['addressing']['access']['hosts'].items()}
    for link in meta['addressing']['links'].values():
        for k, v in link.items():
            if k in order:
                names[v] = k
    sent = sends(run_dir / 'pub.log') if (run_dir / 'pub.log').is_file() else {}
    out = dict(label=meta['label'], scenario=meta['test']['scenario'], topology=meta['test'].get('topology'),
               links=['-'.join(p) for p in links], relay_image=(meta.get('relay_image') or {}).get('tags'),
               relay_labels=(meta.get('relay_image') or {}).get('labels'), sent=len(sent),
               unfinished=meta.get('unfinished'), leftover_impairments=meta.get('leftover_impairments'),
               options=meta.get('options'), draft=meta.get('draft'))
    events = []
    fpath = run_dir / 'faults.jsonl'
    if fpath.is_file():
        events = [json.loads(x) for x in fpath.read_text().splitlines() if x.strip()]
    subs = {}
    for log in sorted(run_dir.glob('*.log')):
        name = log.stem
        if not name.startswith(('sub-', 'cold-', 'warm-')):
            continue
        node = name.split('-', 1)[1]
        s = subscriber(log)
        row = dict(node=node, hops_at_start=hop.get(node), tool=s['summary'], markers={}, **delivery(s, sent))
        mk = s['markers']
        if 'connecting' in mk:
            if 'subscribed' in mk:
                row['markers']['subscribed_ms'] = r3((mk['subscribed'] - mk['connecting']) * 1000)
            if s['arrivals']:
                row['markers']['first_object_ms'] = r3((s['arrivals'][0][0] - mk['connecting']) * 1000)
        if 'error' in mk:
            row['markers']['error'] = mk['error']
        if events and s['arrivals']:
            row['events'] = []
            for i, ev in enumerate(events):
                t0 = ev.get('t0')
                if t0 is None:
                    continue
                t_next = events[i + 1]['t0'] if i + 1 < len(events) and events[i + 1].get('t0') else t0 + 30
                win_end = min(t0 + 10, t_next)
                gap_ms, gap_end = gaps(s, t0, win_end)
                nominal = 1000 / (meta['options'].get('rate') or 30)
                inwin = delivery(s, sent, t0 - 0.5, win_end)
                before = delivery(s, sent, t0 - 5, t0)
                after = delivery(s, sent, min(t0 + 2, win_end), win_end)
                row['events'].append(dict(
                    verb=ev['verb'], on=ev.get('on'), peer=ev.get('peer'), metric=ev.get('metric'),
                    ok=ev.get('ok'), status=ev.get('status'), expect=ev.get('expect'),
                    max_gap_ms=r3(gap_ms), recovery_ms=r3((gap_end - t0) * 1000) if gap_ms > 3 * nominal else 0.0,
                    lost=inwin['missing'], dups=inwin['dups'],
                    lat_p50_before=before['lat_p50'], lat_p50_after=after['lat_p50']))
        subs[name] = row
    out['subscribers'] = subs
    if events:
        evrows = []
        for ev in events:
            t0 = ev.get('t0')
            row = {k: ev.get(k) for k in ('verb', 'on', 'peer', 'metric', 'ok', 'status', 'expect', 'skipped')}
            if t0:
                row['action_ms'] = r3((ev['t1'] - t0) * 1000)
                for label, t in (('path_before', t0 - 0.2), ('path_after', t0 + 5)):
                    ups = upstreams(run_dir, order, names, t)
                    row[label] = {n: '>'.join(path_to(n, ups)) for n in order if n != ingest}
            evrows.append(row)
        out['events'] = evrows
    prop = {}
    for n in order:
        samples = polls(run_dir / f'poll-state-{n}.jsonl').get('state', [])
        for ts, body, _ in samples:
            if body:
                try:
                    found = namespaces_in(json.loads(body))
                except ValueError:
                    continue
                if any(ns == meta['namespace'] or ns.startswith(meta['namespace'] + '/') for ns in found):
                    prop[n] = ts
                    break
    if prop:
        base = prop.get(ingest)
        out['namespace_seen'] = {n: r3((t - base) * 1000) if base else None for n, t in prop.items()}
        out['namespace_missing'] = [n for n in order if n not in prop]
    out['verdicts'] = verdicts(out, meta)
    return out


def verdicts(out, meta):
    group = (meta.get('options') or {}).get('group_size') or 30
    res = []
    for name, s in out['subscribers'].items():
        if not s.get('events'):
            ok = s['unique'] > 0 and s['dups'] == 0 and (s['missing'] or 0) == 0
            res.append(dict(check=f'{name} delivered every object once', status='PASS' if ok else 'FAIL',
                            objects=s['unique'], missing=s['missing'], dups=s['dups']))
        for ev in s.get('events', []):
            exp = ev.get('expect')
            if exp == 'hitless':
                ok = (ev['lost'] or 0) == 0 and ev['dups'] == 0
            elif exp == 'group-loss':
                ok = (ev['lost'] or 0) <= group and ev['dups'] == 0
            else:
                continue
            res.append(dict(check=f'{name} {ev["verb"]} {ev["on"]}-{ev["peer"]}: {exp}', status='PASS' if ok else 'FAIL',
                            lost=ev['lost'], dups=ev['dups'], max_gap_ms=ev['max_gap_ms']))
    if 'namespace_missing' in out:
        res.append(dict(check='namespace reached every relay',
                        status='FAIL' if out['namespace_missing'] else 'PASS', missing=out['namespace_missing']))
    return res


def text(summary):
    """A plain-text table of one run's summary."""
    s = summary
    lines_ = [f'{s["label"]}: {s["scenario"]} on {s["topology"]} ({", ".join(s["links"])}), {s["sent"]} objects sent']
    lines_.append(f'  {"subscriber":<12}{"hops":>5}{"objs":>7}{"miss":>6}{"dups":>6}  '
                  f'{"trace p50/p90/p99 ms":<24}{"tool p50/p95/p99 ms":<22}{"jitter":>7}  setup ms (sub/first)')
    for name, r in s['subscribers'].items():
        t = r['tool']
        tl = '/'.join('-' if r[k] is None else f'{r[k]:.3f}' for k in ('lat_p50', 'lat_p90', 'lat_p99'))
        tool = '/'.join('-' if t.get(k) is None else f'{t[k]:.1f}' for k in ('lat_p50', 'lat_p95', 'lat_p99'))
        mk = r['markers']
        setup = f'{mk.get("subscribed_ms", "-")}/{mk.get("first_object_ms", "-")}'
        lines_.append(f'  {name:<12}{str(r["hops_at_start"]):>5}{r["unique"]:>7}{str(r["missing"]):>6}{r["dups"]:>6}  '
                      f'{tl:<24}{tool:<22}{t.get("jitter_ms", "-")!s:>7}  {setup}')
        for ev in r.get('events', []):
            lines_.append(f'      {ev["verb"]:<12}{ev["on"]}-{ev["peer"]}: gap {ev["max_gap_ms"]} ms, lost {ev["lost"]}, '
                          f'dups {ev["dups"]}, recovery {ev["recovery_ms"]} ms, p50 {ev["lat_p50_before"]} -> '
                          f'{ev["lat_p50_after"]} ms')
    for ev in s.get('events', []):
        lines_.append(f'  event {ev["verb"]} {ev["on"]}-{ev["peer"]} metric={ev.get("metric")} ok={ev["ok"]} '
                      f'status={ev["status"]} took {ev.get("action_ms")} ms; paths {ev.get("path_before")} -> '
                      f'{ev.get("path_after")}')
    if 'namespace_seen' in s:
        lines_.append(f'  namespace seen (ms after ingest): {s["namespace_seen"]}; missing {s["namespace_missing"]}')
    for v in s['verdicts']:
        lines_.append(f'  {v["status"]:<5}{v["check"]}')
    return '\n'.join(lines_)


def main(paths):
    """Analyze each run folder (or every run under a folder); 1 when any verdict failed."""
    status = 0
    for arg in paths:
        arg = Path(arg)
        runs = [arg] if (arg / 'meta.json').is_file() else sorted(m.parent for m in arg.glob('*/meta.json'))
        for run in runs:
            summary = analyze(run)
            (run / 'summary.json').write_text(json.dumps(summary, indent=1) + '\n')
            print(text(summary))
            print()
            if any(v['status'] == 'FAIL' for v in summary['verdicts']):
                status = 1
    return status
