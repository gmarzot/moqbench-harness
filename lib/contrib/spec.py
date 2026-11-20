"""Contribution deployments, suites and scenarios: load and validate.

A contribution deployment names a provider and its three namespaces (src, wan, dst); a suite lists
lanes, profiles and the parameters to sweep; a scenario sets the run shape (length, join window,
source mode). Each file is found over the MOQBENCH_CONFIG folders, like every other config.
"""
import ipaddress

import config

PROVIDERS = {'docker', 'aws', 'oci'}
NAMESPACES = ('src', 'wan', 'dst')
DEPLOYMENT_KEYS = {'provider', 'host', 'region', 'zone', 'arch', 'tag', 'contribution', 'nodes'}
CONTRIB_KEYS = {'subnet', 'images', 'stack_dir', 'uid', 'gid'}
NODE_KEYS = {'name', 'roles', 'cpus', 'instance', 'ocpus', 'memory_gb', 'vcpus'}
IMAGES = {'tools': 'moqbench-contrib-tools:dev', 'ffmpeg': 'moqbench-contrib-ffmpeg:n9.0.2',
          'moq2ts': 'moqbench-contrib-moq2ts:dev', 'aiomoqt': 'moqbench-contrib-aiomoqt:dev',
          'moqx': 'ghcr.io/openmoq/moqx:v0.3.7-amd64'}
PLACEMENTS = ('last-mile', 'first-mile')
RELAYS = {'moqx': 'bbr2', 'aiomoqt': 'bbr', 'none': None}
TEST_KEYS = {'type', 'lanes', 'profiles', 'source', 'scenario', 'repeats', 'placement', 'rtt_multiples',
             'rtt_floor_ms', 'budgets_ms', 'playout_ms', 'relay_cc', 'pub_cc', 'seconds', 'note', 'suffix',
             'dual_path', 'ingest', 'pub_timing', 'pub_deadline', 'sub_repair', 'relay', 'capture'}
CAPTURES = ('none', 'headers', 'full')
SUITE_KEYS = {'description', 'requires', 'test', 'lane'}
SCENARIO_KEYS = {'description', 'seconds', 'join_s', 'drain_s', 'source_mode', 'gap_ms', 'break_ms',
                 'calibration_s', 'soak'}
SCENARIO_DEFAULTS = dict(seconds=300, join_s=10, drain_s=5, source_mode='file', gap_ms=50, break_ms=200,
                         calibration_s=60, soak=False)
TEST_DEFAULTS = dict(source='mpeg2-20m', scenario='standard', repeats=1, placement='last-mile',
                     rtt_multiples=[2, 3, 4, 6], rtt_floor_ms=20, budgets_ms=[200, 300], playout_ms=[300, 200],
                     relay='moqx', relay_cc=None, pub_cc=['bbr'], dual_path=False, ingest='srt',
                     pub_timing=['none'], pub_deadline=['none'], sub_repair=['none'], capture='none')


class Deployment:
    def __init__(self, name, data, where):
        config.only_keys(where, data, DEPLOYMENT_KEYS)
        self.name = name
        self.provider = data.get('provider')
        if self.provider not in PROVIDERS:
            raise config.ConfigError(f'{where}: provider must be one of {", ".join(sorted(PROVIDERS))}')
        if 'contribution' not in data:
            raise config.ConfigError(f'{where}: a contribution deployment needs a [contribution] table')
        c = data['contribution']
        config.only_keys(f'{where}: contribution', c, CONTRIB_KEYS)
        self.host = data.get('host')
        if self.provider == 'docker' and not self.host:
            raise config.ConfigError(f'{where}: a docker deployment needs host: "local" or an ssh target')
        net = ipaddress.ip_network(c.get('subnet', '10.79.0.0/16'))
        if net.prefixlen > 16:
            raise config.ConfigError(f'{where}: contribution subnet must be a /16 or larger')
        self.subnet = net
        self.images = dict(IMAGES)
        images = c.get('images', {})
        config.only_keys(f'{where}: contribution.images', images, set(IMAGES))
        self.images.update(images)
        self.stack_dir = c.get('stack_dir', 'moqbench-contrib')
        self.uid, self.gid = c.get('uid'), c.get('gid')
        self.nodes = {}
        for node in data.get('nodes') or []:
            config.only_keys(f'{where}: node {node.get("name", "?")}', node, NODE_KEYS)
            if node.get('name') not in NAMESPACES:
                raise config.ConfigError(f'{where}: contribution nodes are {", ".join(NAMESPACES)}, not {node.get("name")!r}')
            self.nodes[node['name']] = node
        if self.provider != 'docker' and not {'src', 'dst'} <= set(self.nodes):
            raise config.ConfigError(f'{where}: a VM deployment lists src and dst nodes')
        self.tag = data.get('tag', '')
        self.region = data.get('region')

    def cpus(self, ns):
        return (self.nodes.get(ns) or {}).get('cpus')


