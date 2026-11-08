#!/usr/bin/env python3
"""Dashboard datasets from a bin/sweep.sh directory: runs.json, latency.json, sut.json, loadgen.json.

    ./bin/sweep-report.py results/sweep-<name> [-o OUT]   # OUT defaults to <sweep>/datasets

Each run directory is relay-load.sh's output: sub<i>.log (adaptive_bench's 10 s
table, or moqperf_test_client's [AGGREGATE] lines), telemetry-relay.jsonl and
telemetry-sub<i>.jsonl from remote/load-monitor.py, and sweep.sh's
sweep-point.json. Monitor counters become 5 s rates, as the datasets expect.
"""
import argparse
import calendar
import json
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
import telemetry  # noqa: E402
from telemetry import AGG, BW, ROW, ms  # noqa: E402

SHAPES = json.loads((ROOT / 'scenarios/shapes.json').read_text())
BUILDS = {'pgo': 'PGO+LTO', 'reg': 'regular'}
TOOLS = {'aio': 'aiomoqt adaptive_bench', 'mp': 'moqx perf (moqperf_test_client)'}
STRIDE = 5  # monitor samples are 1 s apart; dataset rows are 5 s apart


def relay_fields(pt):
    """The relay under test, and moq-dev's runtime mode; the moqx_* fields carry whichever relay ran."""
    relay = pt.get('relay', 'moqx')
    out = {'relay': relay}
    if relay == 'moqdev':
        out['relay_runtime'] = pt.get('env', {}).get('MOQDEV_RUNTIME', 'workers')
    return out

def sub_rows(path):
    """adaptive_bench's per-host table rows."""
    out = []
    for line in path.open(errors='replace'):
        m = ROW.match(line)
        if m:
            g = m.groups()
            out.append({'t': int(g[0]), 'subs': int(g[8]), 'target_gbps': float(g[1]) * BW[g[2]],
                        'rx_gbps': float(g[6]) * BW[g[7]], 'mean_ms': ms(g[9]), 'p90_ms': ms(g[10]),
                        'loss_pct': float(g[12]), 'action': g[13].strip()})
    return out


def mp_rows(path, year):
    """moqperf_test_client's [AGGREGATE] lines; glog stamps carry no year and are UTC."""
    out = []
    for line in path.open(errors='replace'):
        m = AGG.match(line)
        if m:
            g = m.groups()
            t = calendar.timegm((year, int(g[0]), int(g[1]), int(g[2]), int(g[3]), int(g[4])))
            out.append({'t': t, 'subs': int(g[6]), 'objs': int(g[7]), 'mbps': float(g[8]), 'lat_avg': float(g[10]),
                        'lat_max': float(g[11]), 'resets': int(g[13]), 'failures': int(g[15])})
    return out


def series(path, stride=STRIDE):
    """Monitor JSONL -> (hostname, rows of host load and the watched processes over each stride)."""
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    meta, hz = records[0], records[0]['clock_ticks']
    samples = [r for r in records if 'monotonic' in r][::stride]
    rows = []
    for a, b in zip(samples, samples[1:]):
        r = telemetry.rates(a, b, hz)
        dt = r['interval_s']
        an, bn = telemetry.net(a['netdev']), telemetry.net(b['netdev'])
        ifs = (an.keys() & bn.keys()) - {'lo'}
        kpps = lambda i: round(sum(bn[k][i] - an[k][i] for k in ifs) / dt / 1e3, 1)  # noqa: E731
        if meta.get('pid'):
            cores = -1 if r['process_cores'] == '' else r['process_cores']
            threads, rss = len(b.get('threads') or {}), r['process_rss_bytes'] or 0
        elif a.get('comm') and b.get('comm'):
            cores = (b['comm']['ticks'] - a['comm']['ticks']) / hz / dt
            threads, rss = b['comm']['threads'], b['comm']['rss_bytes']
        else:
            cores, threads, rss = 0, 0, 0
        rows.append({'t': b['t_wall'], 'busy': round(r['host_busy_pct'], 1), 'max_cpu': round(r['hottest_cpu_pct'], 1),
                     'rx_gbps': round(r['net_rx_mbps'] / 1e3, 2), 'tx_gbps': round(r['net_tx_mbps'] / 1e3, 2),
                     'rx_kpps': kpps(1), 'tx_kpps': kpps(9),
                     'udp_rcvbuf_err': r['udp_RcvbufErrors_delta'], 'udp_sndbuf_err': r['udp_SndbufErrors_delta'],
                     'proc_cores': round(cores, 2), 'proc_threads': threads, 'proc_rss_gb': round(rss / 2**30, 2)})
    return meta.get('hostname', path.stem), rows


