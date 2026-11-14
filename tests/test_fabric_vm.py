"""VM fabrics: the provider's commands, telemetry parsing and the new verdicts, without hosts."""
import csv
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
import config  # noqa: E402
from fabric import faults, report  # noqa: E402
from fabric.vm import VMFabric, nft_rules, run_args  # noqa: E402

INV = {'deployment': 'fab', 'provider': 'oci', 'user': 'ubuntu', 'ssh_key_file': '/k', 'schema': 3,
       'relay': {'public': '203.0.113.1', 'private': '10.42.1.11', 'name': 'relay'},
       'relays': [{'public': f'203.0.113.{i}', 'private': f'10.42.1.1{i}', 'name': f'relay-0{i}'} for i in (1, 2, 3)],
       'clients': [{'public': '203.0.113.21', 'private': '10.42.1.21'}, {'public': '203.0.113.22', 'private': '10.42.1.22'}],
       'probes': [{'public': '203.0.113.31', 'private': '10.42.1.31'}]}


def deployment(**fabric):
    nodes = [dict(name=n, instance='VM.Standard.E6.Flex', ocpus=4, roles=['relay']) for n in ('r1', 'r2', 'r3')]
    nodes += [dict(name='load-1', instance='VM.Standard.E6.Flex', ocpus=8, roles=['subscriber']),
              dict(name='ref', instance='VM.Standard.E6.Flex', ocpus=4, roles=['probe'])]
    return config.Deployment('fab', dict(provider='oci', region='r', nodes=nodes, fabric={'order': ['r1', 'r2', 'r3'], **fabric}))


class Recorder:
    """Stands in for Host: records scripts and answers like timed()."""
    local = False

    def __init__(self, log, target):
        self.log, self.target = log, target

    def sh(self, script, check=True, timeout=None):
        self.log.append((self.target, script))
        return type('R', (), {'stdout': '{"t0": 1.0, "t1": 1.2, "rc": 0}', 'returncode': 0})()


def fabric():
    fab = VMFabric(deployment(), inventory=INV)
    log = []
    fab.hostfor = lambda t: Recorder(log, t)
    fab.user = '1000:1000'
    return fab, log


class Provider(unittest.TestCase):
    def test_hosts_and_addresses(self):
        fab, _ = fabric()
        self.assertEqual(fab.hosts['r2']['private'], '10.42.1.12')
        self.assertEqual(fab.hosts['ref']['private'], '10.42.1.31')
        self.assertEqual(fab.load_targets(), ['load-1', 'load-2'])
        self.assertEqual(fab.client_url('r3'), 'moqt://10.42.1.13:4433/moq-relay')
        self.assertEqual(fab.link_address('r1', 'r2'), '10.42.1.12')

    def test_inventory_must_match(self):
        with self.assertRaisesRegex(Exception, 'relay nodes'):
            VMFabric(deployment(), inventory=dict(INV, relays=INV['relays'][:2]))

    def test_configs_dial_private_addresses(self):
        fab, _ = fabric()
        cfg = json.loads(fab.adapter.render('r1', fab.ctx())['config.yaml'])
        self.assertEqual(cfg['peering']['moq_peering']['peers'], [{'host': '10.42.1.12', 'port': 33434}])
        self.assertEqual(cfg['admin']['address'], '127.0.0.1')

    def test_relay_runs_with_host_networking(self):
        fab, _ = fabric()
        cmd = fab.start_cmd('r2')
        for part in ('--network host', '--user 1000:1000', 'nofile=1048576:1048576', 'moqx-peering:latest',
                     '-v "$HOME/moqbench-fabric/fab/"nodes/r2/config.yaml:/etc/moqx/config.yaml:ro'):
            self.assertIn(part, cmd)
        self.assertEqual(run_args({'volumes': ['/a:/b'], 'environment': {'X': '1'}}, 'root'), ['-v', '/a:/b', '-e', 'X=1'])

    def test_nft_counts_each_peer_both_ways(self):
        text = nft_rules({'r2': '10.42.1.12', 'r3': '10.42.1.13'})
        self.assertIn('hook output', text)
        self.assertIn('ip daddr 10.42.1.12 counter comment "r2"', text)
        self.assertIn('ip saddr 10.42.1.13 counter comment "r3"', text)
        self.assertTrue(text.startswith('add table inet moqbench_fabric\ndelete table inet moqbench_fabric'))

    def test_impairment_sides(self):
        fab, log = fabric()
        out = faults.apply(fab, dict(verb='link.impair', on='ingest', peer='transit', netem='delay 5ms'))
        self.assertTrue(out['ok'])
        (t1, s1), (t2, s2) = log
        self.assertEqual((t1, t2), ('r1', 'r2'))
        self.assertIn('egress pref 102 protocol ip flower dst_ip 10.42.1.12 action mirred egress redirect dev mbo2', s1)
        self.assertIn('flower dst_ip 10.42.1.11', s2)
        log.clear()
        faults.apply(fab, dict(verb='link.down', on='r1', peer='r3', direction='on', side='receive'))
        (target, script), = log
        self.assertEqual(target, 'r3')
        self.assertIn('ingress pref 201 protocol ip flower src_ip 10.42.1.11', script)
        self.assertIn('netem loss 100%', script)
        with self.assertRaises(faults.FaultError):
            faults.apply(fab, dict(verb='link.impair', on='r1', peer='r2', netem='delay 5ms', side='sideways'))

    def test_node_verbs_use_the_relay_host(self):
        fab, log = fabric()
        faults.apply(fab, dict(verb='node.freeze', on='egress'))
        self.assertEqual(log[0][0], 'r3')
        self.assertIn('sudo docker pause mb-fab-r3-relay', log[0][1])
        with self.assertRaises(faults.FaultError):
            faults.node_cmd('docker', 'node.kill', 'c', signal='KILL; reboot')

    def test_telemetry_folds_control_and_counters_into_load_monitor(self):
        fab, log = fabric()
        fab.telemetry('t', 60, peering_s=1, state_ms=2, state_s=5)
        scripts = {}
        for t, s in log:
            scripts[t] = scripts.get(t, '') + s
        self.assertIn('--nft moqbench_fabric', scripts['r1'])
        self.assertIn('--json-url peering=http://127.0.0.1:8000/peering', scripts['r1'])
        self.assertNotIn('--nft', scripts['load-1'])
        self.assertEqual(sum('fabric-poll.py --url state=' in s for _, s in log), 3)


