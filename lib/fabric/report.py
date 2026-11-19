"""Fabric run analysis: per-subscriber delivery and latency, fault windows, namespace propagation, setup times.

Times are host epoch seconds: the publisher's and subscribers' lines carry the
time tsline read them, faults carry the host times bracketing each action, and
pollers stamp each sample. One host, one clock.
"""
import csv
import json
from pathlib import Path
import re
import statistics

from fabric import adapter as adapters
from fabric import topology
from fabric.moqx_peering import namespaces_in

SEND = re.compile(r'TRACE: MOQT send: object (\d+)\.(\d+) alias=\d+ (\d+) bytes')
RECV = re.compile(r'TRACE: MOQT stream\(\d+\): object (\d+)\.(\d+) alias=\d+ \d+ bytes status=(\S+)')
DGRAM = re.compile(r'TRACE: MOQT event: (?:d\d+ )?ObjectDatagram: (\d+)\.(\d+) size: \d+ bytes(?: status: (\S+))?')
NORMAL = {None, '0', 'None', 'NORMAL', 'ObjectStatus.NORMAL'}
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
        m = RECV.search(text) or DGRAM.search(text)
        if m:
            if m[3] in NORMAL:
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
        lost = sorted(k for k in expected if k not in seen)
        out.update(first=list(first), last=list(last), missing=len(lost), missing_ids=[f'{g}.{o}' for g, o in lost[:20]])
    else:
        out.update(first=None, last=None, missing=None, missing_ids=[])
    out.update(lat_p50=r3(pct(lat, 50)), lat_p90=r3(pct(lat, 90)), lat_p99=r3(pct(lat, 99)),
               lat_mean=r3(statistics.fmean(lat)) if lat else None)
    return out


def phase(sent, t, group_size):
    """Where in its group the publisher was at time t: 'object k of n (group g)', by the last object sent."""
    before = [(ts, k) for k, ts in sent.items() if ts <= t]
    if not before:
        return None
    _, (g, o) = max(before)
    return f'object {o + 1} of {group_size} (group {g})'


def gaps(sub, t0, t1):
    """Largest inter-arrival gap (ms) and when it ended, over arrivals in [t0, t1]."""
    ts = sorted(a[0] for a in sub['arrivals'] if t0 - 1 <= a[0] <= t1)
    best = (0.0, None)
    for a, b in zip(ts, ts[1:]):
        if b >= t0 and (b - a) * 1000 > best[0]:
            best = ((b - a) * 1000, b)
    return best


def outage(sub, t0, limit_s=60):
    """(last arrival at or before t0, first arrival after t0 within limit_s); either may be None."""
    before = [a[0] for a in sub['arrivals'] if a[0] <= t0]
    after = [a[0] for a in sub['arrivals'] if t0 < a[0] <= t0 + limit_s]
    return (before[-1] if before else None), (after[0] if after else None)


def lost_between(sub, sent, ta, tb):
    """IDs the publisher sent in [ta, tb] that the subscriber never received."""
    got = {(g, o) for _, g, o in sub['arrivals']}
    return sorted(k for k, ts in sent.items() if ta <= ts <= tb and k not in got)


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


def nft_rates(run_dir, meta):
    """{(src, dst): [(t, bytes/s)]} from each relay's per-peer nft counters (VM fabrics)."""
    out = {}
    for n in meta['order']:
        path = Path(run_dir) / f'monitor-{n}.jsonl'
        if not path.is_file():
            continue
        prev = None
        for raw in path.read_text().splitlines():
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            sent = (row.get('nft') or {}).get('out')
            if sent is None:
                continue
            if prev:
                dt = row['t_wall'] - prev[0]
                for peer, (b, _) in sent.items():
                    if peer in prev[1] and dt > 0:
                        out.setdefault((n, peer), []).append((row['t_wall'], (b - prev[1][peer][0]) / dt))
            prev = (row['t_wall'], sent)
    return out


