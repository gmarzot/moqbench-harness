"""Mixed fabrics: per-node adapters and images, dialer choice, LAPS rendering and control, peer nodes."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
import config  # noqa: E402
from fabric import report, topology  # noqa: E402
from fabric.docker import DockerFabric, parse_timed  # noqa: E402
from fabric.laps import Laps, admin_image, namespaces_in, peer_rows  # noqa: E402
from fabric.run import Run  # noqa: E402


def nodes(laps=None, peer=None, extra=()):
    out = [dict(name='moqx-a', roles=['relay']), {**dict(name='laps', roles=['relay'], adapter='laps', image='laps:t'),
                                                  **(laps or {})},
           dict(name='moqx-b', roles=['relay']), dict(name='client', roles=['subscriber', 'publisher'])]
    if peer is not None:
        out.append(dict(name='probe', roles=['peer'], **peer))
    return out + list(extra)


def deployment(fabric=None, **kw):
    data = dict(provider='docker', host='local', nodes=kw.pop('node_list', None) or nodes(**kw),
                fabric={'order': ['moqx-a', 'laps', 'moqx-b'], 'topology': 'full', **(fabric or {})})
    return config.Deployment('mix', data)


class Config(unittest.TestCase):
    def test_per_node_adapter_and_image(self):
        f = deployment().fabric
        self.assertEqual([f.node_adapter(n) for n in f.order], ['moqx-peering', 'laps', 'moqx-peering'])
        self.assertEqual([f.node_image(n) for n in f.order], ['moqx-peering:latest', 'laps:t', 'moqx-peering:latest'])
        self.assertEqual(f.draft, 18)
        self.assertEqual(config.load_deployment('docker-fabric-example').fabric.draft, 18)

    def test_validation(self):
        cases = [
            (dict(node_list=nodes()[:1] + [dict(name='laps', roles=['relay'], adapter='laps')] + nodes()[2:]), 'needs an image'),
            (dict(laps={'adapter': 'nats'}), 'adapter must be one of'),
            (dict(extra=[dict(name='load', roles=['subscriber'], image='x')]), 'apply only to relay nodes'),
            (dict(extra=[dict(name='load', roles=['subscriber'], dials=['moqx-a'])]), 'apply only to peer nodes'),
            (dict(fabric={'dial': [['laps', 'laps']]}), 'dial entries'),
            (dict(fabric={'metrics': [['laps', 'moqx-b', 200]]}), 'takes no session metric'),
            (dict(fabric={'metrics': [['moqx-b', 'moqx-a', 200]]}), 'does not dial that link'),
            (dict(fabric={'topology': 'line', 'metrics': [['moqx-a', 'moqx-b', 200]]}), 'not a link of this topology'),
            (dict(peer={'command': 'moq-peer-probe {peers} --as {who}'}), 'command fields'),
            (dict(peer={'dials': ['nowhere']}), 'peer node probe'),
        ]
        for kw, msg in cases:
            with self.subTest(msg=msg), self.assertRaisesRegex(config.ConfigError, msg):
                deployment(**kw)

    def test_dialer_override(self):
        order = ['moqx-a', 'laps', 'moqx-b']
        links = topology.links('full', order)
        self.assertEqual(topology.dial_lists(links, order), {'moqx-a': ['laps', 'moqx-b'], 'laps': ['moqx-b'], 'moqx-b': []})
        self.assertEqual(topology.dial_lists(links, order, [('moqx-b', 'laps')]),
                         {'moqx-a': ['laps', 'moqx-b'], 'laps': [], 'moqx-b': ['laps']})
        self.assertEqual(topology.dialer('moqx-b', 'laps', order, [('moqx-b', 'laps')]), 'moqx-b')


class Rendering(unittest.TestCase):
    def test_laps_command_and_moqx_metric(self):
        fab = DockerFabric(deployment(fabric={'metrics': [['moqx-a', 'moqx-b', 300]]}))
        argv = fab.adapter_for('laps').command('laps', fab.ctx())
        self.assertEqual(argv, ['/usr/local/bin/lapsRelay', '-b', '0.0.0.0', '-p', '33435', '-e', 'laps',
                                '--peer_port', '33434', '--peer', '10.77.3.12:33434',
                                '-c', '/certs/relay.pem', '-k', '/certs/relay.key'])
        peers = json.loads(fab.adapter_for('moqx-a').render('moqx-a', fab.ctx())['config.yaml'])['peering']['moq_peering']['peers']
        self.assertEqual(peers, [{'host': '10.77.1.12', 'port': 33434}, {'host': '10.77.2.12', 'port': 33434, 'metric': 300}])
        self.assertEqual(fab.client_url('laps'), 'moqt://10.77.0.102:33435')
        self.assertEqual(fab.client_url('moqx-b'), 'moqt://10.77.0.103:4433/moq-relay')

    def test_dial_override_moves_the_peer_flag(self):
        fab = DockerFabric(deployment(fabric={'dial': [['moqx-b', 'laps']]}))
        self.assertNotIn('--peer', fab.adapter_for('laps').command('laps', fab.ctx()))
        peers = json.loads(fab.adapter_for('moqx-b').render('moqx-b', fab.ctx())['config.yaml'])['peering']['moq_peering']['peers']
        self.assertEqual(peers, [{'host': '10.77.3.11', 'port': 33434}])

    def test_compose_sidecar_and_peer(self):
        fab = DockerFabric(deployment(laps={'admin_image': 'laps:builder-t'}, peer={'dials': ['ingest', 'laps']}))
        c = fab.compose(1000, 1000)['services']
        self.assertEqual(c['laps-relay']['image'], 'laps:t')
        self.assertEqual(c['laps-relay']['stop_grace_period'], '2s')
        self.assertEqual(c['laps-admin']['network_mode'], 'service:laps')
        self.assertEqual(c['laps-admin']['image'], 'laps:builder-t')
        self.assertNotIn('moqx-a-admin', c)
        self.assertEqual(c['probe']['image'], fab.spec.peer_image)
        self.assertEqual(set(c['probe']['networks']), {'access'})
        self.assertEqual(admin_image('laps:4016861'), 'laps:builder-4016861')

    def test_peer_command_template(self):
        dep = deployment(peer={'dials': ['ingest', 'laps']})
        run = Run(dep, dict(type='fabric', scenario='steady', topology='full', seconds=60), 't', tempfile.mkdtemp())
        self.assertEqual(run.peer_argv('probe', 60), [
            'moq-peer-probe', '--peer', '10.77.0.101:33434', '--peer', '10.77.0.102:33434', '-p', '0',
            '--node-id', 'probe', '-t', '60', '-i', '5', '-o', '/fabric/results/t/peer-probe.json'])
        dep = deployment(peer={'command': 'moq-peer-bench {peers} -t {seconds} --out {output}'})
        run = Run(dep, dict(type='fabric', scenario='steady', topology='full', seconds=30), 't', tempfile.mkdtemp())
        self.assertEqual(run.peer_argv('probe', 30)[:3], ['moq-peer-bench', '--peer', '10.77.0.101:33434'])

    def test_draft_must_suit_every_relay(self):
        with self.assertRaisesRegex(config.ConfigError, 'draft 18 only'):
            Run(deployment(), dict(type='fabric', scenario='steady', topology='full', seconds=10, draft=16), 't',
                tempfile.mkdtemp())


class FakeSidecar:
    """Answers lapsAdmin calls with canned documents, recording each command."""

    def __init__(self, docs):
        self.docs, self.calls = docs, []

    def __call__(self, node, command, timeout=60):
        self.calls.append(command)
        method = next(m for m in ('set-peer-metric', 'remove-peer', 'peers') if f' {m}' in command)
        return {'t0': 1.0, 't1': 1.1, 'rc': 0, 'out': json.dumps(self.docs[method])}


PEERS = {'results': {'peers': {'peers': [
    {'role': 'accepted', 'status': 'connected', 'host': '10.77.1.11', 'port': 54548, 'metric': 0, 'node_metric': 100,
     'contact': 'moqx-a', 'metrics': {'srtt_us': 258, 'publishes': 1, 'subscribe_tracks': 0, 'active_streams': 1}},
    {'role': 'dialed', 'status': 'connected', 'host': '10.77.3.12', 'port': 33434, 'contact': 'moqx-b', 'metrics': {}}]}}}


class LapsControl(unittest.TestCase):
    def test_metric_targets_the_session_port(self):
        fab = DockerFabric(deployment())
        fab.sidecar_exec = FakeSidecar({'peers': PEERS, 'set-peer-metric': {'results': {'ok': True}},
                                        'remove-peer': {'results': {}}})
        r = Laps().peer_metric(fab, 'laps', 'moqx-a', 400)
        self.assertEqual(r['status'], 200)
        self.assertIn("set-peer-metric 10.77.1.11:54548:400", fab.sidecar_exec.calls[-1])
        self.assertIn('--uri moq://127.0.0.1:33435', fab.sidecar_exec.calls[-1])
        Laps().peer_remove(fab, 'laps', 'moqx-b')
        self.assertIn('remove-peer 10.77.3.12:33434', fab.sidecar_exec.calls[-1])

    def test_rows_namespaces_and_timing(self):
        rows = peer_rows(PEERS, {'10.77.1.11': 'moqx-a'})
        self.assertEqual((rows[0]['peer'], rows[0]['origin'], rows[0]['publishes'], rows[1]['peer']),
                         ('moqx-a', 'accepted', 1, 'moqx-b'))
        doc = {'results': {'publishes': {'publishes': [{'track_namespace': 'mesh/x', 'track_name': 't'}]},
                           'publish_namespaces': {'items': [{'namespace': ['a', 'b']}]}}}
        self.assertEqual(namespaces_in(doc), {'mesh/x', 'a/b'})
        self.assertEqual(parse_timed('1.5 2.0 0\n{"x": 1}\n'), {'t0': 1.5, 't1': 2.0, 'rc': 0, 'out': '{"x": 1}'})


class PeerReport(unittest.TestCase):
    def test_learned_hashes_and_leaf(self):
        doc = {'node': {'id': '1.1:1.1', 'contact': 'probe'}, 'counters': {'sessions_up': 2, 'dial_failures': {}},
               'sessions': [{'tx_msgs': {'CONNECT': 1}}, {'tx_msgs': {'CONNECT': 1}}],
               'nodes': [{'contact': 'moqx-a', 'path': []}, {'contact': 'laps', 'path': []},
                         {'contact': 'moqx-b', 'path': [{'node': '2.2:2.2'}]}],
               'announces': [{'hash_ok': True}, {'hash_ok': True}], 'subscribes': [{'hash_ok': False}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'peer-probe.json'
            path.write_text(json.dumps(doc))
            pn = report.peer_node(path, ['moqx-a', 'laps', 'moqx-b'])
        self.assertEqual((pn['learned'], pn['missing']), (['laps', 'moqx-a', 'moqx-b'], []))
        self.assertEqual((pn['announces_hash_ok'], pn['subscribes_hash_ok']), ([2, 2], [0, 1]))
        self.assertTrue(pn['leaf']['held'])
        doc['nodes'][2]['path'] = [{'node': '1.1:1.1'}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'peer-probe.json'
            path.write_text(json.dumps(doc))
            self.assertEqual(report.peer_node(path, [])['leaf']['paths_through_us'], ['moqx-b'])


class Examples(unittest.TestCase):
    def test_mixed_example_and_suite(self):
        dep = config.load_deployment('docker-mixed-fabric-example')
        self.assertEqual(dep.fabric.peers, ['probe'])
        self.assertEqual(dep.fabric.metrics, {('moqx-a', 'moqx-b'): 300})
        suite = config.load_suite('example-mixed-fabric')
        self.assertEqual([t['scenario'] for t in suite.fabric_tests], ['steady', 'propagation', 'metric-flip'])


if __name__ == '__main__':
    unittest.main()
