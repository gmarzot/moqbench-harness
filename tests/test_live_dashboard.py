import importlib.machinery
import importlib.util
import json
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