def link_rates(run_dir, meta):
    """{(src, dst): [(t, bytes/s)]}: what each relay sent to each peer, from nft counters on VMs or
    each link interface's counters on Docker."""
    if meta.get('provider') == 'vm':
        return nft_rates(run_dir, meta)
    out = {}
    for n in meta['order']:
        path = Path(run_dir) / f'poll-net-{n}.jsonl'
        if not path.is_file():
            continue
        rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
        ifaddr = next((r['body'] for r in rows if r['name'] == 'ifaddr'), {})
        by_ip = {ip: dev for dev, ip in ifaddr.items()}
        ifaces = {}
        for link in meta['addressing']['links']:
            if n in link['pair'] and link[n] in by_ip:
                ifaces[by_ip[link[n]]] = next(m for m in link['pair'] if m != n)
        prev = None
        for r in (r for r in rows if r['name'] == 'netdev'):
            if prev:
                dt = r['t'] - prev['t']
                for dev, peer in ifaces.items():
                    if dev in r['dev'] and dev in prev['dev'] and dt > 0:
                        rate = (r['dev'][dev][2] - prev['dev'][dev][2]) / dt
                        out.setdefault((n, peer), []).append((r['t'], rate))
            prev = r
    return out


def carrying(rates, t0, t1, floor):
    """Directed links whose mean send rate over [t0, t1] is at least floor bytes/s."""
    out = set()
    for edge, series in rates.items():
        vals = [v for t, v in series if t0 <= t <= t1]
        if vals and sum(vals) / len(vals) >= floor:
            out.add(edge)
    return out


def paths_into(node, edges, ingest, limit=8):
    """Every path from ingest to node over the carrying directed links, as 'a>b>c'."""
    found = []

    def walk(path):
        if path[0] == ingest:
            found.append('>'.join(path))
            return
        if len(path) > limit:
            return
        for src, dst in sorted(edges):
            if dst == path[0] and src not in path:
                walk([src, *path])
    walk([node])
    return found


def clock(run_dir):
    """{host: {tag: (offset_us, rms_us)}} from chronyc tracking snapshots, and the worst-case budget."""
    hosts = {}
    for f in Path(run_dir).glob('clock-*-*.txt'):
        host, tag = f.stem[len('clock-'):].rsplit('-', 1)
        text = f.read_text()
        m = re.search(r'System time\s*:\s*([\d.]+) seconds (fast|slow)', text)
        rms = re.search(r'RMS offset\s*:\s*([\d.]+) seconds', text)
        if m:
            off = float(m[1]) * 1e6 * (1 if m[2] == 'fast' else -1)
            hosts.setdefault(host, {})[tag] = (r3(off), r3(float(rms[1]) * 1e6) if rms else None)
    if not hosts:
        return None
    worst = max(abs(v[0]) for tags in hosts.values() for v in tags.values())
    return {'hosts': hosts, 'budget_us': r3(2 * worst)}


def population(run_dir, name, opts, slo_ms):
    """One load_sim population: its steady window against the gate, and the tool's verdict."""
    rows = []
    path = Path(run_dir) / f'{name}.csv'
    if path.is_file():
        with open(path) as f:
            rows = [r for r in csv.DictReader(f) if r.get('live')]
    rc_path = Path(run_dir) / f'{name}.rc'
    rc = int(rc_path.read_text().strip() or 1) if rc_path.is_file() else None
    per_sub = (opts.get('object_size') or 1000) * 8 * (opts.get('rate') or 30) / 1e6
    start = next((i for i, r in enumerate(rows) if int(r['live']) >= int(r['target'])), None)
    steady = rows[start:-3] if start is not None else []
    good = [r for r in steady if int(r['live']) >= int(r['target'])
            and float(r['rx_mbps']) >= 0.9 * int(r['target']) * per_sub and float(r['p90_ms'] or 0) <= slo_ms]
    target = int(rows[0]['target']) if rows else None
    return dict(target=target, rows=len(rows), steady=len(steady), good=len(good), rc=rc,
                p90_max_ms=r3(max((float(r['p90_ms'] or 0) for r in steady), default=None) if steady else None),
                rx_mbps_min=r3(min((float(r['rx_mbps']) for r in steady), default=None) if steady else None),
                ok=bool(steady) and len(good) >= 0.95 * len(steady) and rc == 0)


