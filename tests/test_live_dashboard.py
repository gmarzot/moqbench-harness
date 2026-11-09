import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('live_dashboard', str(ROOT / 'bin/live-dashboard'))
spec = importlib.util.spec_from_loader('live_dashboard', loader)
live = importlib.util.module_from_spec(spec)
loader.exec_module(live)

BENCH_ROW = '     30s     2.1Gbps    500    │  2.1Gbps  1.9Gbps   500    │  12ms    28ms    5.0ms   │  0.0%   ramp\n'
MOQPERF_ROW = ('I1106 16:15:00.427868 40640 MoQPerfTestClientMain.cpp:155] [AGGREGATE] [1s] Subs: 64 | Obj/s: 982 | '
               'Mbps: 115.34 | Total: 982 objs, 14.42 MB | Latency(run avg): 1.0 ms | Latency(interval min/avg/max): '
               '1/1.0/3 ms | Resets: 2/s, 2 total | Failures: 0/s, 0 total | Done: 0/32\n')

SWEEP_PLAN = """# Written by moqbench from plans/p1.toml and suites/s1.toml.
# plan: Why this plan runs
# suite: What the suite covers
RELAY=cf
STEP_SUBS=10
JOIN=5
THREADS=48
aio src 720p 2000 400 aio-cf48-2b-720
THREADS=3
# note: Fewer workers than vCPUs
aio src 720p 2000 400 aio-cf3-2b-720
"""


def sweep_point(sweep, run, threads, finished=False):
    out = sweep / run
    out.mkdir(parents=True)
    env = dict(THREADS=threads, DRAFT='18', STEP_SUBS='10', LATENCY_MS='500', STOP_AT_CEILING='1')
    (out / 'sweep-point.json').write_text(json.dumps(dict(run=run, relay='cf', tool='aio', build='src', shape='720p',
                                                          max_per_box=2000, duration_s=400, load_boxes=2, env=env)))
    (out / 'run.txt').write_text('== adaptive vs cf-moq-rs/quinn draft 18  400s\n' + ('run_exit_status=0\n' if finished else ''))
    (out / 'versions.txt').write_text('moqdev:   moq-738c82bfbb93 sha 1\ncf:       moq-rs-14f2eb11da41 sha 2\n')
    return out


def sample(t, busy_ticks, tx_bytes, tx_packets, process=None, allowance=0, comm=None):
    cpu = f'cpu  {busy_ticks} 0 0 {1000 - busy_ticks} 0 0 0 5\ncpu0 {busy_ticks} 0 0 {1000 - busy_ticks} 0 0 0 5\n'
    netdev = f'  lo: 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\nens5: 0 0 0 0 0 0 0 0 {tx_bytes} {tx_packets} 0 0 0 0 0 0\n'
    snmp = 'Udp: InDatagrams InErrors RcvbufErrors SndbufErrors OutDatagrams\nUdp: 0 0 0 0 0\n'
    return dict(t_wall=1000 + t, monotonic=float(t), errors=[], cpu=cpu, netdev=netdev, snmp=snmp, process=process,
                threads={'1': dict(ticks=50 * t)}, comm=comm, nic_allowance=dict(pps_allowance_exceeded=allowance))