def load_deployment(name, root=config.ROOT):
    path = config.need('deployments', [f'{name}.toml', f'{name}.local.toml'], root)
    return Deployment(name, config.read_toml(path), config.shown(path, root))


class Test:
    def __init__(self, data, where, lane_opts):
        config.only_keys(where, data, TEST_KEYS)
        if data.get('type') != 'contribution':
            raise config.ConfigError(f'{where}: type must be contribution')
        for key, value in TEST_DEFAULTS.items():
            setattr(self, key, data.get(key, value))
        from contrib import lanes, sources
        self.lanes = data.get('lanes') or []
        if not self.lanes:
            raise config.ConfigError(f'{where}: lanes names at least one of {", ".join(sorted(lanes.LANES))}')
        for lane in self.lanes:
            if lane not in lanes.LANES:
                raise config.ConfigError(f'{where}: lane {lane!r} is not one of {", ".join(sorted(lanes.LANES))}')
        self.profiles = data.get('profiles') or []
        if not self.profiles:
            raise config.ConfigError(f'{where}: profiles names at least one profile')
        if self.source not in sources.TIERS:
            raise config.ConfigError(f'{where}: source is one of {", ".join(sorted(sources.TIERS))}')
        if self.placement not in PLACEMENTS:
            raise config.ConfigError(f'{where}: placement is last-mile or first-mile')
        if self.relay not in RELAYS:
            raise config.ConfigError(f'{where}: relay is {" or ".join(RELAYS)}')
        if self.relay == 'none':
            if self.placement != 'first-mile' or self.relay_cc is not None:
                raise config.ConfigError(f'{where}: relay = "none" runs first-mile, with no relay_cc')
            need_relay = [n for n in self.lanes if lanes.LANES[n].relay and not lanes.LANES[n].direct]
            if need_relay:
                raise config.ConfigError(f'{where}: {", ".join(need_relay)} cannot run without a relay')
            self.relay_cc = [None]
        elif self.relay_cc is None:
            self.relay_cc = [RELAYS[self.relay]]
        for key in ('pub_cc', 'relay_cc'):
            if len(getattr(self, key)) > 1 and not any(key in lanes.LANES[n].cc_on_wan(self.placement)
                                                        for n in self.lanes):
                raise config.ConfigError(f'{where}: {key} sweeps a controller that no lane runs on the impaired '
                                         f'hop in {self.placement}')
        if self.capture not in CAPTURES:
            raise config.ConfigError(f'{where}: capture is one of {", ".join(CAPTURES)}')
        if self.ingest not in ('srt', 'stdin'):
            raise config.ConfigError(f'{where}: ingest is srt or stdin')
        if not set(self.pub_timing) <= {'none', 'mapping'}:
            raise config.ConfigError(f'{where}: pub_timing lists none and/or mapping')
        if not set(self.pub_deadline) <= {'none', 'budget', 'half'}:
            raise config.ConfigError(f'{where}: pub_deadline lists none, budget and/or half')
        if not set(self.sub_repair) <= {'none', 'half'}:
            raise config.ConfigError(f'{where}: sub_repair lists none and/or half')
        config.positive_int(where, 'repeats', self.repeats)
        self.seconds = data.get('seconds')
        self.note = data.get('note', '')
        self.suffix = data.get('suffix', '')
        self.lane_opts = lane_opts

    def budgets(self, rtt_ms):
        """Latency budgets D in ms for a profile: multiples of max(RTT, floor), plus the fixed budgets."""
        base = max(rtt_ms, self.rtt_floor_ms)
        out = {int(round(m * base)) for m in self.rtt_multiples} | {int(b) for b in self.budgets_ms}
        return sorted(out)


class Suite:
    def __init__(self, name, data, where):
        config.only_keys(where, data, SUITE_KEYS)
        self.name = name
        self.description = data.get('description', '')
        lane_opts = data.get('lane', {})
        self.tests = [Test(t, f'{where}: test {i}', lane_opts) for i, t in enumerate(data.get('test') or [], 1)]
        if not self.tests:
            raise config.ConfigError(f'{where}: no tests')


def load_suite(name, root=config.ROOT):
    path = config.need('suites', [f'{name}.toml'], root)
    return Suite(name, config.read_toml(path), config.shown(path, root))


def load_scenario(name, root=config.ROOT):
    path = config.need('scenarios', [f'contrib/{name}.toml'], root)
    where = config.shown(path, root)
    data = config.read_toml(path)
    config.only_keys(where, data, SCENARIO_KEYS)
    out = dict(SCENARIO_DEFAULTS, **data)
    if out['source_mode'] not in ('file', 'live'):
        raise config.ConfigError(f'{where}: source_mode is file or live')
    for key in ('seconds', 'join_s', 'calibration_s'):
        config.positive_int(where, key, out[key])
    out['name'] = name
    return out