class Config(unittest.TestCase):
    def test_relay_count_and_shared_shape(self):
        self.assertEqual(deployment().tfvars()['relay_count'], 3)
        nodes = [dict(name='r1', instance='A', roles=['relay']), dict(name='r2', instance='B', roles=['relay']),
                 dict(name='c', instance='A', roles=['subscriber'])]
        with self.assertRaisesRegex(config.ConfigError, 'share one shape'):
            config.Deployment('x', dict(provider='oci', nodes=nodes, fabric={}))

    def test_example_and_plan_refusal(self):
        dep = config.load_deployment('oci-fabric-example')
        self.assertEqual((dep.tfvars()['relay_count'], dep.tfvars()['probe_count']), (3, 1))
        with self.assertRaisesRegex(config.ConfigError, 'moqbench fabric run'):
            config.Plan('p', dict(deployment='oci-fabric-example', suites=['example-video']))


def write(path, rows):
    path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')


class Telemetry(unittest.TestCase):
    def test_load_monitor_reads_nft_counters(self):
        spec = importlib.util.spec_from_file_location('load_monitor', ROOT / 'remote' / 'load-monitor.py')
        mon = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mon)
        doc = {'nftables': [{'metainfo': {}}, {'rule': {'chain': 'out', 'comment': 'r2', 'expr': [
            {'match': {}}, {'counter': {'packets': 3, 'bytes': 300}}]}}]}
        with mock.patch.object(mon.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout=json.dumps(doc))):
            self.assertEqual(mon.nft_counters('moqbench_fabric'), {'out': {'r2': [300, 3]}})

    def test_nft_rates_clock_and_population(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            write(run / 'monitor-r1.jsonl', [{'type': 'metadata'}] + [
                {'t_wall': 100 + k, 'nft': {'out': {'r2': [k * 30000, k], 'r3': [k * 10, k]}}} for k in range(5)])
            meta = {'order': ['r1', 'r2'], 'provider': 'vm'}
            rates = report.link_rates(run, meta)
            self.assertEqual([v for _, v in rates[('r1', 'r2')]], [30000.0] * 4)
            (run / 'clock-ref-start.txt').write_text('System time     : 0.000012000 seconds fast of NTP time\n'
                                                     'RMS offset      : 0.000003000 seconds\n')
            (run / 'clock-r1-start.txt').write_text('System time     : 0.000020000 seconds slow of NTP time\n')
            self.assertEqual(report.clock(run), {'hosts': {'ref': {'start': (12.0, 3.0)}, 'r1': {'start': (-20.0, None)}},
                                                 'budget_us': 40.0})
            with open(run / 'pop-r3-1.csv', 'w', newline='') as f:
                w = csv.writer(f)
                w.writerow(['t', 'namespace', 'track', 'live', 'target', 'rx_mbps', 'p90_ms'])
                for t in range(10):
                    w.writerow([t, 'n', 'probe', 100 if t else 50, 100, 24.0, 40.0])
            (run / 'pop-r3-1.rc').write_text('0\n')
            pop = report.population(run, 'pop-r3-1', {'object_size': 1000, 'rate': 30}, 500)
            self.assertEqual((pop['target'], pop['steady'], pop['good'], pop['ok']), (100, 6, 6, True))

    def test_fetch_and_media_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / 'fetch-r1.log').write_text('1.0 x INFO: MOQT send: Fetch(request_id=4)\n'
                                              '1.1 x INFO: MOQT event: control message parsed: FetchOk(request_id=4)\n'
                                              '1.2 x DEBUG: MOQT stream(9): FetchHeader(request_id=4)\n')
            (run / 'fetch-r3.log').write_text('1.0 x INFO: MOQT send: Fetch(request_id=4)\n'
                                              '1.1 x INFO: MOQT event: control message parsed: RequestError(code=3)\n')
            self.assertEqual(report.fetched(run, 'fetch-r1')['outcome'], 'ok')
            self.assertTrue(report.fetched(run, 'fetch-r1')['stream'])
            self.assertEqual(report.fetched(run, 'fetch-r3')['outcome'], 'error')
            (run / 'media-r2.log').write_text('10.0   relay: x\n10.25   catalog: [\'audio\']\n'
                                              '10.5   [audio] g3.o0 640B key=True ts_skew_ms=1\n')
            m = report.media(run, 'media-r2')
            self.assertEqual((m['catalog_ms'], m['first_frame_ms']['audio']), (250.0, (500.0, '3.0')))
            out = {'subscribers': {}, 'fetch': {'fetch-r1': report.fetched(run, 'fetch-r1'),
                                                'fetch-r3': report.fetched(run, 'fetch-r3')}}
            meta = {'order': ['r1', 'r2', 'r3'], 'options': {}, 'scenario': {'publisher': 'ingest', 'expect_across': 'fail'}}
            self.assertEqual([v['status'] for v in report.verdicts(out, meta)], ['PASS', 'XFAIL'])
            self.assertEqual(report.expect(True, 'fail'), 'XPASS')


if __name__ == '__main__':
    unittest.main()
