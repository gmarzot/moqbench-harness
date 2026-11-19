"""aiomoqt-relay fabric nodes, named subscribers, per-subscriber accounting and path checks."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
import config  # noqa: E402
from fabric import faults, report  # noqa: E402
from fabric.adapter import Unsupported  # noqa: E402
from fabric.aiomoqt_relay import AiomoqtRelay, namespaces_in, peer_rows, sample  # noqa: E402
from fabric.docker import DockerFabric  # noqa: E402
from fabric.run import Run, load_scenario  # noqa: E402

ORDER = ['laps', 'via-1', 'moqx', 'via-2']
LINE = [('laps', 'via-1'), ('via-1', 'moqx')]
VIA_SCENARIOS = ['via-sub-churn', 'via-sub-churn-reverse', 'via-local-sub', 'via-edge-sub-kill', 'via-pair-failover',
                 'via-pair-failover-reverse', 'via-pair-failover-kill']


def doc(tx=0, own=True, learned=True, ns='fab-x'):
    return {'node_id': '1:via-1', 'label': 'via-1', 'metric': 100, 'listen': '0.0.0.0:33434',
            'peers': [{'host': '10.77.1.11', 'port': 41000, 'origin': 'accepted', 'status': 'connected',
                       'identified': True, 'contact': 'laps', 'remote_node_id': '2:laps', 'srtt_us': 300, 'metric': 0,
                       'node_metric': 100, 'connect_time_us': 900, 'subscribe_tracks': 1, 'publishes': 0,
                       'active_streams': 1, 'tx_streams': 0, 'rx_streams': 4, 'tx_bytes': 0, 'rx_bytes': tx,
                       'tx_datagrams': 0, 'rx_datagrams': 0},
                      {'host': '10.77.4.12', 'port': 33434, 'origin': 'dialed', 'status': 'connected',
                       'identified': True, 'contact': 'moqx', 'remote_node_id': '3:moqx', 'srtt_us': 250, 'metric': 0,
                       'node_metric': 100, 'connect_time_us': 800, 'subscribe_tracks': 0, 'publishes': 1,
                       'active_streams': 1, 'tx_streams': 4, 'rx_streams': 0, 'tx_bytes': tx, 'rx_bytes': 0,
                       'tx_datagrams': 0, 'rx_datagrams': 0}],
            'nodes': [{'node_id': '2:laps', 'contact': 'laps', 'best_peer': '10.77.1.11:41000', 'path_metric': 100,
                       'hops': 1}],
            'announces': [{'namespace': ns, 'name': 'probe', 'source': '2:laps', 'kind': 'track'}],
            'subscribes': [{'namespace': ns, 'name': 'probe', 'source': '3:moqx', 'fullname_hash': '0x1'}]
            if learned else [],
            'transit': [],
            'tracks': [{'namespace': ns, 'name': 'probe', 'local_viewers': 1 if own else 0,
                        'fabric': 'subscribed' if own else 'paused'}]}


def deployment(name='docker-via-example'):
    return config.load_deployment(name)


class FakeAdmin:
    """Answers control-socket calls with GET /moq-fabric documents, recording each call."""

    def __init__(self, body=None, status=200):
        self.body, self.status, self.calls = body or doc(), status, []

    def __call__(self, node, method, path, body=None, base=None):
        self.calls.append((node, method, path, base))
        out = self.body if method == 'GET' else {'ok': True}
        return {'t0': 1.0, 't1': 1.1, 'status': self.status, 'body': json.dumps(out)}


class Adapter(unittest.TestCase):
    def test_line_command_and_compose(self):
        fab = DockerFabric(deployment(), LINE)
        argv = fab.adapter_for('via-1').command('via-1', fab.ctx())
        self.assertEqual(argv, ['aiomoqt-relay', '--quic', '--bind', '0.0.0.0', '--port', '4433',
                                '--cert', '/certs/relay.pem', '--key', '/certs/relay.key',
                                '--control', '/fabric/run/control.sock', '--pause-after', '5',
                                '--node-id', 'via-1', '--node-metric', '100', '--peer-port', '33434',
                                '--peer', '10.77.4.12:33434'])
        self.assertEqual(fab.client_url('via-1'), 'moqt://10.77.0.102:4433')
        c = fab.compose(1000, 1000)['services']
        self.assertEqual(c['via-1-relay']['image'], fab.spec.peer_image)
        self.assertEqual(c['via-1-relay']['command'], argv)
        self.assertIn('./nodes/via-1/run:/fabric/run', c['via-1-relay']['volumes'])
        self.assertIn('./nodes/via-1/run:/fabric/run', c['via-1']['volumes'])
        self.assertNotIn('./nodes/moqx/run:/fabric/run', c['moqx']['volumes'])

    def test_ring_dials_and_node_metric(self):
        fab = DockerFabric(deployment())
        ad = fab.adapter_for('via-2')
        argv = ad.command('via-2', fab.ctx())
        self.assertEqual(argv[argv.index('--node-metric') + 1], '200')
        self.assertNotIn('--peer', argv)
        moqx = json.loads(fab.adapter_for('moqx').render('moqx', fab.ctx())['config.yaml'])['peering']['moq_fabric']
        self.assertEqual(moqx['node_metric'], 100)
        ctl = DockerFabric(deployment('docker-via-moqx-example'))
        via2 = json.loads(ctl.adapter_for('via-2').render('via-2', ctl.ctx())['config.yaml'])['peering']['moq_fabric']
        self.assertEqual(via2['node_metric'], 200)

    def test_render_makes_the_shared_folder(self):
        fab = DockerFabric(deployment(), LINE)
        with tempfile.TemporaryDirectory() as tmp:
            fab.local_dir = Path(tmp)
            stack = fab.render()
            self.assertTrue((stack / 'nodes/via-1/run').is_dir())
            self.assertFalse((stack / 'nodes/moqx/run').exists())
            self.assertTrue((stack / 'bin/unixhttp.py').is_file())

    def test_peer_image_is_built_for_relays(self):
        calls = []

        class Host:
            local = True

            def sh(self, script, check=True, timeout=None):
                calls.append(script)
                return type('R', (), {'returncode': 1 if 'image inspect' in script else 0, 'stdout': ''})()
        fab = DockerFabric(deployment(), LINE, host=Host())
        fab.ensure_lab_image()
        built = [c for c in calls if 'docker build' in c and 'fabric-peer.Dockerfile' in c]
        self.assertEqual(len(built), 1)
        self.assertIn('aiomoqt@gmarzot-0.15.0', built[0])

    def test_fabric_parsing(self):
        names = {'10.77.1.11': 'laps', '10.77.4.12': 'moqx'}
        rows = peer_rows(doc(500), names)
        self.assertEqual([(r['peer'], r['status'], r['port']) for r in rows],
                         [('laps', 'connected', 41000), ('moqx', 'connected', 33434)])
        self.assertEqual((rows[1]['tx_bytes'], rows[1]['active_streams'], rows[0]['remote_node_id']), (500, 1, '2:laps'))
        self.assertEqual(namespaces_in(doc()), {'fab-x'})
        self.assertEqual(sample(doc(700), 'fab-x', 'probe'),
                         {'tx_bytes': 700, 'subscribed': True, 'own_subscribe': True})
        self.assertEqual(sample(doc(own=False), 'fab-x', 'probe')['own_subscribe'], False)
        self.assertEqual(sample(doc(own=False, learned=False), 'fab-x', 'probe')['subscribed'], False)
        self.assertEqual(sample(doc(), 'fab-y', 'probe')['subscribed'], False)

    def test_peer_verbs(self):
        fab = DockerFabric(deployment(), LINE)
        fab.admin = FakeAdmin()
        ad = fab.adapter_for('via-1')
        self.assertTrue(ad.ready(fab, 'via-1'))
        self.assertEqual(ad.peer_state(fab, 'via-1')[0]['peer'], 'laps')
        ad.peer_add(fab, 'via-1', 'via-2', 300)
        ad.peer_remove(fab, 'via-1', 'laps')
        ad.peer_metric(fab, 'via-1', 'moqx', 400)
        posts = [c[2] for c in fab.admin.calls if c[1] == 'POST']
        self.assertEqual(posts, ['/moq-fabric/peer?op=add&host=10.77.5.12&port=33434&metric=300',
                                 '/moq-fabric/peer?op=remove&host=10.77.1.11&port=41000',
                                 '/moq-fabric/peer?op=metric&host=10.77.4.12&port=33434&metric=400'])
        self.assertTrue(all(c[3] == 'http+unix://%2Ffabric%2Frun%2Fcontrol.sock' for c in fab.admin.calls))
        with self.assertRaises(Unsupported):
            ad.peer_restart(fab, 'via-1', 'laps')
        out = faults.apply(fab, dict(verb='peer.restart', on='via-1', peer='laps'))
        self.assertIsNone(out['ok'])
        self.assertIn('peer.restart', out['skipped'])
        fab.admin = FakeAdmin(status=0)
        self.assertFalse(ad.ready(fab, 'via-1'))
        self.assertIsNone(ad.peer_state(fab, 'via-1'))

    def test_control_is_docker_only(self):
        fab = type('VM', (), {'kind': 'vm'})()
        with self.assertRaisesRegex(Unsupported, 'docker'):
            AiomoqtRelay().ready(fab, 'via-1')


class UnixHttp(unittest.TestCase):
    def test_request_over_a_unix_socket(self):
        import http.server
        import socketserver
        import threading
        import urllib.parse
        sys.path.insert(0, str(ROOT / 'remote' / 'fabric'))
        import unixhttp

        class Handler(http.server.BaseHTTPRequestHandler):
            def address_string(self):
                return 'unix'

            def log_message(self, *args):
                pass

            def reply(self):
                body = json.dumps({'method': self.command, 'path': self.path}).encode()
                self.send_response(404 if self.path == '/nope' else 200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            do_GET = do_POST = reply

        with tempfile.TemporaryDirectory() as tmp:
            path = f'{tmp}/control.sock'
            server = socketserver.UnixStreamServer(path, Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                base = 'http+unix://' + urllib.parse.quote(path, safe='')
                status, body = unixhttp.request(f'{base}/moq-fabric/peer?op=add&host=h&port=1', 'POST')
                self.assertEqual((status, json.loads(body)), (200, {'method': 'POST',
                                                                    'path': '/moq-fabric/peer?op=add&host=h&port=1'}))
                self.assertEqual(unixhttp.request(f'{base}/nope')[0], 404)
            finally:
                server.shutdown()
                server.server_close()
            self.assertEqual(unixhttp.request(f'{base}/moq-fabric', timeout=1)[0], 0)


class Config(unittest.TestCase):
    def data(self, via=None):
        nodes = [dict(name='laps', roles=['relay'], adapter='laps', image='laps:t'),
                 dict(name='via-1', roles=['relay'], adapter='aiomoqt-relay', **(via or {})),
                 dict(name='moqx', roles=['relay']), dict(name='client', roles=['subscriber', 'publisher'])]
        return dict(provider='docker', host='local', nodes=nodes,
                    fabric={'order': ['laps', 'via-1', 'moqx'], 'peer_pip': 'git+https://example.invalid/aiomoqt@x'})

    def test_default_peer_pip(self):
        self.assertEqual(deployment().fabric.peer_pip, 'git+https://github.com/gmarzot/aiomoqt@gmarzot-0.15.0')

    def test_image_and_metric(self):
        f = config.Deployment('v', self.data()).fabric
        self.assertEqual(f.node_image('via-1'), f.peer_image)
        self.assertEqual(f.node_metric('via-1'), 100)
        f = config.Deployment('v', self.data({'image': 'aio:x', 'node_metric': 7})).fabric
        self.assertEqual((f.node_image('via-1'), f.node_metric('via-1')), ('aio:x', 7))

    def test_validation(self):
        for via, msg in (({'node_metric': -1}, 'node_metric must be'), ({'node_metric': True}, 'node_metric must be')):
            with self.subTest(msg=msg), self.assertRaisesRegex(config.ConfigError, msg):
                config.Deployment('v', self.data(via))
        data = self.data()
        data['nodes'][0]['node_metric'] = 5
        with self.assertRaisesRegex(config.ConfigError, 'takes no node_metric'):
            config.Deployment('v', data)
        data = self.data()
        data['nodes'][3]['node_metric'] = 5
        with self.assertRaisesRegex(config.ConfigError, 'apply only to relay nodes'):
            config.Deployment('v', data)

    def test_examples_and_suite(self):
        suite = config.load_suite('example-via')
        self.assertEqual([t['scenario'] for t in suite.fabric_tests], VIA_SCENARIOS)
        for name in ('docker-via-example', 'docker-via-moqx-example', 'docker-via-laps-example'):
            dep = deployment(name)
            self.assertEqual(dep.fabric.order, ORDER)
            for test in suite.fabric_tests:
                with self.subTest(deployment=name, scenario=test['scenario']):
                    run = Run(dep, test, 't', tempfile.mkdtemp())
                    self.assertEqual(run.links, LINE if test['topology'] == 'custom' else
                                     [('laps', 'via-1'), ('laps', 'via-2'), ('via-1', 'moqx'), ('moqx', 'via-2')])
        adapters = {n: deployment().fabric.node_adapter(n) for n in ORDER}
        self.assertEqual(adapters, {'laps': 'laps', 'via-1': 'aiomoqt-relay', 'moqx': 'moqx-peering',
                                    'via-2': 'aiomoqt-relay'})


def scenario_test(name='via-sub-churn', seconds=75, **kw):
    return dict(type='fabric', scenario=name, topology='custom', links=[list(p) for p in LINE], seconds=seconds, **kw)


class NamedSubscribers(unittest.TestCase):
    def test_scenarios_declare_and_drive_their_subscribers(self):
        for name in VIA_SCENARIOS:
            scen = load_scenario(name)
            subs = [s['name'] for s in scen['subscriber']]
            faults.validate(scen['event'], ORDER, subs)
            starts = [e['sub'] for e in scen['event'] if e['verb'] == 'sub.start']
            self.assertEqual(sorted(starts), sorted(subs), name)
        churn = load_scenario('via-sub-churn')
        self.assertTrue(all(s.get('expect', 'complete') == 'complete' for s in churn['subscriber']))
        self.assertEqual([(e['node'], e.get('carries'), e.get('subscribed')) for e in churn['expect_path']],
                         [('via-1', True, None), ('via-1', False, False)])
        kill = load_scenario('via-pair-failover-kill')
        ev = next(e for e in kill['event'] if e['verb'] == 'node.kill')
        self.assertEqual((ev['on'], ev['expect'], ev['max_gap_ms'], ev['subs']), ('via-1', 'bounded-gap', 6000, ['far']))

    def test_validation(self):
        ok = [dict(at_s=1, verb='sub.start', sub='a'), dict(at_s=5, verb='sub.stop', sub='a')]
        faults.validate(ok, ORDER, ['a'])
        bad = [
            ([dict(at_s=1, verb='sub.start', sub='b')], 'declares'),
            ([dict(at_s=1, verb='sub.stop', sub='a')], 'not running'),
            ([dict(at_s=1, verb='sub.start', sub='a'), dict(at_s=2, verb='sub.start', sub='a')], 'more than once'),
            (ok + [dict(at_s=6, verb='sub.kill', sub='a')], 'not running'),
            ([], 'no sub.start'),
            (ok + [dict(at_s=3, verb='node.kill', on='moqx', subs=['z'])], 'no named subscriber'),
            (ok + [dict(at_s=3, verb='node.kill', on='moqx', expect='lossy')], 'expect must be'),
            (ok + [dict(at_s=3, verb='node.kill', on='moqx', expect='bounded-gap')], 'max_gap_ms'),
        ]
        for events, msg in bad:
            with self.subTest(msg=msg), self.assertRaisesRegex(faults.FaultError, msg):
                faults.validate(events, ORDER, ['a'])
        with self.assertRaises(faults.FaultError):
            faults.apply(None, dict(verb='sub.start', sub='a'))

    def test_scenario_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            def load(text):
                path = Path(tmp) / 'scenarios' / 'fabric' / 'x.toml'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
                old = config.need
                config.need = lambda kind, names, root=None: path
                try:
                    return load_scenario('x')
                finally:
                    config.need = old
            base = 'mode = "stream"\n'
            for text, msg in (
                    (base + '[[subscriber]]\nname = "A"\non = "moqx"\n', 'distinct lower-case'),
                    (base + '[[subscriber]]\nname = "a"\non = "moqx"\nexpect = "most"\n', 'complete or none'),
                    (base + '[[expect_path]]\nnode = "moqx"\nfrom_s = 5\nto_s = 2\ncarries = true\n', 'from_s first'),
                    (base + '[[expect_path]]\nnode = "moqx"\nfrom_s = 1\nto_s = 2\n', 'carries, subscribed'),
                    ('mode = "setup"\n[[subscriber]]\nname = "a"\non = "moqx"\n', 'mode = "stream"')):
                with self.subTest(msg=msg), self.assertRaisesRegex(config.ConfigError, msg):
                    load(text)

    def test_run_checks(self):
        dep = deployment()
        with self.assertRaisesRegex(config.ConfigError, 'end after'):
            Run(dep, scenario_test(seconds=60), 't', tempfile.mkdtemp())

    def test_start_stop_kill(self):
        run = Run(deployment(), scenario_test(), 't', tempfile.mkdtemp())
        calls = []

        def exec_(where, command, user=None, detach=False, check=True, timeout=120):
            calls.append((where, command, detach))
            return type('R', (), {'stdout': '{"t0": 2.0, "t1": 2.1, "rc": 0}', 'returncode': 0})()
        run.fab.exec, run.fab.now, run.t_start = exec_, lambda: 1.5, __import__('time').monotonic() - 10
        out = run.sub_action('sub.start', 'far-1')
        self.assertEqual((out['on'], out['t0'], out['rc']), ('moqx', 1.5, 0))
        where, script, detach = calls[0]
        self.assertTrue(detach)
        self.assertIn("echo $$ > /fabric/results/t/sub-far-1.pid; exec", script)
        self.assertIn('moqt://10.77.0.103:4433/moq-relay', script)
        self.assertIn(' -t 62 ', script)
        self.assertIn('/fabric/results/t/sub-far-1.log', script)
        out = run.sub_action('sub.stop', 'far-1')
        self.assertEqual((out['t0'], out['t1'], out['on']), (2.0, 2.1, 'moqx'))
        self.assertIn('kill -INT "$(cat /fabric/results/t/sub-far-1.pid)"', calls[1][1])
        self.assertEqual(calls[1][0], where)
        run.sub_action('sub.start', 'far-2')
        run.sub_action('sub.kill', 'far-2')
        self.assertIn('kill -KILL "$(cat /fabric/results/t/sub-far-2.pid)"', calls[3][1])
        rec = faults.apply(run.fab, dict(at_s=5, verb='sub.start', sub='far-3'), run.sub_action)
        self.assertEqual((rec['on'], rec['ok']), ('moqx', True))


def write_via_run(folder):
    """A run on laps - via-1 - moqx at 10 objects/s: named subscribers, events and state polls."""
    folder.mkdir(parents=True)
    meta = dict(label='t', order=ORDER, links=[list(p) for p in LINE], namespace='fab-x', track='probe',
                scenario={'publisher': 'laps'}, test={'scenario': 'via-x', 'topology': 'custom'},
                options={'rate': 10, 'group_size': 10, 'object_size': 100}, t_zero=100.0,
                adapter='moqx-peering', adapters={'laps': 'laps', 'via-1': 'aiomoqt-relay', 'moqx': 'moqx-peering',
                                                  'via-2': 'aiomoqt-relay'},
                named_subscribers={'far-1': {'on': 'moqx', 'expect': 'complete'},
                                   'far-2': {'on': 'moqx', 'expect': 'complete'},
                                   'local': {'on': 'via-1', 'expect': 'complete'},
                                   'idle': {'on': 'moqx', 'expect': 'none'}},
                expect_path=[dict(node='via-1', from_s=0.5, to_s=5.5, carries=True, subscribed=True),
                             dict(node='via-1', from_s=6.5, to_s=9.5, carries=False, own_subscribe=False),
                             dict(node='moqx', from_s=6.5, to_s=9.5, carries=False),
                             dict(node='moqx', from_s=0.5, to_s=5.5, own_subscribe=True),
                             dict(node='laps', from_s=0.5, to_s=5.5, carries=True)],
                addressing={'access': {'hosts': {}}, 'links': []})
    (folder / 'meta.json').write_text(json.dumps(meta))

    def t(i):
        return 100.0 + i * 0.1
    pub = [f'{t(i):.6f} x TRACE: MOQT send: object {i // 10}.{i % 10} alias=0 100 bytes' for i in range(100)]
    (folder / 'pub.log').write_text('\n'.join(pub) + '\n')

    def sub(name, ids, late=None):
        late = late or {}
        rows = [f'{late.get(i, t(i)) + 0.0005:.6f} x TRACE: MOQT stream(3): object {i // 10}.{i % 10} alias=0 '
                '113 bytes status=0' for i in ids]
        (folder / f'sub-{name}.log').write_text('\n'.join(sorted(rows)) + '\n')
    sub('far-1', range(20, 60))
    sub('far-2', [i for i in range(30, 80) if i != 50])
    sub('local', [i for i in range(100) if i not in (81, 82)], late={61: t(63), 62: t(63)})
    sub('idle', range(10, 41))
    events = [dict(verb='sub.start', sub='local', on='via-1', t0=99.9, t1=99.91, ok=True),
              dict(verb='sub.start', sub='idle', on='moqx', t0=100.9, t1=100.91, ok=True),
              dict(verb='sub.start', sub='far-1', on='moqx', t0=101.95, t1=101.96, ok=True),
              dict(verb='sub.start', sub='far-2', on='moqx', t0=102.95, t1=102.96, ok=True),
              dict(verb='sub.kill', sub='far-2', on='moqx', t0=104.05, t1=104.06, ok=True, expect='no-gap',
                   subs=['local']),
              dict(verb='sub.stop', sub='far-1', on='moqx', t0=105.95, t1=105.96, ok=True),
              dict(verb='node.kill', on='moqx', t0=106.05, t1=106.1, ok=True, expect='no-gap', subs=['local']),
              dict(verb='node.kill', on='via-2', t0=108.05, t1=108.1, ok=True, expect='bounded-gap', max_gap_ms=500,
                   subs=['local'])]
    (folder / 'faults.jsonl').write_text('\n'.join(json.dumps(e) for e in events) + '\n')
    rows, last = [], None
    for k in range(10):
        body = json.dumps(doc(tx=2000 * min(k, 5), own=k <= 5, learned=k <= 5))
        if k == 7:
            rows.append({'t': 100.0 + k, 'name': 'peering', 'status': 0, 'body': 'connection refused'})
            last = 'connection refused'
            continue
        rows.append({'t': 100.0 + k, 'name': 'peering', 'status': 200, **({'body': body} if body != last else {})})
        last = body
    (folder / 'poll-via-1.jsonl').write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
    moqx = [{'t': 100.0 + k, 'name': 'peering', 'status': 200,
             'body': json.dumps({'peers': [{'host': '10.77.4.11', 'active_streams': 2 if k <= 5 else 1,
                                            'subscribe_tracks': 1},
                                           {'host': '10.77.1.11', 'active_streams': 2, 'subscribe_tracks': 0}]})}
            for k in range(10)]
    (folder / 'poll-moqx.jsonl').write_text('\n'.join(json.dumps(r) for r in moqx) + '\n')


class Accounting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        run = Path(self.tmp.name) / 'run'
        write_via_run(run)
        self.s = report.analyze(run)
        self.v = {v['check']: v for v in self.s['verdicts']}

    def tearDown(self):
        self.tmp.cleanup()

    def test_complete_from_first_object(self):
        subs = self.s['subscribers']
        self.assertEqual((subs['sub-far-1']['node'], subs['sub-far-1']['missing']), ('moqx', 0))
        self.assertEqual(self.v['sub-far-1 complete from its first object']['status'], 'PASS')
        self.assertEqual(self.v['sub-far-2 complete from its first object']['status'], 'FAIL')
        self.assertEqual(self.v['sub-far-2 complete from its first object']['missing'], 1)
        self.assertEqual(self.v['sub-local complete from its first object']['status'], 'FAIL')
        self.assertFalse([c for c in self.v if c.startswith('sub-idle')])

    def test_events_only_while_running(self):
        far1 = [(e['verb'], e.get('sub')) for e in self.s['subscribers']['sub-far-1']['events']]
        self.assertEqual(far1, [('sub.start', 'far-2'), ('sub.kill', 'far-2')])
        self.assertEqual(len(self.s['subscribers']['sub-local']['events']), 7)

    def test_gap_expectations_for_named_subscribers(self):
        kill = self.v['sub-local sub.kill far-2 on moqx: no-gap']
        self.assertEqual((kill['status'], kill['gap_bound_ms']), ('PASS', 200))
        node = self.v['sub-local node.kill moqx: no-gap']
        self.assertEqual(node['status'], 'FAIL')
        self.assertAlmostEqual(node['max_gap_ms'], 300, places=1)
        bounded = self.v['sub-local node.kill via-2: bounded-gap']
        self.assertEqual((bounded['status'], bounded['lost'], bounded['gap_bound_ms']), ('PASS', 2, 500))
        self.assertFalse([c for c in self.v if c.startswith(('sub-far-1 ', 'sub-far-2 ')) and 'no-gap' in c])

    def test_expect_path(self):
        rows = {(r['node'], r['state'], r['want'], r['check'].rsplit(', ', 1)[1]): r for r in self.s['path_checks']}
        carry = rows[('via-1', 'carries', True, '0.5-5.5 s')]
        self.assertEqual((carry['status'], carry['source'], carry['measured']), ('PASS', 'tx_bytes', 2000.0))
        self.assertEqual(rows[('via-1', 'subscribed', True, '0.5-5.5 s')]['status'], 'PASS')
        quiet = rows[('via-1', 'carries', False, '6.5-9.5 s')]
        self.assertEqual((quiet['status'], quiet['measured']), ('PASS', 0.0))
        self.assertEqual(rows[('via-1', 'own_subscribe', False, '6.5-9.5 s')]['status'], 'PASS')
        self.assertEqual(rows[('moqx', 'carries', False, '6.5-9.5 s')]['source'], 'streams')
        self.assertEqual(rows[('moqx', 'carries', False, '6.5-9.5 s')]['status'], 'PASS')
        self.assertEqual(rows[('moqx', 'own_subscribe', True, '0.5-5.5 s')]['status'], 'SKIP')
        self.assertEqual(rows[('laps', 'carries', True, '0.5-5.5 s')]['status'], 'SKIP')
        self.assertIn('path: via-1 carries the track, 0.5-5.5 s', report.text(self.s))

    def test_delivery_that_never_resumes_has_no_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / 'run'
            write_via_run(run)
            log = run / 'sub-local.log'
            log.write_text('\n'.join(x for x in log.read_text().splitlines() if float(x.split()[0]) < 106.6) + '\n')
            v = {r['check']: r for r in report.analyze(run)['verdicts']}
            node = v['sub-local node.kill moqx: no-gap']
            self.assertEqual((node['status'], node['max_gap_ms']), ('FAIL', None))

    def test_down_samples_carry_nothing_forward(self):
        samples = report.state_samples(Path(self.tmp.name) / 'run', json.loads(
            (Path(self.tmp.name) / 'run' / 'meta.json').read_text()), 'via-1')
        self.assertEqual([s is None for _, s in samples], [k == 7 for k in range(10)])
        self.assertEqual(samples[9][1]['tx_bytes'], 10000)

    def test_link_rates_stand_in_for_state(self):
        rates = {('laps', 'via-1'): [(100.0 + k, 1000.0) for k in range(10)]}
        self.assertEqual(report.carried([], rates, 'laps', 100.5, 105.5, 500), (True, 'links', ['via-1']))
        self.assertEqual(report.carried([], rates, 'laps', 100.5, 105.5, 5000)[0], False)
        self.assertEqual(report.carried([], {}, 'laps', 100.5, 105.5, 500), (None, None, None))


if __name__ == '__main__':
    unittest.main()