def media(run_dir, name):
    """One sub_media --analyze late joiner: time to catalog and to each track's first frame, and its summary."""
    lines_ = lines(Path(run_dir) / f'{name}.log')
    out = dict(catalog_ms=None, first_frame_ms={}, fail=[], tracks={})
    if not lines_:
        return out
    t0 = lines_[0][0]
    for t, text in lines_:
        if out['catalog_ms'] is None and text.strip().startswith('catalog:'):
            out['catalog_ms'] = r3((t - t0) * 1000)
        m = re.match(r'\s+\[([^\]]+)\] g(\d+)\.o(\d+) ', text)
        if m and m[1] not in out['first_frame_ms']:
            out['first_frame_ms'][m[1]] = (r3((t - t0) * 1000), f'{m[2]}.{m[3]}')
        if 'FAIL' in text or 'error:' in text:
            out['fail'].append(text.strip()[:160])
    path = Path(run_dir) / f'{name}.csv'
    if path.is_file():
        with open(path) as f:
            for r in csv.DictReader(f):
                if r.get('kind') == 'summary':
                    out['tracks'][r['track']] = {k: r.get(k) for k in (
                        'objects', 'lost', 'dups', 'lat_p50_ms', 'lat_p95_ms', 'groups', 'partial_groups')}
    return out


def fetched(run_dir, name):
    """A joining FETCH's outcome from its log: ok (FETCH_OK), error (a refusal), or none."""
    out = dict(outcome='none', response=None, stream=False)
    sent = False
    for _, text in lines(Path(run_dir) / f'{name}.log'):
        if 'MOQT send: Fetch(' in text or 'MOQT send: Fetch ' in text:
            sent = True
        elif sent and 'FetchOk' in text and out['outcome'] == 'none':
            out.update(outcome='ok', response=text.strip()[-160:])
        elif sent and ('FetchError' in text or 'RequestError' in text) and out['outcome'] == 'none':
            out.update(outcome='error', response=text.strip()[-160:])
        if 'FetchHeader' in text:
            out['stream'] = True
    out['sent'] = sent
    return out


def peer_node(path, relays):
    """What a peer node learned: nodes, hash checks, and whether it stayed a leaf. A moq-fabric-bench
    report ({"role", "pub", "sub"}) gives that per end, plus what it published and received."""
    try:
        doc = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        return {'error': str(exc)}
    if 'role' in doc:
        out = {'role': doc['role'], 'ends': {}}
        for end in ('pub', 'sub'):
            if isinstance(doc.get(end), dict):
                part = fabric_view(doc[end], relays)
                part['published'] = [dict(track='/'.join([*t.get('ns', []), t.get('name', '')]), objects=t.get('objects'))
                                     for t in doc[end].get('published', [])]
                part['tracks'] = [received(t) for t in doc[end].get('tracks', [])]
                out['ends'][end] = part
        return out
    return fabric_view(doc, relays)


def received(t):
    """One track a moq-fabric-bench subscriber received."""
    lat = t.get('latency_ms') or {}
    head = t.get('header_to_first_object_ms') or {}
    return dict(track='/'.join([*t.get('ns', []), t.get('name', '')]), objects=t.get('objects'), lost=t.get('lost'),
                dups=t.get('dups'), payload_bad=t.get('payload_bad'), datagrams=t.get('datagrams'),
                latency_ms={k: lat.get(k) for k in ('n', 'p50', 'p90', 'p99')},
                header_to_first_object_ms={k: head.get(k) for k in ('p50', 'p99')})


def fabric_view(doc, relays):
    """Nodes learned, hash checks and the leaf rule from one peering node's report."""
    own = (doc.get('node') or {}).get('id')
    learned = sorted({n.get('contact') or n.get('node') for n in doc.get('nodes', [])})
    sessions = doc.get('sessions', [])
    adv = sum((s.get('tx_msgs') or {}).get('NODE_INFO_ADV', 0) for s in sessions)
    via_us = [n.get('contact') for n in doc.get('nodes', []) if any(p.get('node') == own for p in n.get('path', []))]
    out = dict(node=(doc.get('node') or {}).get('contact'), learned=learned,
               missing=sorted(set(relays) - set(learned)),
               sessions_up=(doc.get('counters') or {}).get('sessions_up'),
               dial_failures=(doc.get('counters') or {}).get('dial_failures'))
    for kind in ('announces', 'subscribes'):
        rows = doc.get(kind, [])
        out[f'{kind}_hash_ok'] = [sum(1 for r in rows if r.get('hash_ok')), len(rows)]
    out['leaf'] = dict(node_info_sent=adv, paths_through_us=via_us, held=adv == 0 and not via_us)
    return out


