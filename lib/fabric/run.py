"""Run one fabric scenario: bring the fabric up in the test's topology, drive clients and events, collect.

Every publisher and reference subscriber runs in the one client container, so
latency is measured on one clock. Client output is prefixed with the host time
each line was read (remote/fabric/tsline.py); per-object TRACE lines give the
send and arrival timeline that gap, duplicate and recovery figures come from.
"""
import json
from pathlib import Path
import shlex
import time
import uuid

import config
from fabric import faults, topology
from fabric.docker import DockerFabric, FabricError

MODES = {'stream', 'setup'}
SCENARIO_KEYS = {'description', 'mode', 'publisher', 'subscribers', 'pub_ns', 'sub_start_s', 'peering_poll_s',
                 'state_poll_ms', 'settle_s', 'gap_s', 'warm_s', 'cold_order', 'namespace_check', 'netdev_s',
                 'event'}
DEFAULTS = dict(object_size=1000, rate=30, group_size=30)


def load_scenario(name):
    path = config.need('scenarios', [f'fabric/{name}.toml'])
    data = config.read_toml(path)
    config.only_keys(config.shown(path), data, SCENARIO_KEYS)
    if data.get('mode') not in MODES:
        raise config.ConfigError(f'{config.shown(path)}: mode must be one of {", ".join(sorted(MODES))}')
    if data.get('cold_order', 'nearest') not in ('nearest', 'farthest'):
        raise config.ConfigError(f'{config.shown(path)}: cold_order must be nearest or farthest')
    data.setdefault('event', [])
    return data


def subscriber_nodes(scen, order):
    want = scen.get('subscribers', ['all'])
    if want == ['all']:
        return list(order)
    return [topology.resolve(n, order) for n in want]