def point(d):
    """sweep-point.json, else what run.txt and the run name say."""
    p = d / 'sweep-point.json'
    pt = json.loads(p.read_text()) if p.exists() else {}
    run_txt = (d / 'run.txt').read_text() if (d / 'run.txt').exists() else ''
    if 'tool' not in pt:
        pt['tool'] = 'mp' if 'moqperf-ramp vs' in run_txt else 'aio'
    if 'shape' not in pt:
        m = re.search(r'shape (\S+)', run_txt)
        pt['shape'] = m.group(1) if m else '?'
    if 'build' not in pt:
        m = re.search(r'-(pgo|reg)\d*-', d.name)
        pt['build'] = m.group(1) if m else '?'
    m = re.search(r'relay_cpus=(\S+)', run_txt)
    pt['relay_cpus'] = cpu_count(m.group(1)) if m else None
    m = re.search(r'^threads=(\d+)', run_txt, re.M)
    env = pt.get('env', {})
    pt['threads'] = int(env.get('THREADS') or (m.group(1) if m else 0))
    pt['threshold_ms'] = int(env.get('LATENCY_MS') or 500)
    pt['load_boxes'] = len(subs(d))
    return pt


def cpu_count(spec):
    """CPUs in a Linux CPU list such as 2-5,8; None for the default."""
    if not re.fullmatch(r'\d+(-\d+)?(,\d+(-\d+)?)*', spec):
        return None
    return sum(int(b or a) - int(a) + 1 for a, _, b in (p.partition('-') for p in spec.split(',')))


def efficiency(pt, pl, subs):
    """The relay's cost at the plateau: per measured core of its process, and memory per subscriber."""
    cores = sum(x['moqx_cores'] for x in pl) / len(pl)
    gbps = sum(x['moqx_tx_gbps'] for x in pl) / len(pl)
    rss = max(x['relay_rss_gb'] for x in pl)
    return {'relay_cpus': pt.get('relay_cpus'), 'relay_rss_gb': rss,
            'relay_mb_per_sub': round(1024 * rss / subs, 2) if subs else None,
            'subs_per_core': round(subs / cores) if cores > 0 else None,
            'mbps_per_core': round(1000 * gbps / cores) if cores > 0 else None}


def subs(d):
    return sorted(d.glob('sub*.log'), key=lambda p: int(re.sub(r'\D', '', p.stem) or 0))


def monitors(d):
    """Relay series, and each sub host's (hostname, series) in sub index order."""
    relay = d / 'telemetry-relay.jsonl'
    if not relay.exists():
        return None, []
    return series(relay)[1], [series(d / f'telemetry-{p.stem}.jsonl') for p in subs(d)
                              if (d / f'telemetry-{p.stem}.jsonl').exists()]


def limit_of(pl, reached):
    relay_hot = max(x['relay_busiest_cpu_pct'] for x in pl) >= 90
    tool_hot = max(x['loadbox_busy_max_pct'] for x in pl) >= 85 or max(x['loadbox_busiest_cpu_pct'] for x in pl) >= 97
    tool_warm = sum(x['loadbox_busy_max_pct'] for x in pl) / len(pl) >= 60
    return ('not reached' if not reached else 'relay and load generator' if relay_hot and tool_hot else
            'load generator' if tool_hot else 'relay, load boxes warm' if relay_hot and tool_warm else
            'relay' if relay_hot else 'latency, no CPU saturated')


