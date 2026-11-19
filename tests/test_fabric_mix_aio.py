"""aiomoqt-relay in each role of the mixed moqx / LAPS / aiomoqt-relay fabrics."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
import config  # noqa: E402
from fabric import topology  # noqa: E402
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


if __name__ == '__main__':
    unittest.main()