def analyze(run_dir):
    run_dir = Path(run_dir)
    meta = json.loads((run_dir / 'meta.json').read_text())
    order = meta['order']
    links = [tuple(p) for p in meta['links']]
    ingest = topology.resolve(meta['scenario'].get('publisher', 'ingest'), order)
    hop = topology.hops(links, ingest)
    opts = meta.get('options') or {}
    floor = 0.5 * (opts.get('object_size') or 1000) * (opts.get('rate') or 30)
    rates = link_rates(run_dir, meta)
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
    named = meta.get('named_subscribers') or {}
    life = lifetimes(events)
    subs = {}
    for log in sorted(run_dir.glob('*.log')):
        name = log.stem
        if not name.startswith(('sub-', 'cold-', 'warm-')):
            continue
        own = named.get(name[len('sub-'):]) if name.startswith('sub-') else None
        node = own['on'] if own else name.split('-', 1)[1]
        s = subscriber(log)
        row = dict(node=node, hops_at_start=hop.get(node), tool=s['summary'], markers={}, **delivery(s, sent))
        if own:
            row.update(named=True, expect=own.get('expect', 'complete'), life=life.get(name[len('sub-'):]))
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
                if t0 is None or (own and not running(row.get('life'), t0)):
                    continue
                t_next = events[i + 1]['t0'] if i + 1 < len(events) and events[i + 1].get('t0') else t0 + 30
                win_end = min(t0 + 10, t_next)
                gap_ms, gap_end = gaps(s, t0, win_end)
                nominal = 1000 / (meta['options'].get('rate') or 30)
                last_before, first_after = outage(s, t0)
                out_ms = (first_after - last_before) * 1000 if last_before and first_after else 0.0
                if first_after is None:
                    recovery = None
                elif out_ms >= gap_ms and out_ms > 3 * nominal:
                    recovery = r3((first_after - t0) * 1000)
                elif gap_ms > 3 * nominal:
                    recovery = r3((gap_end - t0) * 1000)
                else:
                    recovery = 0.0
                last = s['arrivals'][-1][0]
                if (own and running(row.get('life'), win_end)
                        and any(last + 1 < ts <= win_end for ts in sent.values())):
                    recovery = None
                lost = lost_between(s, sent, t0 - 0.5, max(win_end, first_after or win_end))
                inwin = delivery(s, sent, t0 - 0.5, max(win_end, first_after or win_end))
                before = delivery(s, sent, t0 - 5, t0)
                after = delivery(s, sent, min(t0 + 2, win_end), win_end)
                row['events'].append(dict(
                    verb=ev['verb'], on=ev.get('on'), peer=ev.get('peer'), metric=ev.get('metric'), sub=ev.get('sub'),
                    ok=ev.get('ok'), status=ev.get('status'), expect=ev.get('expect'), label=ev.get('label'),
                    subs=ev.get('subs'), gap_bound_ms=ev.get('max_gap_ms', 2 * nominal),
                    phase=phase(sent, t0, (meta.get('options') or {}).get('group_size') or 30),
                    max_gap_ms=None if recovery is None else r3(max(gap_ms, out_ms)), recovery_ms=recovery,
                    lost=len(lost), lost_ids=[f'{g}.{o}' for g, o in lost[:20]], dups=inwin['dups'],
                    lat_p50_before=before['lat_p50'], lat_p50_after=after['lat_p50']))
        subs[name] = row
    out['subscribers'] = subs
    if events:
        evrows = []
        for ev in events:
            t0 = ev.get('t0')
            row = {k: ev.get(k) for k in ('verb', 'on', 'peer', 'sub', 'metric', 'ok', 'status', 'expect', 'skipped',
                                          'label')}
            if t0:
                row['phase'] = phase(sent, t0, (meta.get('options') or {}).get('group_size') or 30)
            if t0:
                row['action_ms'] = r3((ev['t1'] - t0) * 1000)
                if rates:
                    for label, (a, b) in (('path_before', (t0 - 1.5, t0 - 0.25)), ('path_after', (t0 + 2, t0 + 4))):
                        edges = carrying(rates, a, b, floor)
                        row[label] = {n: paths_into(n, edges, ingest) for n in order if n != ingest}
            evrows.append(row)
        out['events'] = evrows
    if rates:
        t_start, t_end = meta.get('started', 0) + 3, meta.get('ended', 1e12) - 3
        out['link_kBps'] = {f'{a}>{b}': r3(sum(v for t, v in s_ if t_start <= t <= t_end) /
                                          max(1, sum(1 for t, _ in s_ if t_start <= t <= t_end)) / 1000)
                            for (a, b), s_ in sorted(rates.items())}
        if not events:
            edges = carrying(rates, t_start, t_end, floor)
            out['paths'] = {n: paths_into(n, edges, ingest) for n in order if n != ingest}
            for row in subs.values():
                found = out['paths'].get(row['node'])
                if found and len(found) == 1:
                    row['path_hops'] = found[0].count('>')
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
    sampled = {}
    for f in sorted(run_dir.glob('ns-sample-*.json')):
        doc = json.loads(f.read_text())
        found = any(ns == meta['namespace'] or ns.startswith(meta['namespace'] + '/') for ns in doc['namespaces'])
        sampled[f.stem[len('ns-sample-'):]] = (doc['t'], found)
    if prop or sampled:
        base = prop.get(ingest)
        out['namespace_seen'] = {n: r3((t - base) * 1000) if base else None for n, t in prop.items()}
        out['namespace_sampled'] = {n: {'seen': found, 'at_ms': r3((t - base) * 1000) if base else None}
                                    for n, (t, found) in sampled.items()}
        out['namespace_missing'] = ([n for n in meta.get('namespace_nodes', order) if n not in prop]
                                    + [n for n, (_, found) in sampled.items() if not found])
    slo = meta['test'].get('slo_ms', 500)
    pops = {}
    for f in sorted(run_dir.glob('pop-*.rc')) or sorted(run_dir.glob('pop-*.csv')):
        pops[f.stem] = population(run_dir, f.stem, meta.get('options') or {}, slo)
    if pops:
        out['populations'] = pops
    medias = {f.stem: media(run_dir, f.stem) for f in sorted(run_dir.glob('media-*.log'))}
    if medias:
        out['media'] = medias
    fetches = {f.stem: fetched(run_dir, f.stem) for f in sorted(run_dir.glob('fetch-*.log'))}
    if fetches:
        out['fetch'] = fetches
    peers = {f.stem: peer_node(f, order) for f in sorted(run_dir.glob('peer-*.json'))}
    if peers:
        out['peer_nodes'] = peers
    if meta.get('expect_path'):
        out['path_checks'] = path_checks(run_dir, meta, rates, floor)
    out['clock'] = clock(run_dir)
    out['verdicts'] = verdicts(out, meta)
    return out


