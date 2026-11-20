"""aiomoqt-relay in each role of the mixed moqx / LAPS / aiomoqt-relay fabrics."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
import config  # noqa: E402
from fabric import report, topology  # noqa: E402
from fabric.docker import DockerFabric, FabricError, runtime_metrics  # noqa: E402

ROTATIONS = {'ingest': ['aio', 'laps', 'moqx'], 'transit': ['moqx', 'aio', 'laps'], 'egress': ['moqx', 'laps', 'aio']}


class Rotations(unittest.TestCase):
    def test_roles(self):
        for role, order in ROTATIONS.items():
            for suffix, shape in (('', 'full'), ('-line', 'line')):
                with self.subTest(role=role, shape=shape):
                    f = config.load_deployment(f'docker-mix-aio-{role}{suffix}-example').fabric
                    self.assertEqual((f.order, f.topology, f.draft), (order, shape, 18))
                    self.assertEqual(topology.resolve(role, f.order), 'aio')
                    self.assertEqual({f.node_adapter(n) for n in f.order}, {'aiomoqt-relay', 'laps', 'moqx-peering'})
                    self.assertEqual(bool(f.metrics), shape == 'full')

    def test_metrics_sit_on_the_direct_link(self):
        for role, order in ROTATIONS.items():
            f = config.load_deployment(f'docker-mix-aio-{role}-example').fabric
            (a, b), m = next(iter(f.metrics.items()))
            self.assertEqual(({a, b}, m), ({order[0], order[-1]}, 300))
            self.assertEqual(bool(f.runtime_metrics()), role == 'ingest')

    def test_node_id_is_the_node_name(self):
        fab = DockerFabric(config.load_deployment('docker-mix-aio-transit-example'))
        argv = fab.adapter_for('aio').command('aio', fab.ctx())
        self.assertEqual(argv[argv.index('--node-id') + 1], 'aio')
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == '--peer'], ['10.77.3.12:33434'])

    def test_runtime_metrics_after_links(self):
        fab = DockerFabric(config.load_deployment('docker-mix-aio-ingest-example'))
        calls = []

        def peer_metric(f, node, peer, metric):
            calls.append((node, peer, metric))
            return {'status': 200, 'body': '{"ok": true}'}
        fab.adapter_for('aio').peer_metric = peer_metric
        runtime_metrics(fab)
        self.assertEqual(calls, [('aio', 'moqx', 300)])
        fab.adapter_for('aio').peer_metric = lambda *a: {'status': 404, 'body': 'no such peer'}
        with self.assertRaisesRegex(FabricError, 'session metric 300'):
            runtime_metrics(fab)
        runtime_metrics(DockerFabric(config.load_deployment('docker-mix-aio-transit-example')))

    def test_metrics_need_a_relay_that_takes_them(self):
        data = config.read_toml(ROOT / 'deployments' / 'docker-mix-aio-ingest-example.toml')
        data['fabric']['metrics'] = [['laps', 'moqx', 300]]
        with self.assertRaisesRegex(config.ConfigError, 'takes no session metric'):
            config.Deployment('x', data)

    def test_line_rejects_triangle_metrics(self):
        from fabric.run import Run
        dep = config.load_deployment('docker-mix-aio-ingest-example')
        with self.assertRaisesRegex(config.ConfigError, 'not a link of this topology'):
            Run(dep, dict(type='fabric', scenario='steady', topology='line', seconds=30), 't', tempfile.mkdtemp())


class Suites(unittest.TestCase):
    def test_every_test_runs_on_every_rotation(self):
        from fabric.run import Run
        for suite, suffix in (('example-mix-aio', ''), ('example-mix-aio-line', '-line')):
            tests = config.load_suite(suite).fabric_tests
            for role in ROTATIONS:
                dep = config.load_deployment(f'docker-mix-aio-{role}{suffix}-example')
                for test in tests:
                    with self.subTest(suite=suite, role=role, scenario=test['scenario']):
                        Run(dep, test, 't', tempfile.mkdtemp())
        names = [t['scenario'] for t in config.load_suite('example-mix-aio').fabric_tests]
        self.assertEqual(names, ['steady', 'propagation', 'subscribe-setup', 'metric-flip', 'late-join', 'fetch-peer',
                                 'datagram-transit-origin', 'datagram-through-transit',
                                 'datagram-through-transit-control', 'relay-restart', 'relay-restart-kill',
                                 'edge-scale', 'namespace-tuples'])
        self.assertEqual([t['scenario'] for t in config.load_suite('example-mix-aio-line').fabric_tests],
                         ['steady', 'peer-churn', 'relay-restart', 'relay-restart-kill'])


class FetchHistory(unittest.TestCase):
    def test_a_subscriber_holds_the_publisher_open(self):
        from fabric import run as runmod
        r = runmod.Run(config.load_deployment('docker-mix-aio-transit-example'),
                       dict(type='fabric', scenario='fetch-peer', topology='full', seconds=30), 't', tempfile.mkdtemp())
        launched = []
        r.launch = lambda name, argv, where=None, limit_s=None, pidfile=None: launched.append((name, argv))
        r.fab.telemetry = lambda *a, **k: None
        r.wait = lambda names, limit: []
        orig = runmod.time.sleep
        runmod.time.sleep = lambda s: None
        try:
            r.fetch()
        finally:
            runmod.time.sleep = orig
        names = [n for n, _ in launched]
        self.assertEqual(names[:2], ['pub', 'hold-moqx'])
        self.assertIn('moqt://10.77.0.101:4433/moq-relay', launched[1][1])
        self.assertEqual(names[2:], ['fetch-moqx', 'fetch-aio', 'fetch-laps'])


class Tuples(unittest.TestCase):
    def test_publishers_and_subscribers(self):
        from fabric.run import Run
        run = Run(config.load_deployment('docker-mix-aio-egress-example'),
                  dict(type='fabric', scenario='namespace-tuples', topology='full', seconds=30), 't', tempfile.mkdtemp())
        spec = run.tuple_spec()
        self.assertEqual(spec, {'a': {'namespace': f'{run.ns}/a/bc', 'object_size': 1000},
                                'b': {'namespace': f'{run.ns}/ab/c', 'object_size': 1300}})
        argv = run.pub_argv(20, ns=spec['b']['namespace'], size=spec['b']['object_size'])
        self.assertEqual((argv[argv.index('-N') + 1], argv[argv.index('-s') + 1]), (spec['b']['namespace'], 1300))
        self.assertIn('moqt://10.77.0.101:4433/moq-relay', argv)
        sub = run.sub_argv('aio', 10, ns=spec['a']['namespace'])
        self.assertEqual((sub[sub.index('-N') + 1], sub[3]), (spec['a']['namespace'], 'moqt://10.77.0.103:4433'))

    def test_foreign_objects_fail(self):
        spec = {'a': {'namespace': 'fab-x/a/bc', 'object_size': 1000}, 'b': {'namespace': 'fab-x/ab/c', 'object_size': 1300}}
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)

            def log(name, sizes):
                (run / f'{name}.log').write_text(''.join(
                    f'1.{i} x TRACE: MOQT stream(3): object 0.{i} alias=0 {size} bytes status=0\n'
                    for i, size in enumerate(sizes)) + '1.9 x TRACE: MOQT stream(3): object 0.9 alias=0 4 bytes status=3\n')
            log('tuple-a-aio', [1013] * 5)
            log('tuple-b-aio', [1013, 1313, 1313])
            log('tuple-b-laps', [])
            got = report.tuples(run, spec)
            self.assertEqual((got['tuple-a-aio']['own'], got['tuple-a-aio']['foreign']), (5, 0))
            self.assertEqual((got['tuple-b-aio']['own'], got['tuple-b-aio']['foreign']), (2, 1))
            v = {r['check']: r['status'] for r in report.verdicts(
                {'subscribers': {}, 'tuples': got}, {'order': ['aio'], 'options': {}, 'scenario': {}})}
            self.assertEqual(v['tuple-a-aio: fab-x/a/bc received only its own track'], 'PASS')
            self.assertEqual(v['tuple-b-aio: fab-x/ab/c received only its own track'], 'FAIL')
            self.assertEqual(v['tuple-b-laps: fab-x/ab/c received only its own track'], 'FAIL')


if __name__ == '__main__':
    unittest.main()
