"""Run one fabric scenario: bring the fabric up in the test's topology, drive clients and events, collect.

Every publisher and reference subscriber runs on the one client target (the
client container, or the reference VM), so their latency needs no clock sync.
Subscriber populations run on the load targets. Client output is prefixed with
the time each line was read (remote/fabric/tsline.py); per-object TRACE lines
give the send and arrival timeline that gap, duplicate and recovery figures
come from.
"""
import json
from pathlib import Path
import re
import shlex
import time
import uuid

import config
from fabric import faults, topology
from fabric.adapter import Unsupported
from fabric.docker import DockerFabric, FabricError
from fabric.moqx_peering import PEERING_PORT

MODES = {'stream', 'setup', 'media', 'fetch', 'tuples'}
# Namespaces that concatenate alike under the run's namespace, and each publisher's extra object size.
TUPLES = (('a', ('a', 'bc'), 0), ('b', ('ab', 'c'), 300))
SCENARIO_KEYS = {'description', 'mode', 'publisher', 'subscribers', 'pub_ns', 'sub_start_s', 'peering_poll_s',
                 'state_poll_ms', 'settle_s', 'gap_s', 'warm_s', 'cold_order', 'namespace_check', 'netdev_s',
                 'event', 'population', 'audio_frames_per_group', 'fetch_history', 'expect_across', 'datagram',
                 'expect_remote', 'subscriber', 'expect_path'}
EXPECT_REMOTE_KEYS = {'publisher_adapter', 'expect', 'defect'}
POPULATION_KEYS = {'edge', 'subs', 'subs_per_proc'}
SUBSCRIBER_KEYS = {'name', 'on', 'expect'}
PATH_KEYS = {'node', 'from_s', 'to_s', 'carries', 'subscribed', 'own_subscribe', 'label'}
PATH_STATES = ('carries', 'subscribed', 'own_subscribe')
DEFAULTS = dict(object_size=1000, rate=30, group_size=30)


def load_scenario(name):
    path = config.need('scenarios', [f'fabric/{name}.toml'])
    where = config.shown(path)
    data = config.read_toml(path)
    config.only_keys(where, data, SCENARIO_KEYS)
    if data.get('mode') not in MODES:
        raise config.ConfigError(f'{where}: mode must be one of {", ".join(sorted(MODES))}')
    if data.get('cold_order', 'nearest') not in ('nearest', 'farthest'):
        raise config.ConfigError(f'{where}: cold_order must be nearest or farthest')
    if data.get('expect_across', 'pass') not in ('pass', 'fail'):
        raise config.ConfigError(f'{where}: expect_across must be pass or fail')
    remote = data.get('expect_remote')
    if remote is not None:
        config.only_keys(f'{where}: expect_remote', remote, EXPECT_REMOTE_KEYS)
        if remote.get('expect') != 'fail' or not remote.get('defect'):
            raise config.ConfigError(f'{where}: expect_remote needs expect = "fail" and a defect description')
    for i, pop in enumerate(data.get('population', []), 1):
        config.only_keys(f'{where}: population {i}', pop, POPULATION_KEYS)
        config.positive_int(f'{where}: population {i}', 'subs', pop.get('subs'))
    names = set()
    for i, sub in enumerate(data.get('subscriber', []), 1):
        at = f'{where}: subscriber {i}'
        config.only_keys(at, sub, SUBSCRIBER_KEYS)
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', str(sub.get('name', ''))) or sub['name'] in names:
            raise config.ConfigError(f'{at}: name must be a distinct lower-case word such as far-1')
        names.add(sub['name'])
        if not isinstance(sub.get('on'), str):
            raise config.ConfigError(f'{at}: on must name a relay or a role')
        if sub.get('expect', 'complete') not in ('complete', 'none'):
            raise config.ConfigError(f'{at}: expect must be complete or none')
    for i, exp in enumerate(data.get('expect_path', []), 1):
        at = f'{where}: expect_path {i}'
        config.only_keys(at, exp, PATH_KEYS)
        if not isinstance(exp.get('node'), str):
            raise config.ConfigError(f'{at}: node must name a relay or a role')
        t0, t1 = exp.get('from_s'), exp.get('to_s')
        if not all(isinstance(t, (int, float)) for t in (t0, t1)) or not 0 <= t0 < t1:
            raise config.ConfigError(f'{at}: from_s and to_s are seconds from the run start, from_s first')
        states = [k for k in PATH_STATES if k in exp]
        if not states or any(not isinstance(exp[k], bool) for k in states):
            raise config.ConfigError(f'{at}: give carries, subscribed or own_subscribe as true or false')
    if (data.get('subscriber') or data.get('expect_path')) and data['mode'] != 'stream':
        raise config.ConfigError(f'{where}: subscriber and expect_path tables need mode = "stream"')
    for key in ('event', 'population', 'subscriber', 'expect_path'):
        data.setdefault(key, [])
    return data