def lifetimes(events):
    """{named subscriber: [start, end or None]} on the host clock, from its sub.* events."""
    out = {}
    for ev in events:
        if ev.get('verb') == 'sub.start' and ev.get('t0') is not None:
            out[ev['sub']] = [ev['t0'], None]
        elif ev.get('verb') in ('sub.stop', 'sub.kill') and ev.get('sub') in out:
            out[ev['sub']][1] = ev.get('t0')
    return out


def running(life, t):
    """Whether a named subscriber ran across t: its own start and end are not events it sees."""
    return bool(life) and life[0] < t and (life[1] is None or t < life[1])


def where(ev):
    """An event's target for a check or a report line: a subscriber, a node, or a node pair."""
    if ev.get('sub'):
        return f'{ev["sub"]} on {ev.get("on")}'
    return ev.get('on') if ev.get('peer') is None else f'{ev["on"]}-{ev["peer"]}'


def state_samples(run_dir, meta, node):
    """[(t, sample)] from a relay's peering poll, read by its adapter for the run's track; sample is
    None while its control API did not answer."""
    adapter = adapters.get((meta.get('adapters') or {}).get(node, meta.get('adapter')))
    out, last = [], None
    for t, body, status in polls(Path(run_dir) / f'poll-{node}.jsonl').get('peering', []):
        if status != 200:
            last = None
        elif body is not None:
            try:
                last = adapter.sample(json.loads(body), meta['namespace'], meta.get('track', 'probe'))
            except ValueError:
                last = None
        out.append((t, last))
    return out


