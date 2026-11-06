import csv
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


report = module('report', 'bin/fixed-load-report.py')
monitor = module('monitor', 'remote/load-monitor.py')
telemetry = module('telemetry', 'bin/load-telemetry-report.py')


def row(t, rx=20, p90=10):
    return dict(t_wall=t, target_subs=10, active_subs=10, target_mbps=20,
                rx_mbps=rx, p90_ms=p90, latency_known=1, loss_pct=0)


class Reporting(unittest.TestCase):
    def verdict(self, mutate=lambda rows: rows, complete=True):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for i in range(3):
                folder = root / f'sub{i}'
                folder.mkdir()
                rows = mutate([row(t) for t in range(0, 61, 5)])
                with (folder / 'loadreport.csv').open('w') as f:
                    w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
                (root / f'sub{i}.log').write_text('duration cap reached (60s)' if complete else 'crashed')
            result = subprocess.run([sys.executable, str(ROOT / 'bin/fixed-load-report.py'), d,
                                     '--hosts', '3', '--subs', '30', '--duration', '60'], capture_output=True)
            return result.returncode, json.loads((root / 'combined.json').read_text())

    def test_good_run(self):
        self.assertEqual(self.verdict()[0], 0)

    def test_silent_receivers_fail_despite_active_and_zero_loss(self):
        self.assertEqual(self.verdict(lambda rows: [dict(r, rx_mbps=0) for r in rows])[0], 1)

    def test_high_latency_fails_despite_duration_cap(self):
        code, result = self.verdict(lambda rows: [dict(r, p90_ms=100000) for r in rows])
        self.assertEqual(code, 1); self.assertEqual(result['healthy_samples'], 0)

    def test_missing_duration_fails(self):
        self.assertEqual(self.verdict(complete=False)[0], 1)

    def test_sparse_samples_fail(self):
        self.assertEqual(self.verdict(lambda rows: rows[::4])[0], 1)

    def test_whole_timestamp_span_is_bounded(self):
        rows = [[row(10)], [row(8)], [row(12)]]
        self.assertEqual(list(report.combine(rows, 2.5)), [])
        self.assertEqual(list(report.combine(rows, 5))[0]['sample_spread_s'], 4)

    def test_no_sample_reuse(self):
        self.assertEqual(len(list(report.combine([[row(0), row(1)], [row(0)]]))), 1)

    def test_cpu_identity_change_is_not_negative_utilization(self):
        a = monitor.snapshot(0)
        b = dict(a, monotonic=a['monotonic']+1)
        a['process'] = dict(pid=1, start_ticks=1, ticks=500, rss_bytes=1)
        b['process'] = dict(pid=1, start_ticks=2, ticks=10, rss_bytes=1)
        self.assertEqual(telemetry.rates(a, b, 100)['process_cores'], '')
        b['process'] = dict(a['process'], ticks=700)
        self.assertEqual(telemetry.rates(a, b, 100)['process_cores'], 2)

    def test_nic_allowance_counters(self):
        text = """NIC statistics:
     tx_timeout: 0
     bw_in_allowance_exceeded: 3
     pps_allowance_exceeded: 7
     conntrack_allowance_available: 900
     queue_0_tx_cnt: 12
"""
        self.assertEqual(monitor.allowance_counters(text), dict(bw_in_allowance_exceeded=3, pps_allowance_exceeded=7))
        a = dict(monitor.snapshot(0), nic_allowance=dict(pps_allowance_exceeded=7))
        b = dict(a, monotonic=a['monotonic'] + 1, nic_allowance=dict(pps_allowance_exceeded=10))
        self.assertEqual(telemetry.rates(a, b, 100)['nic_allowance_exceeded_delta'], 3)
        self.assertEqual(telemetry.allowance_deltas([a, b]), dict(pps_allowance_exceeded=3))
        self.assertEqual(telemetry.allowance_deltas([dict(t=1)]), {})


if __name__ == '__main__':
    unittest.main()