class Run:
    def __init__(self, deployment, test, label, out_root):
        self.dep, self.test, self.label = deployment, test, label
        spec = deployment.fabric
        self.scen = load_scenario(test['scenario'])
        faults.validate(self.scen['event'], spec.order)
        shape = test.get('topology', spec.topology)
        custom = [tuple(p) for p in test.get('links') or spec.custom]
        self.links = topology.links(shape, spec.order, spec.hub, custom)
        self.fab = DockerFabric(deployment, self.links)
        self.out = Path(out_root) / label
        self.remote = f'{self.fab.stack}/results/{label}'
        self.inside = f'/fabric/results/{label}'
        self.ns = f'fab-{uuid.uuid4().hex[:6]}'
        self.track = 'probe'
        self.opts = {k: test.get(k, v) for k, v in DEFAULTS.items()}
        self.draft = test.get('draft', spec.draft)
        self.user = None
        self.events = []

    def launch(self, name, argv, where=None):
        cmd = ' '.join(shlex.quote(str(a)) for a in argv)
        script = (f'set -o pipefail; {cmd} 2>&1 | python3 /fabric/bin/tsline.py > {self.inside}/{name}.log; '
                  f'echo $? > {self.inside}/{name}.rc')
        self.fab.exec(where or self.fab.client(), f'bash -c {shlex.quote(script)}', user=self.user, detach=True)

    def pub_argv(self, seconds, pub_ns=False):
        node = topology.resolve(self.scen.get('publisher', 'ingest'), self.fab.order)
        argv = ['python3', '-m', 'aiomoqt.tools.pub_bench', self.fab.client_url(node), '-k', '--draft', self.draft,
                '-N', self.ns, '-T', self.track, '-s', self.opts['object_size'], '-r', self.opts['rate'],
                '-g', self.opts['group_size'], '-t', int(seconds), '--log', 'data=trace']
        return argv + (['--pub-ns'] if pub_ns else [])

    def sub_argv(self, node, seconds):
        return ['python3', '-m', 'aiomoqt.tools.sub_bench', self.fab.client_url(node), '-k', '--draft', self.draft,
                '-N', self.ns, '-T', self.track, '-t', int(seconds), '-i', 1, '--log', 'data=trace']

    def poll(self, kinds, interval, seconds, suffix=''):
        for n in self.fab.order:
            urls = {k: u for k, u in self.fab.adapter.poll_urls(n).items() if k in kinds}
            args = ' '.join(f'--url {k}={u}' for k, u in urls.items())
            if 'netdev' in kinds:
                args += ' --netdev'
            if not args:
                continue
            self.fab.exec(self.fab.holder(n), f'python3 /fabric/bin/fabric-poll.py {args} --interval {interval} '
                          f'--duration {seconds} --out {self.inside}/poll{suffix}-{n}.jsonl', user=self.user, detach=True)

    def wait(self, names, limit_s):
        end = time.time() + limit_s
        while True:
            r = self.fab.host.sh(f'cd {shlex.quote(self.remote)} && ls *.rc 2>/dev/null || true')
            done = {Path(x).stem for x in r.stdout.split()}
            if set(names) <= done or time.time() > end:
                return sorted(set(names) - done)
            time.sleep(1)

    def stream(self):
        secs = self.test['seconds']
        sub_start = self.scen.get('sub_start_s', 2)
        self.poll({'peering'}, self.test.get('peering_poll_s', self.scen.get('peering_poll_s', 5)), secs + 10)
        self.poll({'netdev'}, self.scen.get('netdev_s', 0.25), secs + 10, suffix='-net')
        start = time.monotonic()
        self.launch('pub', self.pub_argv(secs + 5, self.scen.get('pub_ns', False)))
        time.sleep(sub_start)
        subs = subscriber_nodes(self.scen, self.fab.order)
        for n in subs:
            self.launch(f'sub-{n}', self.sub_argv(n, secs - sub_start - 2))
        self.events = faults.Timeline(self.scen['event'], self.out / 'faults.jsonl').run(self.fab, start)
        return self.wait(['pub', *(f'sub-{n}' for n in subs)], secs + 60)

    def setup(self):
        settle = self.scen.get('settle_s', 5)
        gap = self.scen.get('gap_s', 2)
        warm = self.scen.get('warm_s', 6)
        subs = subscriber_nodes(self.scen, self.fab.order)
        if self.scen.get('cold_order', 'nearest') == 'farthest':
            subs.reverse()
        total = settle + 2 * gap * len(subs) + warm + 4
        if self.scen.get('namespace_check'):
            self.poll({'state'}, self.scen.get('state_poll_ms', 20) / 1000, settle + 2, suffix='-state')
        self.poll({'peering'}, self.test.get('peering_poll_s', self.scen.get('peering_poll_s', 2)), total + 5)
        self.poll({'netdev'}, self.scen.get('netdev_s', 0.25), total + 5, suffix='-net')
        time.sleep(0.5)
        self.launch('pub', self.pub_argv(total + 5, self.scen.get('pub_ns', True)))
        time.sleep(settle)
        for n in subs:
            self.launch(f'cold-{n}', self.sub_argv(n, total - settle - gap * subs.index(n)))
            time.sleep(gap)
        for n in subs:
            self.launch(f'warm-{n}', self.sub_argv(n, warm))
            time.sleep(gap)
        names = ['pub', *(f'cold-{n}' for n in subs), *(f'warm-{n}' for n in subs)]
        return self.wait(names, total + 60)

    def execute(self, keep=False):
        self.out.mkdir(parents=True, exist_ok=True)
        meta = dict(label=self.label, deployment=self.dep.name, test=self.test, scenario=self.scen,
                    links=self.links, order=self.fab.order, namespace=self.ns, track=self.track, draft=self.draft,
                    options=self.opts, adapter=self.fab.adapter.name,
                    addressing={'access': self.fab.addr['access'],
                                'links': [{'pair': list(k), **v} for k, v in self.fab.addr['links'].items()]})
        took = self.fab.up()
        uid, gid = self.fab.ids()
        self.user = f'{uid}:{gid}'
        self.fab.host.sh(f'rm -rf {shlex.quote(self.remote)} && mkdir -p {shlex.quote(self.remote)}')
        meta.update(links_up_s=took, started=self.fab.now(),
                    relay_image=self.fab.image_info(self.fab.spec.image),
                    lab_image=self.fab.image_info(self.fab.spec.lab_image),
                    host=self.fab.host.sh('nproc; uname -r; docker version --format "{{.Server.Version}}"').stdout.split())
        r = self.fab.exec(self.fab.client(), 'python3 -m aiomoqt.versions 2>&1 | head -5', check=False)
        meta['client_versions'] = r.stdout.strip().splitlines()
        missing = []
        try:
            missing = self.setup() if self.scen['mode'] == 'setup' else self.stream()
        finally:
            meta['unfinished'] = missing
            meta['ended'] = self.fab.now()
            self.collect(meta)
            if not keep:
                self.fab.down()
        return self.out

    def collect(self, meta):
        for n in self.fab.order:
            final = {}
            for name, call in (('peering', self.fab.adapter.peer_state), ('state', self.fab.adapter.state)):
                try:
                    final[name] = call(self.fab, n)
                except (FabricError, ValueError) as exc:
                    final[name] = {'error': str(exc)}
            (self.out / f'final-{n}.json').write_text(json.dumps(final, indent=1) + '\n')
            (self.out / f'relay-{n}.log').write_text(self.fab.logs(n))
        meta['leftover_impairments'] = faults.check(self.fab)
        if meta['leftover_impairments']:
            faults.clear_all(self.fab)
        try:
            self.fab.host.pull(self.remote, self.out)
        except FabricError as exc:
            meta['pull_error'] = str(exc)
        stack = self.fab.local_dir / 'stack'
        for rel in ['compose.yaml', *(f'nodes/{n}/config.yaml' for n in self.fab.order)]:
            dst = self.out / 'stack' / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes((stack / rel).read_bytes())
        (self.out / 'meta.json').write_text(json.dumps(meta, indent=1, default=str) + '\n')