def sut_row(rid, name, build, thr, t0, s, subs_now, lr):
    return {'run': rid, 'run_name': name, 'build': build, 'threads': thr, 't_s': round(s['t'] - t0), 'subs': subs_now,
            'moqx_cores': s['proc_cores'], 'moqx_tx_gbps': s['tx_gbps'], 'moqx_tx_mpps': round(s['tx_kpps'] / 1000, 3),
            'relay_rx_kpps': s['rx_kpps'], 'relay_tx_kpps': s['tx_kpps'], 'relay_busy_pct': s['busy'],
            'relay_busiest_cpu_pct': s['max_cpu'], 'relay_rss_gb': s['proc_rss_gb'],
            'udp_buf_errors': s['udp_rcvbuf_err'] + s['udp_sndbuf_err'],
            'loadbox_busy_max_pct': max(r['busy'] for r in lr.values()),
            'loadbox_busiest_cpu_pct': max(r['max_cpu'] for r in lr.values())}


def loadgen_row(rid, h, t_s, k, r):
    return {'run': rid, 'box': h, 't_s': t_s, 'subs': k, 'cores': r['proc_cores'],
            'mcores_per_sub': round(1000 * r['proc_cores'] / k, 2), 'rss_gb': r['proc_rss_gb'],
            'mb_per_sub': round(1024 * r['proc_rss_gb'] / k, 2), 'nic_rx_gbps': r['rx_gbps'],
            'cores_per_gbps': round(r['proc_cores'] / r['rx_gbps'], 2) if r['rx_gbps'] > 0.5 else None,
            'busy_pct': r['busy'], 'busiest_cpu_pct': r['max_cpu']}


def near(rows, t):
    return min(rows, key=lambda r: abs(r['t'] - t))