def carried(samples, rates, node, t0, t1, floor):
    """(carries, source, measure) for [t0, t1]: bytes the relay sent to peers, else data streams open on its
    peer sessions, else its links' send rates; (None, None, None) when nothing tells."""
    win = [s for t, s in samples if t0 <= t <= t1 and s]
    tx = [(t, s['tx_bytes']) for t, s in samples if t0 <= t <= t1 and s and s.get('tx_bytes') is not None]
    if len(tx) >= 2 and tx[-1][0] > tx[0][0]:
        rate = sum(max(0, b - a) for (_, a), (_, b) in zip(tx, tx[1:])) / (tx[-1][0] - tx[0][0])
        return rate >= floor, 'tx_bytes', r3(rate)
    streams = [s['streams'] for s in win if s.get('streams') is not None]
    if streams:
        busy = sum(1 for v in streams if v > 0)
        return 2 * busy >= len(streams), 'streams', f'{busy}/{len(streams)} samples with open streams'
    series = {edge: v for edge, v in rates.items() if edge[0] == node}
    if any(t0 <= t <= t1 for v in series.values() for t, _ in v):
        out = sorted(b for a, b in carrying(series, t0, t1, floor))
        return bool(out), 'links', out
    return None, None, None


def path_checks(run_dir, meta, rates, floor):
    """Each expect_path window against the relay's state samples (or link rates when its state cannot tell)."""
    rows, cache = [], {}
    base = meta.get('t_zero')
    for exp in meta['expect_path']:
        node = exp['node']
        if node not in cache:
            cache[node] = state_samples(run_dir, meta, node)
        samples = cache[node]
        span = f'{exp["from_s"]:g}-{exp["to_s"]:g} s' + (f' ({exp["label"]})' if exp.get('label') else '')
        t0, t1 = (base + exp['from_s'], base + exp['to_s']) if base else (None, None)
        for key in ('carries', 'subscribed', 'own_subscribe'):
            if key not in exp:
                continue
            want = exp[key]
            what = {'carries': ('carries the track', 'carries no track'),
                    'subscribed': ('holds a fabric subscribe for the track', 'holds no fabric subscribe for the track'),
                    'own_subscribe': ('holds its own fabric subscribe', 'holds no fabric subscribe of its own')}[key]
            row = dict(check=f'path: {node} {what[0 if want else 1]}, {span}', node=node, state=key, want=want)
            if base is None:
                rows.append(dict(row, status='SKIP', reason='no run start time'))
                continue
            if key == 'carries':
                got, source, measure = carried(samples, rates, node, t0, t1, floor)
            else:
                vals = [s[key] for t, s in samples if t0 <= t <= t1 and s and s.get(key) is not None]
                got = None if not vals else (all(vals) if want else any(vals))
                source, measure = ('state', f'{sum(map(bool, vals))}/{len(vals)} samples true') if vals else (None, None)
            if got is None:
                rows.append(dict(row, status='SKIP', reason=f'no {key} samples from {node} in the window'))
                continue
            rows.append(dict(row, status='PASS' if got == want else 'FAIL', source=source, measured=measure))
    return rows


def expect(ok, expected):
    """PASS or FAIL against an expected pass; XFAIL or XPASS against an expected failure."""
    if expected == 'fail':
        return 'XPASS' if ok else 'XFAIL'
    return 'PASS' if ok else 'FAIL'