class LiveDashboard(unittest.TestCase):
    def test_tool_rows(self):
        row = live.tool_row(BENCH_ROW)
        self.assertEqual((row['subs'], row['latency_ms'], row['metric']), (500, 28.0, 'p90'))
        row = live.tool_row(MOQPERF_ROW)
        self.assertEqual((row['subs'], row['latency_ms'], row['resets'], row['metric']), (64, 3.0, 2, 'slowest'))
        self.assertIsNone(live.tool_row('anything else\n'))

    def test_sample_row_rates(self):
        p0, p1 = (dict(pid=1, start_ticks=1, ticks=t, rss_bytes=1, state='R') for t in (0, 200))
        a = sample(0, 100, 0, 0, p0, comm=dict(ticks=0, rss_bytes=2**30, threads=1, count=1))
        b = sample(2, 300, 500_000_000, 200_000, p1, allowance=3, comm=dict(ticks=400, rss_bytes=2**31, threads=1, count=1))
        r = live.sample_row('relay', 'relay', a, b, 100)
        self.assertAlmostEqual(r['tx_gbps'], 2.0)
        self.assertAlmostEqual(r['tx_mpps'], 0.1)
        self.assertEqual(r['allowance'], 3)
        self.assertAlmostEqual(r['cores'], 1.0)
        self.assertAlmostEqual(r['hot_thread'], 50.0)
        self.assertAlmostEqual(r['tool_cores'], 2.0)
        self.assertAlmostEqual(r['tool_rss_gb'], 2.0)

    def test_replay_loads_hosts_and_tools(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            meta = dict(type='metadata', clock_ticks=100, pid=1, comm=[])
            for name in ('relay', 'sub0'):
                lines = [meta, sample(0, 0, 0, 0), sample(1, 100, 10, 1), dict(type='end')]
                (run / f'telemetry-{name}.jsonl').write_text(''.join(json.dumps(x) + '\n' for x in lines))
            (run / 'sub0.log').write_text(BENCH_ROW + BENCH_ROW.replace(' 30s', ' 40s'))
            (run / 'sub0').mkdir()
            (run / 'sub0/loadreport.csv').write_text('t_monotonic,worst_p90_ms\n5.0,28\n')
            store = live.Store(slo_ms=500, load_limit=85)
            live.replay(run, store)
            rows = store.since(0)
            self.assertEqual({r['kind'] for r in rows}, {'run', 'host', 'tool'})
            self.assertEqual([h['name'] for h in store.meta['hosts']], ['relay', 'load-1'])
            tool = [r for r in rows if r['kind'] == 'tool']
            self.assertEqual([r['t'] - tool[0]['t'] for r in tool], [0, 10])

    def test_a_later_run_clears_the_rows_and_an_earlier_one_does_not(self):
        store = live.Store()
        store.new_run('m-200')
        store.add(dict(kind='host'))
        store.new_run('m-100')
        self.assertEqual(len(store.since(0)), 2)
        store.new_run('m-300')
        self.assertEqual([r['kind'] for r in store.since(0)], ['run'])

    def test_point_info_states_what_the_point_tests(self):
        with tempfile.TemporaryDirectory() as d:
            sweep = Path(d) / 'sweep-p1-s1'
            sweep.mkdir()
            (sweep / 'plan').write_text(SWEEP_PLAN)
            info = live.point_info(sweep_point(sweep, 'aio-cf3-2b-720', '3'))
        facts = dict(info['facts'])
        self.assertEqual((info['title'], info['purpose'], info['note'], info['seconds']),
                         ('cf-moq-rs/quinn, 720p', 'Why this plan runs', 'Fewer workers than vCPUs', 400))
        self.assertEqual(facts['Plan'], 'p1, point 2 of 2')
        self.assertEqual(facts['Relay'], 'cf-moq-rs/quinn moq-rs-14f2eb11da41')
        self.assertEqual(facts['Threads'], '3 threads')
        self.assertEqual(facts['Tool'], 'aiomoqt, K=10 viewers per load process')
        self.assertEqual(facts['Video'], '720p, 2.19 Mbps per subscriber')
        self.assertEqual(facts['Draft'], '18')
        self.assertEqual(facts['Ramp'], '4 subscribers/s')  # JOIN from the plan: 10 per 5 s on each of 2 hosts
        self.assertEqual(facts['Bound'], 'worst-sub p90 over 500 ms; the first ceiling ends the run')

    def test_point_info_without_a_plan_header(self):
        with tempfile.TemporaryDirectory() as d:
            sweep = Path(d) / 'sweep-old'
            sweep.mkdir()
            (sweep / 'plan').write_text('# hand-written: no header\nRELAY=cf\naio src 720p 2000 400 aio-cf3-2b-720\n')
            info = live.point_info(sweep_point(sweep, 'aio-cf3-2b-720', '3'))
        self.assertEqual((dict(info['facts'])['Plan'], info['purpose'], info['note']), ('old, point 1 of 1', '', ''))
        self.assertEqual(dict(info['facts'])['Ramp'], 'n/a')

    def test_the_commands_as_run_set_the_ramp(self):
        with tempfile.TemporaryDirectory() as d:
            sweep = Path(d) / 'sweep-p1-s1'
            sweep.mkdir()
            (sweep / 'plan').write_text('aio src 720p 2000 400 other\n')
            out = sweep_point(sweep, 'aio-cf3-2b-720', '3')
            (out / 'run-settings.txt').write_text('publisher_command=x -j 9\nsubscriber_command_per_host=y --step-subs 20 -j 4 -S 1\n')
            facts = dict(live.point_info(out)['facts'])
        self.assertEqual((facts['Tool'], facts['Ramp']), ('aiomoqt, K=20 viewers per load process', '10 subscribers/s'))

    def test_an_aiomoqt_relay_reports_its_package_version(self):
        with tempfile.TemporaryDirectory() as d:
            sweep = Path(d) / 'sweep-p1-s1'
            sweep.mkdir()
            out = sweep_point(sweep, 'aio-aiomoqt1-2b-720', '1')
            pt = json.loads((out / 'sweep-point.json').read_text())
            (out / 'sweep-point.json').write_text(json.dumps(dict(pt, relay='aiomoqt')))
            (out / 'run.txt').write_text('== adaptive vs aiomoqt/picoquic draft 18  600s\n')
            (out / 'versions.txt').write_text('aiomoqt-relay: 0.15.0.dev9+g1049da0 aiopquic 0.8.0\naiomoqt:        aiomoqt: x\n')
            facts = dict(live.point_info(out)['facts'])
        self.assertEqual(facts['Relay'], 'aiomoqt/picoquic 0.15.0.dev9+g1049da0 on aiopquic 0.8.0')
        self.assertEqual(facts['Threads'], '1 picoquic worker + 1 Python event loop')

    def test_an_aiomoqt_relay_states_its_workers_and_cpus(self):
        with tempfile.TemporaryDirectory() as d:
            sweep = Path(d) / 'sweep-p1-s1'
            sweep.mkdir()
            out = sweep_point(sweep, 'aio-aiomoqt1-2b-720', '1')
            pt = json.loads((out / 'sweep-point.json').read_text())
            pt['env'].update(AIOMOQT_WORKERS='3', RELAY_CPUS='4-7')
            (out / 'sweep-point.json').write_text(json.dumps(dict(pt, relay='aiomoqt')))
            (out / 'run.txt').write_text('== adaptive vs aiomoqt/picoquic draft 18  600s\n')
            (out / 'versions.txt').write_text('relay-cpu: 32 vcpu, AMD EPYC 9R14\n')
            info = live.point_info(out)
        facts = dict(info['facts'])
        self.assertEqual(facts['Threads'], '3 picoquic workers + 1 Python event loop')
        self.assertEqual(facts['CPUs'], 'cores 4-7 (4 of 32 vCPU), AMD EPYC 9R14')
        self.assertTrue(info['title'].endswith(', 3 workers'))

    def test_current_point_is_the_newest_unfinished_one(self):
        with tempfile.TemporaryDirectory() as d:
            sweep = Path(d) / 'sweep-p1-s1'
            sweep.mkdir()
            (sweep / 'plan').write_text(SWEEP_PLAN)
            old = sweep_point(sweep, 'aio-cf48-2b-720', '48', finished=True)
            new = sweep_point(sweep, 'aio-cf3-2b-720', '3')
            os.utime(old / 'sweep-point.json', (1000, 1000))
            self.assertEqual(live.current_point(d)['run'], 'aio-cf3-2b-720')
            (new / 'run.txt').write_text('run_exit_status=0\n')
            self.assertIsNone(live.current_point(d))
        self.assertIsNone(live.current_point(d))

    def test_the_plan_heads_the_page_until_a_point_starts(self):
        with tempfile.TemporaryDirectory() as d:
            results, inventory = Path(d) / 'results', Path(d) / 'inventory.json'
            (results / 'moqbench' / 'p1').mkdir(parents=True)
            (results / 'moqbench' / 'p1' / 'plan.toml').write_text(
                'description = "Why"\ndeployment = "aws-c7a-small"\nrelays = ["aiomoqt:src"]\nsuites = ["s1"]\n'
                '[builds]\naiomoqt = "1049da0"\n')
            inventory.write_text(json.dumps(dict(deployment='aws-c7a-small')))
            store = live.Store()
            info = live.header(store, inventory, results)
            facts = dict(info['facts'])
            self.assertEqual((info['title'], info['purpose'], facts['Relays'], facts['Builds']),
                             ('p1', 'Why', 'aiomoqt:src', 'aiomoqt 1049da0'))
            self.assertIn('relay c7a.2xlarge', facts['Hosts'])
            store.set_point(dict(run='aio-x'))
            self.assertIsNone(live.header(store, inventory, results))
            inventory.write_text(json.dumps(dict(deployment='other')))
            self.assertIsNone(live.header(live.Store(), inventory, results))

    def test_a_new_run_keeps_the_point(self):
        store = live.Store()
        store.set_point(dict(run='x'))
        store.new_run('m-100')
        self.assertEqual([(r['kind'], r.get('info')) for r in store.since(0)], [('run', None), ('point', dict(run='x'))])

    def test_remote_commands_only_read(self):
        for command in (live.list_command(), live.follow_command('/tmp/a b.jsonl')):
            self.assertNotRegex(command, r'(^|[^/])\b(rm|mv|cp|kill|sudo|tee)\b')
            self.assertNotRegex(command.replace('2>/dev/null', ''), r'>')
        self.assertIn('nice -n 19 tail', live.follow_command('/tmp/x'))
        self.assertIn("'/tmp/a b.jsonl'", live.follow_command('/tmp/a b.jsonl'))

    def test_page_loads_nothing_from_elsewhere(self):
        page = (ROOT / 'lib/live-dashboard.html').read_text()
        self.assertEqual(re.findall(r'https?://', page), [])
        self.assertNotRegex(page, r'<(link|img|iframe)\b|<script[^>]+src=')


if __name__ == '__main__':
    unittest.main()