def aio_run(d, pt, ds):
    rid, build, thr, boxes, shape, thresh = (d.name, BUILDS.get(pt['build'], pt['build']), pt['threads'],
                                             pt['load_boxes'], pt['shape'], pt['threshold_ms'])
    name = f"{build}, {thr} threads, {boxes} load box{'es' if boxes > 1 else ''}, {shape}, {thresh} ms"
    per_box = [sub_rows(f) for f in subs(d)]
    sut, loads = monitors(d)
    if not per_box or not sut or not loads or not min(map(len, per_box)):
        return f'{rid}: no table rows or monitor series'
    latency = []
    for i in range(min(len(r) for r in per_box)):
        rows = [r[i] for r in per_box]
        p90 = [r['p90_ms'] for r in rows if r['p90_ms'] is not None]
        latency.append({'run': rid, 'run_name': name, 't_s': rows[0]['t'], 'subs': sum(r['subs'] for r in rows),
                        'p90_worst_ms': round(max(p90), 1) if p90 else None,
                        'mean_worst_ms': round(max((r['mean_ms'] or 0) for r in rows), 1),
                        'target_gbps': round(sum(r['target_gbps'] for r in rows), 2),
                        'rx_reported_gbps': round(sum(r['rx_gbps'] for r in rows), 2),
                        'loss_pct': max(r['loss_pct'] for r in rows)})
    loads = dict(loads)
    base = {h: rows[0]['proc_threads'] for h, rows in loads.items()}  # load-tool threads before the ramp
    t0, rs, loadgen = sut[0]['t'], [], []
    for s in sut:
        if s['proc_cores'] < 0:
            continue
        lr = {h: near(rows, s['t']) for h, rows in loads.items()}
        if any(abs(r['t'] - s['t']) > 4 for r in lr.values()):
            continue
        rs.append(sut_row(rid, name, build, thr, t0, s, sum(max(r['proc_threads'] - base[h], 0) for h, r in lr.items()), lr))
    for h, rows in loads.items():
        for r in rows:
            k = r['proc_threads'] - base[h]
            if k >= 100 and r['proc_cores'] > 0:
                loadgen.append(loadgen_row(rid, h, round(r['t'] - t0), k, r))
    if not rs:
        return f'{rid}: no aligned monitor samples'
    peak = max(x['subs'] for x in rs)
    pl = [x for x in rs if x['subs'] >= 0.95 * peak]
    avg = lambda k: sum(x[k] for x in pl) / len(pl)  # noqa: E731
    eq = [x for x in latency if x['subs'] >= 0.95 * max(y['subs'] for y in latency)]
    p90s = sorted(x['p90_worst_ms'] for x in eq if x['p90_worst_ms'] is not None)
    reached = any(x['p90_worst_ms'] and x['p90_worst_ms'] > thresh for x in latency)
    ds['latency'] += latency; ds['sut'] += rs; ds['loadgen'] += loadgen
    ds['runs'].append({
        'run': rid, 'run_name': name, 'build': build, 'moqx_threads': thr, 'load_boxes': boxes, 'shape': shape,
        'mbps_per_sub': SHAPES.get(shape, {}).get('nominal_mbps'), 'threshold_ms': thresh,
        'subs_plateau': round(sum(x['subs'] for x in eq) / len(eq)),
        'p90_median_ms': p90s[len(p90s) // 2] if p90s else None,
        'moqx_tx_gbps': round(avg('moqx_tx_gbps'), 1), 'moqx_tx_mpps': round(avg('moqx_tx_mpps'), 2),
        'moqx_cores': round(avg('moqx_cores'), 1),
        'relay_busiest_cpu_pct': round(max(x['relay_busiest_cpu_pct'] for x in pl), 1),
        'loadbox_busy_max_pct': round(avg('loadbox_busy_max_pct'), 1),
        'loadbox_busiest_cpu_pct': round(max(x['loadbox_busiest_cpu_pct'] for x in pl), 1),
        'udp_buf_errors': sum(x['udp_buf_errors'] for x in rs), 'loss_pct_max': max(x['loss_pct'] for x in latency),
        'limit': limit_of(pl, reached), 'tool': TOOLS['aio'], **relay_fields(pt)})
    ds['runs'][-1].update(efficiency(pt, pl, ds['runs'][-1]['subs_plateau']))


def mp_run(d, pt, ds):
    rid, build, thr, boxes, shape, thresh = (d.name, BUILDS.get(pt['build'], pt['build']), pt['threads'],
                                             pt['load_boxes'], pt['shape'], pt['threshold_ms'])
    name = f"moqperf, {shape} shape; {build}, {thr} threads, {boxes} load box{'es' if boxes > 1 else ''}"
    sut, loads = monitors(d)
    if not sut or len(loads) != boxes:
        return f'{rid}: monitor series missing'
    year = time.gmtime(sut[0]['t']).tm_year
    per = {h: mp_rows(f, year) for (h, _), f in zip(loads, subs(d))}
    if not all(per.values()):
        return f'{rid}: no [AGGREGATE] lines'
    loads = dict(loads)
    by_t = {h: {r['t']: r for r in rows} for h, rows in per.items()}

    def at(box, t):
        rows = [by_t[box][x] for x in (t - 1, t, t + 1) if x in by_t[box]]
        return rows[-1] if rows else None
    t0, rs, loadgen = sut[0]['t'], [], []
    for s in sut:
        now = [at(b, round(s['t'])) for b in per]
        if s['proc_cores'] < 0 or any(r is None for r in now):
            continue
        lr = {h: near(rows, s['t']) for h, rows in loads.items()}
        objs = sum(r['objs'] for r in now) or 1
        row = sut_row(rid, name, build, thr, t0, s, sum(r['subs'] for r in now), lr)
        row.update({'mp_lat_avg_ms': round(sum(r['lat_avg'] * r['objs'] for r in now) / objs, 1),
                    'mp_lat_max_ms': max(r['lat_max'] for r in now),
                    'mp_rx_gbps': round(sum(r['mbps'] for r in now) / 1000, 2),
                    'mp_resets': sum(r['resets'] for r in now), 'mp_failures': sum(r['failures'] for r in now)})
        rs.append(row)
        for h, r in lr.items():
            c = at(h, round(s['t']))
            if c and c['subs'] >= 100 and r['proc_cores'] > 0:
                loadgen.append(loadgen_row(rid, h, round(s['t'] - t0), c['subs'], r))
    if not rs:
        return f'{rid}: no aligned samples'
    peak = max(x['subs'] for x in rs)
    pl = [x for x in rs if x['subs'] >= 0.95 * peak]
    avg = lambda k: sum(x[k] for x in pl) / len(pl)  # noqa: E731
    lat_max = sorted(x['mp_lat_max_ms'] for x in pl)
    failed = sum(x['mp_failures'] + x['mp_resets'] for x in rs) > 0
    reached = lat_max[len(lat_max) // 2] > thresh or failed
    ds['sut'] += rs; ds['loadgen'] += loadgen
    ds['runs'].append({
        'run': rid, 'run_name': name, 'tool': TOOLS['mp'], 'build': build, 'moqx_threads': thr, 'load_boxes': boxes,
        'shape': shape, 'mbps_per_sub': SHAPES.get(shape, {}).get('nominal_mbps'), 'threshold_ms': thresh,
        'subs_plateau': round(avg('subs')), 'p90_median_ms': None,
        'mp_lat_avg_median_ms': sorted(x['mp_lat_avg_ms'] for x in pl)[len(pl) // 2],
        'mp_lat_max_median_ms': lat_max[len(lat_max) // 2],
        'moqx_tx_gbps': round(avg('moqx_tx_gbps'), 1), 'moqx_tx_mpps': round(avg('moqx_tx_mpps'), 2),
        'moqx_cores': round(avg('moqx_cores'), 1),
        'relay_busiest_cpu_pct': round(max(x['relay_busiest_cpu_pct'] for x in pl), 1),
        'loadbox_busy_max_pct': round(avg('loadbox_busy_max_pct'), 1),
        'loadbox_busiest_cpu_pct': round(max(x['loadbox_busiest_cpu_pct'] for x in pl), 1),
        'udp_buf_errors': sum(x['udp_buf_errors'] for x in rs), 'loss_pct_max': None,
        'resets_total': max(x['mp_resets'] for x in rs), 'failures_total': max(x['mp_failures'] for x in rs),
        'limit': limit_of(pl, reached), **relay_fields(pt)})
    ds['runs'][-1].update(efficiency(pt, pl, ds['runs'][-1]['subs_plateau']))


def run_dirs(sweep):
    dirs = {p.parent for p in sweep.glob('*/run.txt')} | {p.parent for p in sweep.glob('*/sweep-point.json')}
    order = {}
    plan = sweep / 'plan'
    if plan.exists():
        for line in plan.read_text().splitlines():
            f = line.split('#')[0].split()
            if len(f) == 6:
                order.setdefault(f[5], len(order))
    return sorted(dirs, key=lambda p: (order.get(p.name, len(order)), p.name))


def report(sweep):
    ds = {'runs': [], 'latency': [], 'sut': [], 'loadgen': []}
    skipped = []
    for d in run_dirs(sweep):
        pt = point(d)
        why = (mp_run if pt['tool'] == 'mp' else aio_run)(d, pt, ds)
        if why:
            skipped.append(why)
    return ds, skipped


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('sweep', type=Path)
    p.add_argument('-o', '--out', type=Path)
    args = p.parse_args()
    out = args.out or args.sweep / 'datasets'
    ds, skipped = report(args.sweep)
    out.mkdir(parents=True, exist_ok=True)
    for k, v in ds.items():
        (out / f'{k}.json').write_text(json.dumps(v, separators=(',', ':')))
        print(k, len(v), (out / f'{k}.json').stat().st_size, 'bytes')
    for r in ds['runs']:
        print({k: r.get(k) for k in ('run', 'subs_plateau', 'p90_median_ms', 'mp_lat_max_median_ms', 'moqx_tx_gbps',
                                     'moqx_tx_mpps', 'moqx_cores', 'relay_cpus', 'subs_per_core', 'mbps_per_core',
                                     'relay_rss_gb', 'relay_mb_per_sub', 'relay_busiest_cpu_pct',
                                     'loadbox_busy_max_pct', 'limit')})
    for why in skipped:
        print('SKIPPED', why)
    return 1 if not ds['runs'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
