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
import shlex
import time
import uuid

import config
from fabric import faults, topology
from fabric.docker import DockerFabric, FabricError

MODES = {'stream', 'setup', 'media', 'fetch'}
SCENARIO_KEYS = {'description', 'mode', 'publisher', 'subscribers', 'pub_ns', 'sub_start_s', 'peering_poll_s',
                 'state_poll_ms', 'settle_s', 'gap_s', 'warm_s', 'cold_order', 'namespace_check', 'netdev_s',
                 'event', 'population', 'audio_frames_per_group', 'fetch_history', 'expect_across'}
POPULATION_KEYS = {'edge', 'subs', 'subs_per_proc'}
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
    for i, pop in enumerate(data.get('population', []), 1):
        config.only_keys(f'{where}: population {i}', pop, POPULATION_KEYS)
        config.positive_int(f'{where}: population {i}', 'subs', pop.get('subs'))
    data.setdefault('event', [])
    data.setdefault('population', [])
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
        faults.validate(self.scen['event'], spec.order)
        shape = test.get('topology', spec.topology)
        custom = [tuple(p) for p in test.get('links') or spec.custom]
        self.links = topology.links(shape, spec.order, spec.hub, custom)
        self.fab = provider(deployment, self.links)
        self.out = Path(out_root) / label
        self.dir = self.fab.results_dir(label)
        self.ns = f'fab-{uuid.uuid4().hex[:6]}'
        self.track = 'probe'
        self.opts = {k: test.get(k, v) for k, v in DEFAULTS.items()}
        self.draft = test.get('draft', spec.draft)
        self.where = {}
        self.events = []

    def launch(self, name, argv, where=None, limit_s=None):
        where = where or self.fab.client()
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

    def pub_argv(self, seconds, pub_ns=False, extra=()):
        argv = [*self.tool('aiomoqt.tools.pub_bench'), self.fab.client_url(self.node('publisher')), '-k',
                '--draft', self.draft, '-N', self.ns, '-T', self.track, '-s', self.opts['object_size'],
                '-r', self.opts['rate'], '-g', self.opts['group_size'], '-t', int(seconds), '--log', 'data=trace']
        return argv + (['--pub-ns'] if pub_ns else []) + list(extra)

    def sub_argv(self, node, seconds):
        return [*self.tool('aiomoqt.tools.sub_bench'), self.fab.client_url(node), '-k', '--draft', self.draft,
                '-N', self.ns, '-T', self.track, '-t', int(seconds), '-i', 1, '--log', 'data=trace']

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
        start = time.monotonic()
        self.launch('pub', self.pub_argv(secs + 5, self.scen.get('pub_ns', False)))
        time.sleep(sub_start)
        subs = subscriber_nodes(self.scen, self.fab.order)
        for n in subs:
            self.launch(f'sub-{n}', self.sub_argv(n, secs - sub_start - 2))
        if self.scen['population']:
            self.populations(secs - sub_start - 4)
        self.events = faults.Timeline(self.scen['event'], self.out / 'faults.jsonl').run(self.fab, start)
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
        time.sleep(settle)
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

    def execute(self, keep=False):
        self.out.mkdir(parents=True, exist_ok=True)
        fab = self.fab
        meta = dict(label=self.label, deployment=self.dep.name, provider=fab.kind, test=self.test,
                    scenario=self.scen, links=self.links, order=fab.order, namespace=self.ns, track=self.track,
                    draft=self.draft, options=self.opts, adapter=fab.adapter.name,
                    addressing={'access': fab.addr['access'],
                                'links': [{'pair': list(k), **v} for k, v in fab.addr['links'].items()]})
        took = fab.up()
        fab.ids()
        fab.reset(self.label)
        meta.update(links_up_s=took, started=fab.now(), relay_image=fab.image_info(fab.spec.image),
                    lab_image=fab.image_info(fab.spec.lab_image) if fab.kind == 'docker' else None)
        r = fab.exec(fab.client(), f'{fab.python} -m aiomoqt.versions 2>&1 | head -5', check=False)
        meta['client_versions'] = r.stdout.strip().splitlines()
        fab.clock(self.label, 'start')
        missing = []
        try:
            missing = {'setup': self.setup, 'media': self.media, 'fetch': self.fetch}.get(self.scen['mode'], self.stream)()
        finally:
            meta['unfinished'] = missing
            meta['ended'] = fab.now()
            fab.clock(self.label, 'end')
            self.collect(meta)
            if not keep:
                fab.down()
        return self.out

    def collect(self, meta):
        fab = self.fab
        for n in fab.order:
            final = {}
            for name, call in (('peering', fab.adapter.peer_state), ('state', fab.adapter.state)):
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
        for rel in [*(['compose.yaml'] if fab.kind == 'docker' else []), *(f'nodes/{n}/config.yaml' for n in fab.order)]:
            dst = self.out / 'stack' / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes((stack / rel).read_bytes())
        (self.out / 'meta.json').write_text(json.dumps(meta, indent=1, default=str) + '\n')