def subscriber_nodes(scen, order):
    want = scen.get('subscribers', ['all'])
    if want == ['all']:
        return list(order)
    return [topology.resolve(n, order) for n in want]


def provider(deployment, link_list=None):
    """The fabric for a deployment: Docker on one host, or the selected testbed's VMs."""
    if deployment.provider == 'docker':
        return DockerFabric(deployment, link_list)
    from fabric.vm import VMFabric
    return VMFabric(deployment, link_list)


class Run:
    def __init__(self, deployment, test, label, out_root):
        self.dep, self.test, self.label = deployment, test, label
        spec = deployment.fabric
        self.scen = load_scenario(test['scenario'])
        at = f'test {test["scenario"]}'
        try:
            self.subs = {s['name']: dict(on=topology.resolve(s['on'], spec.order), expect=s.get('expect', 'complete'))
                         for s in self.scen['subscriber']}
            self.paths = [dict(e, node=topology.resolve(e['node'], spec.order)) for e in self.scen['expect_path']]
            faults.validate(self.scen['event'], spec.order, self.subs)
        except (topology.TopologyError, faults.FaultError) as exc:
            raise config.ConfigError(f'{at}: {exc}') from exc
        clash = set(self.subs) & set(spec.order)
        if clash:
            raise config.ConfigError(f'{at}: subscriber names {", ".join(sorted(clash))} are relay names')
        late = [e['to_s'] for e in self.paths if e['to_s'] > test['seconds']]
        if late:
            raise config.ConfigError(f'{at}: expect_path windows end after the test\'s {test["seconds"]} s')
        shape = test.get('topology', spec.topology)
        custom = [tuple(p) for p in test.get('links') or spec.custom]
        self.links = topology.links(shape, spec.order, spec.hub, custom)
        spec.check_links(self.links, f'test {test["scenario"]}')
        self.fab = provider(deployment, self.links)
        draft = test.get('draft', spec.draft)
        for n in self.fab.order:
            drafts = self.fab.adapter_for(n).drafts
            if drafts and draft not in drafts:
                raise config.ConfigError(f'test {test["scenario"]}: {n} ({self.fab.adapter_for(n).name}) takes '
                                         f'MoQT draft {", ".join(map(str, drafts))} only, not {draft}')
        self.out = Path(out_root) / label
        self.dir = self.fab.results_dir(label)
        self.ns = f'fab-{uuid.uuid4().hex[:6]}'
        self.track = 'probe'
        self.opts = {k: test.get(k, v) for k, v in DEFAULTS.items()}
        self.draft = test.get('draft', spec.draft)
        self.where = {}
        self.events = []
        self.t_start = None
        self.t_zero = None

    def launch(self, name, argv, where=None, limit_s=None, pidfile=None):
        where = where or self.fab.client()
        if pidfile:
            argv = ['sh', '-c', f'echo $$ > {pidfile}; exec "$@"', 'sh', *argv]
        cmd = ' '.join(shlex.quote(str(a)) for a in argv)
        if limit_s:
            cmd = f'timeout -s INT {int(limit_s)} {cmd}'
        script = (f'export PYTHONUNBUFFERED=1; mkdir -p {self.dir}; set -o pipefail; '
                  f'{cmd} 2>&1 | python3 {self.fab.bin_dir}/tsline.py > {self.dir}/{name}.log; '
                  f'echo $? > {self.dir}/{name}.rc')
        self.fab.exec(where, f'bash -c {shlex.quote(script)}', user=self.fab.user, detach=True)
        self.where[name] = where

    def tool(self, module):
        return [self.fab.python, '-m', module]

    def node(self, role):
        return topology.resolve(self.scen.get(role, 'ingest'), self.fab.order)

    def pub_argv(self, seconds, pub_ns=False, extra=(), ns=None, size=None):
        argv = [*self.tool('aiomoqt.tools.pub_bench'), self.fab.client_url(self.node('publisher')), '-k',
                '--draft', self.draft, '-N', ns or self.ns, '-T', self.track, '-s', size or self.opts['object_size'],
                '-r', self.opts['rate'], '-g', self.opts['group_size'], '-t', int(seconds), '--log', 'data=trace']
        return argv + (['--pub-ns'] if pub_ns else []) + (['-D'] if self.scen.get('datagram') else []) + list(extra)

    def sub_argv(self, node, seconds, ns=None):
        return [*self.tool('aiomoqt.tools.sub_bench'), self.fab.client_url(node), '-k', '--draft', self.draft,
                '-N', ns or self.ns, '-T', self.track, '-t', int(seconds), '-i', 1, '--log', 'data=trace']

    def peer_argv(self, name, seconds):
        """A peer node's command from its template: {peers} dials each relay it names."""
        fab = self.fab
        peers = ' '.join(f'--peer {fab.access(n)}:{PEERING_PORT}' for n in fab.spec.peer_dials(name))
        text = fab.spec.peer_template(name).format(peers=peers, node=name, seconds=int(seconds),
                                                   output=f'{self.dir}/peer-{name}.json',
                                                   namespace=self.ns, track=self.track)
        return shlex.split(text)

    def sub_action(self, verb, name):
        """Start a named subscriber until the run's end, or signal it: INT to stop (it prints its
        summary), KILL to kill. Returns {on, t0, t1, rc} on the host clock."""
        node, log = self.subs[name]['on'], f'sub-{name}'
        pidfile = f'{self.dir}/{log}.pid'
        if verb == 'sub.start':
            left = self.test['seconds'] - (time.monotonic() - self.t_start) - 2
            t0 = self.fab.now()
            self.launch(log, self.sub_argv(node, max(1, left)), pidfile=pidfile)
            return dict(on=node, t0=t0, t1=self.fab.now(), rc=0)
        signal = 'INT' if verb == 'sub.stop' else 'KILL'
        r = self.fab.exec(self.where[log], faults.timed(f'kill -{signal} "$(cat {pidfile})"'))
        return dict(on=node, **json.loads(r.stdout))

    def start_peers(self, seconds):
        for name in getattr(self.fab, 'peers', []):
            self.launch(f'peer-{name}', self.peer_argv(name, seconds), where=self.fab.peer_target(name))

    def sample_namespaces(self):
        """Namespaces once from each relay whose state is not polled, as ns-sample-<node>.json."""
        fab = self.fab
        for n in fab.order:
            adapter = fab.adapter_for(n)
            if 'state' in adapter.poll_urls(n):
                continue
            try:
                t = fab.now()
                found = adapter.namespaces(fab, n)
            except (FabricError, ValueError, Unsupported):
                continue
            if found is not None:
                (self.out / f'ns-sample-{n}.json').write_text(json.dumps({'t': t, 'namespaces': sorted(found)}) + '\n')

    def populations(self, seconds):
        """load_sim subscribers on each population's edge, spread over the load targets."""
        targets = self.fab.load_targets()
        k = 0
        for pop in self.scen['population']:
            edges = self.fab.order if pop.get('edge') == 'each' else [topology.resolve(pop['edge'], self.fab.order)]
            for edge in edges:
                where = targets[k % len(targets)]
                k += 1
                name = f'pop-{edge}-{k}'
                argv = [*self.tool('aiomoqt.tools.load_sim'), self.fab.client_url(edge), '-k', '--draft', self.draft,
                        '--role', 'sub', '-N', self.ns, '-T', self.track, '-s', self.opts['object_size'],
                        '-r', self.opts['rate'], '-g', self.opts['group_size'], '--subs', pop['subs'],
                        '--subs-per-proc', pop.get('subs_per_proc', 100), '-t', int(seconds), '-i', 1,
                        '--report', f'{self.dir}/{name}.csv']
                self.launch(name, argv, where=where)

    def wait(self, names, limit_s):
        end = time.time() + limit_s
        while True:
            done = set()
            for target in {self.where[n] for n in names}:
                done |= self.fab.done(self.label, target)
            if set(names) <= done or time.time() > end:
                return sorted(set(names) - done)
            time.sleep(1)

    def stream(self):
        secs = self.test['seconds']
        sub_start = self.scen.get('sub_start_s', 2)
        self.fab.telemetry(self.label, secs + 10, peering_s=self.test.get('peering_poll_s', self.scen.get('peering_poll_s', 5)),
                           netdev_s=self.scen.get('netdev_s', 0.25))
        self.t_zero = self.fab.now()
        start = self.t_start = time.monotonic()
        self.launch('pub', self.pub_argv(secs + 5, self.scen.get('pub_ns', False)))
        self.start_peers(secs)
        time.sleep(sub_start)
        subs = subscriber_nodes(self.scen, self.fab.order)
        for n in subs:
            self.launch(f'sub-{n}', self.sub_argv(n, secs - sub_start - 2))
        if self.scen['population']:
            self.populations(secs - sub_start - 4)
        self.events = faults.Timeline(self.scen['event'], self.out / 'faults.jsonl').run(self.fab, start,
                                                                                         subs=self.sub_action)
        return self.wait(list(self.where), secs + 90)

    def cold_warm(self):
        subs = subscriber_nodes(self.scen, self.fab.order)
        if self.scen.get('cold_order', 'nearest') == 'farthest':
            subs.reverse()
        return subs

    def setup(self):
        settle, gap, warm = self.scen.get('settle_s', 5), self.scen.get('gap_s', 2), self.scen.get('warm_s', 6)
        subs = self.cold_warm()
        total = settle + 2 * gap * len(subs) + warm + 4
        self.fab.telemetry(self.label, total + 5, peering_s=self.test.get('peering_poll_s', self.scen.get('peering_poll_s', 2)),
                           netdev_s=self.scen.get('netdev_s', 0.25),
                           state_ms=self.scen.get('state_poll_ms', 20) if self.scen.get('namespace_check') else None,
                           state_s=settle + 2)
        time.sleep(0.5)
        self.launch('pub', self.pub_argv(total + 5, self.scen.get('pub_ns', True)))
        self.start_peers(total)
        time.sleep(settle)
        if self.scen.get('namespace_check'):
            self.sample_namespaces()
        for i, n in enumerate(subs):
            self.launch(f'cold-{n}', self.sub_argv(n, total - settle - gap * i))
            time.sleep(gap)
        for n in subs:
            self.launch(f'warm-{n}', self.sub_argv(n, warm))
            time.sleep(gap)
        return self.wait(list(self.where), total + 60)

    def media(self):
        """pub_media (catalog and tone) on the publisher's relay; one late sub_media --analyze per relay."""
        settle, gap, warm = self.scen.get('settle_s', 5), self.scen.get('gap_s', 2), self.scen.get('warm_s', 10)
        subs = self.cold_warm()
        total = settle + gap * len(subs) + warm + 4
        self.fab.telemetry(self.label, total + 5, peering_s=self.scen.get('peering_poll_s', 5),
                           netdev_s=self.scen.get('netdev_s', 0.25))
        argv = [*self.tool('aiomoqt.tools.pub_media'), self.fab.client_url(self.node('publisher')), '-k',
                '--draft', self.draft, '-N', self.ns, '-t', int(total + 5)]
        if self.scen.get('audio_frames_per_group'):
            argv += ['--audio-frames-per-group', self.scen['audio_frames_per_group']]
        self.launch('pub', argv)
        time.sleep(settle)
        for n in subs:
            self.launch(f'media-{n}', [*self.tool('aiomoqt.tools.sub_media'), self.fab.client_url(n), '-k',
                                       '--draft', self.draft, '-N', self.ns, '--analyze', '--inspect', 3,
                                       '-i', 1, '-t', int(warm), '--report', f'{self.dir}/media-{n}.csv'])
            time.sleep(gap)
        return self.wait(list(self.where), total + 60)

    def fetch(self):
        """A joining FETCH for groups already published, on the publisher's relay (the control) and on
        every other relay, one at a time."""
        settle, gap, warm = self.scen.get('settle_s', 5), self.scen.get('gap_s', 2), self.scen.get('warm_s', 6)
        subs = self.cold_warm()
        total = settle + (gap + warm) * len(subs) + 4
        self.fab.telemetry(self.label, total + 5, peering_s=self.scen.get('peering_poll_s', 5),
                           netdev_s=self.scen.get('netdev_s', 0.25))
        history = self.scen.get('fetch_history', 1048576)
        self.launch('pub', self.pub_argv(total + 5, extra=['--fetch-history', history]))
        time.sleep(settle)
        for n in subs:
            self.launch(f'fetch-{n}', [*self.tool('aiomoqt.examples.join_example'), self.fab.client_url(n), '-k',
                                       '--draft', self.draft, '-N', self.ns, '-T', self.track, '-d'],
                        limit_s=warm)
            time.sleep(warm + gap)
        return self.wait(list(self.where), 60)

    def tuple_spec(self):
        """{key: {namespace, object_size}} of the tuples mode's two publishers."""
        return {key: dict(namespace='/'.join((self.ns, *parts)), object_size=self.opts['object_size'] + extra)
                for key, parts, extra in TUPLES}

    def tuples(self):
        """Two publishers on the publisher's relay whose namespaces concatenate alike, then a subscriber to
        each on every relay; each must receive only its own track."""
        settle, warm = self.scen.get('settle_s', 5), self.scen.get('warm_s', 10)
        total = settle + warm + 6
        self.fab.telemetry(self.label, total + 5, peering_s=self.scen.get('peering_poll_s', 5),
                           netdev_s=self.scen.get('netdev_s', 0.25))
        spec = self.tuple_spec()
        for key, t in spec.items():
            self.launch(f'pub-{key}', self.pub_argv(total + 5, ns=t['namespace'], size=t['object_size']))
        self.start_peers(total)
        time.sleep(settle)
        for n in subscriber_nodes(self.scen, self.fab.order):
            for key, t in spec.items():
                self.launch(f'tuple-{key}-{n}', self.sub_argv(n, warm, ns=t['namespace']))
        return self.wait(list(self.where), total + 60)

    def execute(self, keep=False):
        self.out.mkdir(parents=True, exist_ok=True)
        fab = self.fab
        meta = dict(label=self.label, deployment=self.dep.name, provider=fab.kind, test=self.test,
                    scenario=self.scen, links=self.links, order=fab.order, namespace=self.ns, track=self.track,
                    draft=self.draft, options=self.opts, adapter=fab.adapter.name,
                    adapters={n: fab.adapter_for(n).name for n in fab.order},
                    dialers={'-'.join(p): topology.dialer(*p, fab.order, fab.spec.dialers) for p in self.links},
                    metrics={f'{a}>{b}': m for (a, b), m in fab.spec.metrics.items()},
                    peers=list(getattr(fab, 'peers', [])),
                    tuples=self.tuple_spec() if self.scen['mode'] == 'tuples' else None,
                    named_subscribers=self.subs,
                    expect_path=self.paths,
                    peer_dials={n: fab.spec.peer_dials(n) for n in getattr(fab, 'peers', [])},
                    namespace_nodes=[n for n in fab.order if 'state' in fab.adapter_for(n).poll_urls(n)],
                    addressing={'access': fab.addr['access'],
                                'links': [{'pair': list(k), **v} for k, v in fab.addr['links'].items()]})
        try:
            took = fab.up()
        except Exception:
            if not keep:
                fab.down()
            raise
        fab.ids()
        fab.reset(self.label)
        meta.update(links_up_s=took, started=fab.now(), relay_image=fab.image_info(fab.spec.node_image(fab.order[0])),
                    images={n: fab.image_info(fab.spec.node_image(n)) for n in fab.order},
                    lab_image=fab.image_info(fab.spec.lab_image) if fab.kind == 'docker' else None)
        r = fab.exec(fab.client(), f'{fab.python} -m aiomoqt.versions 2>&1 | head -5', check=False)
        meta['client_versions'] = r.stdout.strip().splitlines()
        fab.clock(self.label, 'start')
        missing = []
        try:
            missing = {'setup': self.setup, 'media': self.media, 'fetch': self.fetch,
                       'tuples': self.tuples}.get(self.scen['mode'], self.stream)()
        finally:
            try:
                meta['unfinished'] = missing
                meta['t_zero'] = self.t_zero
                meta['ended'] = fab.now()
                fab.clock(self.label, 'end')
                self.collect(meta)
            finally:
                if not keep:
                    fab.down()
        return self.out

    def collect(self, meta):
        fab = self.fab
        for n in fab.order:
            final = {}
            adapter = fab.adapter_for(n)
            for name, call in (('peering', adapter.peer_state), ('state', adapter.state)):
                try:
                    final[name] = call(fab, n)
                except (FabricError, ValueError) as exc:
                    final[name] = {'error': str(exc)}
            (self.out / f'final-{n}.json').write_text(json.dumps(final, indent=1) + '\n')
            (self.out / f'relay-{n}.log').write_text(fab.logs(n))
        meta['leftover_impairments'] = faults.check(fab)
        if meta['leftover_impairments']:
            faults.clear_all(fab)
        try:
            fab.collect(self.label, self.out)
        except FabricError as exc:
            meta['pull_error'] = str(exc)
        stack = fab.local_dir / 'stack'
        rendered = [stack / 'compose.yaml'] if fab.kind == 'docker' else []
        rendered += [f for n in fab.order for f in sorted((stack / 'nodes' / n).glob('*')) if f.is_file()]
        for src in rendered:
            dst = self.out / 'stack' / src.relative_to(stack)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
        (self.out / 'meta.json').write_text(json.dumps(meta, indent=1, default=str) + '\n')