def verdicts(out, meta):
    opts = meta.get('options') or {}
    group = opts.get('group_size') or 30
    scen = meta.get('scenario') or {}
    order = meta['order']
    ingest = topology.resolve(scen.get('publisher', 'ingest'), order)
    res = []
    remote = scen.get('expect_remote')
    if remote and (meta.get('adapters') or {}).get(ingest, meta.get('adapter')) != remote.get('publisher_adapter'):
        remote = None
    for name, s in out['subscribers'].items():
        if s.get('named'):
            if s.get('expect') == 'complete':
                ok = s['unique'] > 0 and s['dups'] == 0 and (s['missing'] or 0) == 0
                res.append(dict(check=f'{name} complete from its first object', status='PASS' if ok else 'FAIL',
                                objects=s['unique'], missing=s['missing'], dups=s['dups']))
        elif not s.get('events'):
            ok = s['unique'] > 0 and s['dups'] == 0 and (s['missing'] or 0) == 0
            row = dict(check=f'{name} delivered every object once', status='PASS' if ok else 'FAIL',
                       objects=s['unique'], missing=s['missing'], dups=s['dups'])
            if remote and s['node'] != ingest:
                row.update(status=expect(ok, 'fail'), defect=remote['defect'])
            res.append(row)
        for ev in s.get('events', []):
            exp = ev.get('expect')
            if ev.get('subs') is not None and name[len('sub-'):] not in ev['subs']:
                continue
            gap = ev['max_gap_ms']
            if exp == 'hitless':
                ok = (ev['lost'] or 0) == 0 and ev['dups'] == 0
            elif exp == 'group-loss':
                ok = (ev['lost'] or 0) <= group and ev['dups'] == 0
            elif exp == 'no-gap':
                ok = (ev['lost'] or 0) == 0 and ev['dups'] == 0 and gap is not None and gap <= ev['gap_bound_ms']
            elif exp == 'bounded-gap':
                ok = gap is not None and gap <= ev['gap_bound_ms']
            else:
                continue
            at = where(ev) + (f' ({ev["label"]})' if ev.get('label') else '')
            row = dict(check=f'{name} {ev["verb"]} {at}: {exp}', status='PASS' if ok else 'FAIL',
                       lost=ev['lost'], dups=ev['dups'], max_gap_ms=gap)
            if exp in ('no-gap', 'bounded-gap'):
                row['gap_bound_ms'] = ev['gap_bound_ms']
            res.append(row)
    for name, pop in (out.get('populations') or {}).items():
        res.append(dict(check=f'{name}: {pop["target"]} subscribers held the gate', status=expect(pop['ok'], None),
                        good=pop['good'], steady=pop['steady'], p90_max_ms=pop['p90_max_ms'], rc=pop['rc']))
    if out.get('populations') and out.get('link_kBps'):
        payload = (opts.get('object_size') or 1000) * (opts.get('rate') or 30) / 1000
        carrying = {k: v for k, v in out['link_kBps'].items() if v >= 0.5 * payload}
        ok = bool(carrying) and all(v <= 1.6 * payload for v in carrying.values())
        res.append(dict(check='each link carrying the track carries it once', status=expect(ok, None),
                        payload_kBps=r3(payload), carrying=carrying))
    for name, m in (out.get('media') or {}).items():
        ok = (m['catalog_ms'] is not None and bool(m['tracks']) and not m['fail']
              and all(int(t['objects'] or 0) > 0 and int(t['dups'] or 0) == 0 for t in m['tracks'].values()))
        res.append(dict(check=f'{name}: catalog and every track after a late join', status=expect(ok, None),
                        catalog_ms=m['catalog_ms']))
    for name, f in (out.get('fetch') or {}).items():
        node = name.split('-', 1)[1]
        across = node != ingest
        exp = scen.get('expect_across', 'pass') if across else None
        res.append(dict(check=f'{name}: joining FETCH {"across peering" if across else "on the publisher relay"}',
                        status=expect(f['outcome'] == 'ok', exp), outcome=f['outcome'], response=f['response']))
    for name, pn in (out.get('peer_nodes') or {}).items():
        ends = pn.get('ends', {name: pn}) if 'error' not in pn else {}
        ok = 'error' not in pn and bool(ends) and all(
            not e['missing'] and e['leaf']['held']
            and all(h[0] == h[1] for h in (e['announces_hash_ok'], e['subscribes_hash_ok'])) for e in ends.values())
        res.append(dict(check=f'{name}: learned every relay, every hash checked, stayed a leaf',
                        status=expect(ok, None), error=pn.get('error')))
        dials = (meta.get('peer_dials') or {}).get(name[len('peer-'):], [])
        for end, e in ends.items():
            for t in e.get('tracks', []):
                good = (t['objects'] or 0) > 0 and not t['lost'] and not t['dups'] and not t['payload_bad']
                row = dict(check=f'{name} {end}: {t["track"]} received every object once over peering',
                           status=expect(good, None), objects=t['objects'], lost=t['lost'], dups=t['dups'])
                if remote and dials and ingest not in dials:
                    row.update(status=expect(good, 'fail'), defect=remote['defect'])
                res.append(row)
    if 'namespace_missing' in out:
        res.append(dict(check='namespace reached every relay',
                        status='FAIL' if out['namespace_missing'] else 'PASS', missing=out['namespace_missing']))
    res += out.get('path_checks', [])
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
        hops = r.get('path_hops', r['hops_at_start'])
        lines_.append(f'  {name:<12}{str(hops):>5}{r["unique"]:>7}{str(r["missing"]):>6}{r["dups"]:>6}  '
                      f'{tl:<24}{tool:<22}{t.get("jitter_ms", "-")!s:>7}  {setup}')
        for ev in r.get('events', []):
            lost = f' {ev["lost_ids"]}' if ev.get('lost_ids') else ''
            if ev.get('label'):
                lines_.append(f'      [{ev["label"]}; at {ev.get("phase")}]')
            lines_.append(f'      {ev["verb"]:<12}{where(ev)}: gap {ev["max_gap_ms"]} ms, lost {ev["lost"]}{lost}, '
                          f'dups {ev["dups"]}, recovery {ev["recovery_ms"]} ms, p50 {ev["lat_p50_before"]} -> '
                          f'{ev["lat_p50_after"]} ms')
    for ev in s.get('events', []):
        at = where(ev)
        if ev.get('label'):
            at += f' [{ev["label"]}; at {ev.get("phase")}]'
        lines_.append(f'  event {ev["verb"]} {at} metric={ev.get("metric")} ok={ev["ok"]} '
                      f'status={ev["status"]} took {ev.get("action_ms")} ms; paths {ev.get("path_before")} -> '
                      f'{ev.get("path_after")}')
    if s.get('link_kBps'):
        lines_.append(f'  link send kB/s: {s["link_kBps"]}')
    if s.get('paths'):
        lines_.append(f'  paths carrying the track: {s["paths"]}')
    for name, p in (s.get('populations') or {}).items():
        lines_.append(f'  {name}: target {p["target"]}, {p["good"]}/{p["steady"]} steady samples at the gate, '
                      f'p90 max {p["p90_max_ms"]} ms, rx min {p["rx_mbps_min"]} Mbps, exit {p["rc"]}')
    for name, m in (s.get('media') or {}).items():
        lines_.append(f'  {name}: catalog {m["catalog_ms"]} ms, first frames {m["first_frame_ms"]}, tracks {m["tracks"]}')
    for name, f in (s.get('fetch') or {}).items():
        lines_.append(f'  {name}: {f["outcome"]}{" (fetch stream seen)" if f["stream"] else ""} {f["response"] or ""}')
    for name, pn in (s.get('peer_nodes') or {}).items():
        if 'error' in pn:
            lines_.append(f'  {name}: no report ({pn["error"]})')
            continue
        for end, e in pn.get('ends', {'': pn}).items():
            label = f'{name} {end}'.strip()
            lines_.append(f'  {label}: learned {e["learned"]}, announces hash_ok {e["announces_hash_ok"][0]}/'
                          f'{e["announces_hash_ok"][1]}, subscribes {e["subscribes_hash_ok"][0]}/'
                          f'{e["subscribes_hash_ok"][1]}, NODE_INFO sent {e["leaf"]["node_info_sent"]}, '
                          f'paths through it {e["leaf"]["paths_through_us"]}, sessions up {e["sessions_up"]}')
            for t in e.get('tracks', []):
                lat = t['latency_ms']
                lines_.append(f'    {t["track"]}: {t["objects"]} objects, lost {t["lost"]}, dups {t["dups"]}, '
                              f'payload_bad {t["payload_bad"]}, latency p50/p90/p99 {lat["p50"]}/{lat["p90"]}/'
                              f'{lat["p99"]} ms, header to first object p50 {t["header_to_first_object_ms"]["p50"]} ms')
            for t in e.get('published', []):
                lines_.append(f'    published {t["track"]}: {t["objects"]} objects')
    if s.get('clock'):
        lines_.append(f'  clock budget {s["clock"]["budget_us"]} us: {s["clock"]["hosts"]}')
    if 'namespace_seen' in s:
        lines_.append(f'  namespace seen (ms after ingest): {s["namespace_seen"]}; sampled once: '
                      f'{s.get("namespace_sampled")}; missing {s["namespace_missing"]}')
    for v in s['verdicts']:
        lines_.append(f'  {v["status"]:<6}{v["check"]}' + (f' (known: {v["defect"]})' if v.get('defect') else ''))
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
            if any(v['status'] in ('FAIL', 'XPASS') for v in summary['verdicts']):
                status = 1
    return status
