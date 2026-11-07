import calendar
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'tests/fixtures/sweep'
spec = importlib.util.spec_from_file_location('sweep_report', ROOT / 'bin/sweep-report.py')
sr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sr)


def sample(t, busy, net, proc_ticks=None, comm=None, udp_err=0):
    """One load-monitor.py snapshot after t seconds of constant load.

    busy: per-CPU busy fractions; net: (rx_bytes, rx_pkts, tx_bytes, tx_pkts) per second;
    proc_ticks: relay process ticks per second; comm: (ticks/s, threads, rss_bytes)."""
    cpus = [(round(f * 100 * t), round((1 - f) * 100 * t)) for f in busy]
    line = lambda name, u, i: f'{name} {u} 0 0 {i} 0 0 0 0 0 0'  # noqa: E731
    cpu = '\n'.join([line('cpu', sum(u for u, _ in cpus), sum(i for _, i in cpus))] +
                    [line(f'cpu{k}', u, i) for k, (u, i) in enumerate(cpus)])
    rb, rp, tb, tp = (round(x * t) for x in net)
    netdev = ('Inter-|   Receive |  Transmit\n face |bytes packets\n'
              f'  eth0: {rb} {rp} 0 0 0 0 0 0 {tb} {tp} 0 0 0 0 0 0\n    lo: 9 9 0 0 0 0 0 0 9 9 0 0 0 0 0 0\n')
    snmp = ('Udp: InDatagrams NoPorts InErrors OutDatagrams RcvbufErrors SndbufErrors\n'
            f'Udp: 0 0 0 0 {udp_err * t} 0\n')
    s = dict(t_wall=T0 + t, monotonic=1000.0 + t, errors=[], cpu=cpu, netdev=netdev, snmp=snmp)
    if proc_ticks is not None:
        s['process'] = dict(pid=42, name='moqx', state='S', ticks=proc_ticks * t, start_ticks=7,
                            rss_bytes=3 * 2**30, cpu=0)
    if comm:
        s['comm'] = dict(ticks=comm[0] * t, threads=comm[1](t), rss_bytes=comm[2], count=2)
    return s


T0 = calendar.timegm((2026, 11, 4, 17, 8, 20))


def write_monitor(path, host, pid, samples):
    lines = [dict(type='metadata', hostname=host, clock_ticks=100, pid=pid, comm=[])] + samples + [dict(type='end')]
    path.write_text('\n'.join(json.dumps(x) for x in lines) + '\n')


def relay_series(n):
    return [sample(t, [0.95, 0.5, 0.5, 0.5], (1.25e8, 1e5, 1.25e9, 1e6), proc_ticks=200, udp_err=1)
            for t in range(n)]


def load_series(n, threads):
    return [sample(t, [0.4, 0.6], (1.25e9, 1e6, 1e7, 1e4), comm=(150, threads, 2 * 2**30)) for t in range(n)]


