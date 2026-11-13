"""Fabric rendering, validation, fault commands and run analysis, without Docker or a network."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
import config  # noqa: E402
from fabric import faults, report, topology  # noqa: E402
from fabric.docker import DockerFabric  # noqa: E402
from fabric.moqx_peering import MoqxPeering, namespaces_in, peer_rows  # noqa: E402
from fabric.run import load_scenario  # noqa: E402


def deployment(fabric=None, nodes=None, **over):
    data = dict(provider='docker', host='local', fabric=fabric or {'order': ['r1', 'r2', 'r3']},
                nodes=nodes or [dict(name='r1', roles=['relay'], cpus='2-3'), dict(name='r2', roles=['relay']),
                                dict(name='r3', roles=['relay']), dict(name='client', roles=['subscriber', 'publisher'])])
    data.update(over)
    return config.Deployment('fab', data)


class Topology(unittest.TestCase):
    order = ['a', 'b', 'c', 'd']

    def test_shapes(self):
        self.assertEqual(topology.links('line', self.order), [('a', 'b'), ('b', 'c'), ('c', 'd')])
        self.assertEqual(topology.links('ring', self.order), [('a', 'b'), ('a', 'd'), ('b', 'c'), ('c', 'd')])
        self.assertEqual(topology.links('star', self.order, hub='c'), [('a', 'c'), ('b', 'c'), ('c', 'd')])
        self.assertEqual(len(topology.links('full', self.order)), 6)
        self.assertEqual(topology.links('custom', self.order, custom=[('d', 'a'), ('a', 'd')]), [('a', 'd')])

    def test_rejects(self):
        for args in (('ring', ['a', 'b']), ('line', ['a']), ('mesh', self.order), ('custom', self.order)):
            with self.assertRaises(topology.TopologyError):
                topology.links(*args)
        with self.assertRaises(topology.TopologyError):
            topology.links('custom', self.order, custom=[('a', 'z')])

    def test_earlier_node_dials(self):
        dials = topology.dial_lists(topology.links('ring', self.order), self.order)
        self.assertEqual(dials, {'a': ['b', 'd'], 'b': ['c'], 'c': ['d'], 'd': []})

    def test_hops_cycles_roles(self):
        line = topology.links('line', self.order)
        self.assertEqual(topology.hops(line, 'a'), {'a': 0, 'b': 1, 'c': 2, 'd': 3})
        self.assertFalse(topology.has_cycle(line, self.order))
        self.assertTrue(topology.has_cycle(topology.links('ring', self.order), self.order))
        self.assertEqual([topology.resolve(r, self.order) for r in ('ingest', 'egress', 'transit', 'c')],
                         ['a', 'd', 'b', 'c'])

    def test_addressing_is_distinct_and_stable(self):
        addr = topology.addressing(self.order, ['client'], '10.77.0.0/16')
        ips = list(addr['access']['hosts'].values())
        for pair, link in addr['links'].items():
            ips += [link[n] for n in pair]
        self.assertEqual(len(ips), len(set(ips)))
        self.assertEqual(addr['access']['hosts']['a'], '10.77.0.101')
        self.assertEqual(addr['links'][('a', 'b')], {'name': 'l-a-b', 'subnet': '10.77.1.0/24',
                                                     'a': '10.77.1.11', 'b': '10.77.1.12'})
        self.assertEqual(topology.link_address(addr, 'b', 'a', 'b'), '10.77.1.12')
        self.assertEqual(len(addr['links']), 6)


class Config(unittest.TestCase):
    def test_example_loads(self):
        dep = config.load_deployment('docker-fabric-example')
        self.assertEqual(dep.fabric.order, ['r1', 'r2', 'r3'])
        self.assertEqual(dep.fabric.links, [('r1', 'r2'), ('r2', 'r3')])

    def test_validation(self):
        with self.assertRaisesRegex(config.ConfigError, 'host'):
            deployment(host=None)
        with self.assertRaisesRegex(config.ConfigError, 'order'):
            deployment(fabric={'order': ['r1', 'r2']})
        with self.assertRaisesRegex(config.ConfigError, 'check_interval_ms'):
            deployment(fabric={'check_interval_ms': 1000})
        with self.assertRaisesRegex(config.ConfigError, 'fabric_role'):
            deployment(nodes=[dict(name='r1', roles=['relay'], fabric_role='hub'), dict(name='r2', roles=['relay']),
                              dict(name='c', roles=['subscriber'])], fabric={})
        with self.assertRaisesRegex(config.ConfigError, 'several need a'):
            config.Deployment('x', dict(provider='static', nodes=[
                dict(name='r1', roles=['relay'], address='a'), dict(name='r2', roles=['relay'], address='b'),
                dict(name='c', roles=['subscriber'], address='c')]))

    def test_fabric_suite(self):
        suite = config.load_suite('example-fabric')
        self.assertEqual({t['scenario'] for t in suite.fabric_tests},
                         {'steady', 'propagation', 'subscribe-setup', 'peer-churn'})
        with self.assertRaisesRegex(config.ConfigError, 'scenario'):
            config.Suite('s', dict(test=[dict(type='fabric', scenario='nope', seconds=10)]))
        with self.assertRaisesRegex(config.ConfigError, 'unknown'):
            config.Suite('s', dict(test=[dict(type='fabric', scenario='steady', seconds=10, tool='aiomoqt')]))

    def test_plans_refuse_fabrics(self):
        with self.assertRaisesRegex(config.ConfigError, 'moqbench fabric run'):
            config.Plan('p', dict(deployment='docker-fabric-example', suites=['example-video']))

    def test_scenarios(self):
        for path in (ROOT / 'scenarios' / 'fabric').glob('*.toml'):
            scen = load_scenario(path.stem)
            faults.validate(scen['event'], ['r1', 'r2', 'r3'])


class MoqxConfig(unittest.TestCase):
    def setUp(self):
        self.fab = DockerFabric(deployment())

    def test_node_config(self):
        cfgs = {n: json.loads(MoqxPeering().render(n, self.fab.ctx())['config.yaml']) for n in self.fab.order}
        for n, cfg in cfgs.items():
            self.assertEqual(cfg['listeners']['relay']['moqt_versions'], [16, 18])
            self.assertTrue(cfg['services']['default']['cache']['enabled'])
            self.assertEqual(cfg['admin']['address'], '127.0.0.1')
            peering = cfg['peering']['moq_peering']
            self.assertEqual((peering['node_id'], peering['listen_port']), (n, 33434))
            self.assertNotIn('cluster', cfg)
        self.assertEqual(cfgs['r1']['peering']['moq_peering']['peers'], [{'host': '10.77.1.12', 'port': 33434}])
        self.assertEqual(cfgs['r2']['peering']['moq_peering']['peers'], [{'host': '10.77.3.12', 'port': 33434}])
        self.assertEqual(cfgs['r3']['peering']['moq_peering']['peers'], [])

    def test_compose(self):
        c = self.fab.compose(1001, 1002)
        self.assertEqual(set(c['networks']), {'access', 'l-r1-r2', 'l-r1-r3', 'l-r2-r3'})
        self.assertTrue(all(n['internal'] for n in c['networks'].values()))
        holder, relay, client = c['services']['r1'], c['services']['r1-relay'], c['services']['client']
        self.assertEqual(holder['cap_add'], ['NET_ADMIN'])
        self.assertEqual(set(holder['networks']), {'access', 'l-r1-r2', 'l-r1-r3'})
        self.assertEqual(relay['network_mode'], 'service:r1')
        self.assertEqual((relay['cpuset'], relay['user']), ('2-3', '1001:1002'))
        self.assertIn('./nodes/r1/certs:/etc/moqx/certs', relay['volumes'])
        self.assertEqual(set(client['networks']), {'access'})

    @unittest.skipUnless(shutil.which('openssl'), 'needs openssl')
    def test_render_writes_certs(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.fab.local_dir = Path(tmp)
            stack = self.fab.render()
            self.assertTrue((stack / 'nodes/r2/certs/relay.pem').is_file())
            self.assertEqual((stack / 'nodes/r2/certs/relay.key').stat().st_mode & 0o777, 0o600)
            self.assertTrue((stack / 'bin/fabric-poll.py').is_file())
            json.loads((stack / 'compose.yaml').read_text())


class FakeHost:
    local = True

    def __init__(self):
        self.calls = []

    def sh(self, script, check=True, timeout=None):
        self.calls.append(script)
        return type('R', (), {'stdout': '{"t0": 1.0, "t1": 1.5, "rc": 0}', 'returncode': 0})()


class Faults(unittest.TestCase):
    def test_netem_spec_is_checked(self):
        self.assertIn('netem delay 20ms loss 1% limit', faults.netem_cmd('10.0.0.1', 'delay 20ms loss 1%'))
        with self.assertRaises(faults.FaultError):
            faults.netem_cmd('10.0.0.1', 'delay 1ms; reboot')

    def test_validate(self):
        faults.validate([dict(at_s=1, verb='peer.metric', on='egress', peer='ingest', metric=5)], ['a', 'b'])
        for bad in (dict(at_s=1, verb='peer.metric', on='a', peer='b'), dict(at_s=-1, verb='node.kill', on='a'),
                    dict(at_s=1, verb='link.impair', on='a', peer='b'), dict(at_s=1, verb='reboot', on='a')):
            with self.assertRaises((faults.FaultError, topology.TopologyError)):
                faults.validate([bad], ['a', 'b'])

    def test_impair_both_ends_of_one_link(self):
        fab = DockerFabric(deployment())
        fab.host = FakeHost()
        out = faults.apply(fab, dict(verb='link.impair', on='ingest', peer='egress', netem='delay 5ms'))
        self.assertEqual([e['node'] for e in out['ends']], ['r1', 'r3'])
        self.assertTrue(out['ok'])
        self.assertIn('10.77.2.11', fab.host.calls[0])
        self.assertIn('mb-fab-r3 ', fab.host.calls[1])


def write_run(folder, events=True):
    folder.mkdir(parents=True)
    meta = dict(label='t', order=['r1', 'r2', 'r3'], links=[['r1', 'r2'], ['r2', 'r3']], namespace='fab-x',
                scenario={'publisher': 'ingest'}, test={'scenario': 'steady', 'topology': 'line'},
                options={'rate': 10, 'group_size': 10, 'object_size': 100},
                addressing={'access': {'hosts': {'r1': '10.77.0.101', 'r2': '10.77.0.102', 'r3': '10.77.0.103'}},
                            'links': [{'pair': ['r1', 'r2'], 'r1': '10.77.1.11', 'r2': '10.77.1.12'},
                                      {'pair': ['r2', 'r3'], 'r2': '10.77.3.11', 'r3': '10.77.3.12'}]})
    (folder / 'meta.json').write_text(json.dumps(meta))
    pub, sub = [], ['100.000000   Connecting...', '100.004000   Subscribed to \'fab-x/probe\', receiving...']
    for i in range(30):
        g, o, t = i // 10, i % 10, 100.1 + i * 0.1
        pub.append(f'{t:.6f} 2026-01-01 00:00:00.000 TRACE: MOQT send: object {g}.{o} alias=0 100 bytes')
        if i in (15, 16):
            continue
        sub.append(f'{t + 0.0005:.6f} x TRACE: MOQT stream(3): object {g}.{o} alias=0 113 bytes status=0')
        if i == 20:
            sub.append(f'{t + 0.0007:.6f} x TRACE: MOQT stream(7): object {g}.{o} alias=0 113 bytes status=0')
    sub += ['200.0   Objects:     29', '200.0                p50=0.5  p95=0.6  p99=0.7 ms', '200.0   Jitter:      0.02 ms',
            '200.0   Lost:        2 (6.00%)']
    (folder / 'pub.log').write_text('\n'.join(pub) + '\n')
    (folder / 'sub-r3.log').write_text('\n'.join(sub) + '\n')
    if events:
        (folder / 'faults.jsonl').write_text(json.dumps(dict(verb='peer.remove', on='r1', peer='r3', t0=101.55,
                                                             t1=101.6, ok=True, status=200, expect='hitless')) + '\n')
    for node, sends_on in (('r1', {'eth1': '10.77.1.11'}), ('r2', {'eth1': '10.77.1.12', 'eth2': '10.77.3.11'}),
                           ('r3', {'eth1': '10.77.3.12'})):
        rows = [{'t': 99.0, 'name': 'ifaddr', 'body': sends_on}]
        for k in range(12):
            tx = {'eth1': 0, 'eth2': 0}
            if node in ('r1', 'r2'):
                tx[{'r1': 'eth1', 'r2': 'eth2'}[node]] = k * 1000
            rows.append({'t': 100.0 + k * 0.25, 'name': 'netdev', 'dev': {d: [0, 0, tx[d], 0] for d in sends_on}})
        (folder / f'poll-net-{node}.jsonl').write_text('\n'.join(json.dumps(r) for r in rows) + '\n')


class Report(unittest.TestCase):
    def test_delivery_gap_and_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / 'run'
            write_run(run)
            s = report.analyze(run)
            sub = s['subscribers']['sub-r3']
            self.assertEqual((sub['unique'], sub['missing'], sub['dups'], sub['hops_at_start']), (28, 2, 1, 2))
            self.assertAlmostEqual(sub['lat_p50'], 0.5, places=3)
            self.assertEqual(sub['tool']['lat_p99'], 0.7)
            self.assertEqual(sub['markers']['subscribed_ms'], 4.0)
            ev = sub['events'][0]
            self.assertEqual((ev['lost'], ev['dups']), (2, 1))
            self.assertAlmostEqual(ev['max_gap_ms'], 300, places=1)
            self.assertEqual(s['events'][0]['path_before']['r3'], ['r1>r2>r3'])
            self.assertEqual(sub['events'][0]['lost_ids'], ['1.5', '1.6'])
            self.assertEqual([v['status'] for v in s['verdicts']], ['FAIL'])
            self.assertIn('sub-r3', report.text(s))

    def test_state_and_peering_parsing(self):
        doc = {'services': {'default': {'namespace_tree': {'full_namespace': [], 'children': {
            'a': {'full_namespace': ['a'], 'publisher': 'x', 'children': {}},
            'b': {'full_namespace': ['b'], 'children': {}}}}}}}
        self.assertEqual(namespaces_in(doc), {'a'})
        rows = peer_rows({'peers': [{'host': '10.0.0.2', 'status': 'connected'}]}, {'10.0.0.2': 'r2'})
        self.assertEqual(rows[0]['peer'], 'r2')


if __name__ == '__main__':
    unittest.main()