class SweepReport(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_dir(self, name, tool, logs, n, threads=lambda t: 10):
        d = self.tmp / name
        d.mkdir()
        for i, log in enumerate(logs):
            shutil.copy(FIX / log, d / f'sub{i}.log')
            write_monitor(d / f'telemetry-sub{i}.jsonl', f'load-{"ab"[i]}', 0, load_series(n, threads))
        write_monitor(d / 'telemetry-relay.jsonl', 'relay', 42, relay_series(n))
        (d / 'sweep-point.json').write_text(json.dumps(dict(
            run=name, tool=tool, build='pgo', shape='1080p', env=dict(THREADS='60', LATENCY_MS='500'))))
        (d / 'run.txt').write_text('threads=60 relay_cpus=default subscribers_per_host=5000\n')
        return d

    def test_adaptive_rows_parse(self):
        rows = sr.sub_rows(FIX / 'adaptive-sub0.log')
        self.assertEqual(len(rows), 15)
        self.assertEqual(rows[0], {'t': 160, 'subs': 1000, 'target_gbps': 2.0, 'rx_gbps': 2.0, 'mean_ms': 100.0,
                                   'p90_ms': 200.0, 'loss_pct': 0.0, 'action': 'ramp'})

    def test_adaptive_run(self):
        # Sub threads grow by 100/s: 500 subscribers per box per 5 s row over the first row.
        self.run_dir('aio-pgo60-2b-1080', 'aio', ['adaptive-sub0.log', 'adaptive-sub1.log'], 31,
                     threads=lambda t: 10 + 100 * t)
        ds, skipped = sr.report(self.tmp)
        self.assertEqual(skipped, [])
        self.assertEqual(len(ds['latency']), 15)
        self.assertEqual(ds['latency'][0], {
            'run': 'aio-pgo60-2b-1080', 'run_name': 'PGO+LTO, 60 threads, 2 load boxes, 1080p, 500 ms', 't_s': 160,
            'subs': 2000, 'p90_worst_ms': 200.0, 'mean_worst_ms': 100.0, 'target_gbps': 4.0,
            'rx_reported_gbps': 4.2, 'loss_pct': 0.0})
        self.assertEqual(len(ds['sut']), 6)
        s = ds['sut'][0]
        self.assertEqual((s['t_s'], s['subs'], s['moqx_cores'], s['moqx_tx_gbps'], s['moqx_tx_mpps']),
                         (0, 0, 2.0, 10.0, 1.0))
        self.assertEqual((s['relay_rx_kpps'], s['relay_tx_kpps'], s['relay_busiest_cpu_pct'], s['relay_rss_gb']),
                         (100.0, 1000.0, 95.0, 3.0))
        self.assertAlmostEqual(s['relay_busy_pct'], 61.25, delta=0.06)
        self.assertEqual((s['udp_buf_errors'], s['loadbox_busy_max_pct'], s['loadbox_busiest_cpu_pct']), (5, 50.0, 60.0))
        self.assertEqual(len(ds['loadgen']), 10)
        self.assertEqual(ds['loadgen'][0], {
            'run': 'aio-pgo60-2b-1080', 'box': 'load-a', 't_s': 5, 'subs': 500, 'cores': 1.5, 'mcores_per_sub': 3.0,
            'rss_gb': 2.0, 'mb_per_sub': 4.1, 'nic_rx_gbps': 10.0, 'cores_per_gbps': 0.15, 'busy_pct': 50.0,
            'busiest_cpu_pct': 60.0})
        run = ds['runs'][0]
        # Plateau: table rows within 5% of the peak (2500), 180 s to 300 s.
        self.assertEqual((run['subs_plateau'], run['p90_median_ms'], run['limit']), (2438, 360.0, 'relay'))
        self.assertEqual((run['moqx_tx_gbps'], run['moqx_cores'], run['udp_buf_errors'], run['mbps_per_sub']),
                         (10.0, 2.0, 30, 4.3))
        self.assertEqual((run['tool'], run['moqx_threads'], run['load_boxes'], run['threshold_ms']),
                         ('aiomoqt adaptive_bench', 60, 2, 500))

    def test_moqperf_run(self):
        self.run_dir('mp-pgo60-2b-1080', 'mp', ['moqperf-sub0.log', 'moqperf-sub1.log'], 11)
        ds, skipped = sr.report(self.tmp)
        self.assertEqual(skipped, [])
        self.assertEqual(len(ds['sut']), 2)
        a, b = ds['sut']
        self.assertEqual((a['subs'], a['mp_lat_avg_ms'], a['mp_lat_max_ms'], a['mp_rx_gbps'], a['mp_resets']),
                         (4000, 29.1, 520.0, 7.5, 2230))
        self.assertEqual((b['mp_lat_max_ms'], b['mp_resets'], b['mp_failures']), (540.0, 2350, 0))
        self.assertEqual([(x['box'], x['subs']) for x in ds['loadgen']],
                         [('load-a', 2000), ('load-b', 2000)] * 2)
        run = ds['runs'][0]
        self.assertEqual((run['subs_plateau'], run['mp_lat_max_median_ms'], run['resets_total'], run['limit']),
                         (4000, 540.0, 2350, 'relay'))
        self.assertEqual((run['tool'], run['p90_median_ms'], run['loss_pct_max']),
                         ('moqx perf (moqperf_test_client)', None, None))

    def test_identity_change_drops_relay_rows(self):
        d = self.run_dir('aio-pgo60-2b-1080', 'aio', ['adaptive-sub0.log', 'adaptive-sub1.log'], 31,
                         threads=lambda t: 10 + 100 * t)
        rows = [json.loads(x) for x in (d / 'telemetry-relay.jsonl').read_text().splitlines()]
        for r in rows[16:-1]:
            r['process']['start_ticks'] = 8  # relay restarted
        (d / 'telemetry-relay.jsonl').write_text('\n'.join(map(json.dumps, rows)))
        ds, _ = sr.report(self.tmp)
        self.assertEqual([x['t_s'] for x in ds['sut']], [0, 5, 15, 20, 25])

    def test_point_from_name_without_sweep_point(self):
        d = self.run_dir('aio-reg60-2b-720', 'aio', ['adaptive-sub0.log'], 11)
        (d / 'sweep-point.json').unlink()
        (d / 'run.txt').write_text('== adaptive vs moqx/mvfst draft 18  600s  subs=5000 over 1 host(s)  pub on client:2'
                                   '  shape 720p\nthreads=60 relay_cpus=default\n')
        pt = sr.point(d)
        self.assertEqual((pt['tool'], pt['build'], pt['shape'], pt['threads'], pt['load_boxes']),
                         ('aio', 'reg', '720p', 60, 1))

    def test_cli_writes_datasets(self):
        self.run_dir('mp-pgo60-2b-1080', 'mp', ['moqperf-sub0.log', 'moqperf-sub1.log'], 11)
        out = self.tmp / 'out'
        subprocess.run([sys.executable, str(ROOT / 'bin/sweep-report.py'), str(self.tmp), '-o', str(out)],
                       check=True, capture_output=True)
        for k in ('runs', 'latency', 'sut', 'loadgen'):
            self.assertIsInstance(json.loads((out / f'{k}.json').read_text()), list)


if __name__ == '__main__':
    unittest.main()
