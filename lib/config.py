"""Deployments, suites and plans: load, validate, and resolve into sweep points.

A deployment says what hardware exists and what each node does. A suite says
which tests to run, with no hardware in it. A plan pairs them with the relays,
builds and tuning of one series, and is what `moqbench run` executes.
"""
import json
import os
from pathlib import Path
import re
import tomllib

ROOT = Path(__file__).resolve().parents[1]
ROLES = {'relay', 'subscriber', 'publisher', 'probe'}
PROVIDERS = {'aws', 'oci', 'static', 'docker'}
TOOLS = {'aiomoqt': 'aio', 'moqperf': 'mp'}
TEST_TYPES = {'capacity': 'ramp until the latency SLO is crossed',
              'ceiling': 'ramp past the SLO to the egress plateau',
              'fabric': 'a scenario on a relay fabric (moqbench fabric run)'}
BUILDS = {'pgo', 'reg'}
# Relays built from source on the relay host, and the plan's builds key for each one's git ref.
SOURCE_RELAYS = {'moqdev': 'moqdev', 'cf': 'moq_rs', 'aiomoqt': 'aiomoqt'}
# Source relays that provisioning installs with the load tools, so no relay-build.sh step.
PROVISIONED_RELAYS = {'aiomoqt'}
# moq-rs closes the moqperf publisher's session at moxygen's 30 s keepalive.
UNSUPPORTED = {('moqperf', 'cf')}
GENERATED = '# Written by moqbench from deployments/'

# Settings a test can set, the environment variable each becomes, and its default.
SETTINGS = {
    'draft': ('DRAFT', 18), 'step_subs': ('STEP_SUBS', 250), 'join_s': ('JOIN', 10), 'stagger_s': ('STAGGER', 0.02),
    'shortfall': ('SHORTFALL', 0.7), 'delivery_timeout_ms': ('DELIVERY_TIMEOUT', 500), 'mp_threads': ('MP_THREADS', 32),
    'mp_ramp': ('MP_RAMP', 64), 'moqx_send': ('MOQX_SEND', 16), 'moqx_recv': ('MOQX_RECV', 256), 'moqx_gro': ('MOQX_GRO', ''),
    'aiomoqt_env': ('AIOMOQT_ENV', ''),
    'aiomoqt_extra': ('AIOMOQT_EXTRA', ''), 'stop_at_ceiling': ('STOP_AT_CEILING', 0),
    'relay_cpus': ('RELAY_CPUS', ''), 'aiomoqt_workers': ('AIOMOQT_WORKERS', ''),
    'moqx_stack': ('MOQX_STACK', 'mvfst'), 'hold_s': ('HOLD', 0), 'confirm_s': ('CONFIRM', 30),
    'join_search': ('JOIN_SEARCH', ''), 'join_max': ('JOIN_MAX', ''), 'searcher_subs': ('SEARCHER_SUBS', ''),
    'join_bound_ms': ('JOIN_BOUND', 1000), 'trial_gap_s': ('TRIAL_GAP', 5), 'base_s': ('BASE_S', 45),
}


class ConfigError(Exception):
    """A file that cannot be used, and why."""


def read_toml(path):
    try:
        return tomllib.loads(Path(path).read_text())
    except OSError as exc:
        raise ConfigError(f'{path}: {exc.strerror}') from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f'{path}: {exc}') from exc


def config_dirs(root=ROOT):
    """Folders holding deployments/, suites/ and plans/: each MOQBENCH_CONFIG folder in order, then root."""
    extra = [Path(p).expanduser() for p in os.environ.get('MOQBENCH_CONFIG', '').split(os.pathsep) if p]
    return [*extra, Path(root)]


def find(kind, names, root=ROOT):
    """The first <folder>/<kind>/<name> over config_dirs(root), trying names in order in each folder; None if absent."""
    for folder in config_dirs(root):
        for name in names:
            if (folder / kind / name).is_file():
                return folder / kind / name
    return None


def need(kind, names, root=ROOT):
    path = find(kind, names, root)
    if path is None:
        raise ConfigError(f'no {kind}/{names[0]} in {", ".join(map(str, config_dirs(root)))}')
    return path


def shown(path, root=ROOT):
    """A config file's path, relative to root when it is inside it."""
    try:
        return str(Path(path).relative_to(root))
    except ValueError:
        return str(path)


def deployment_names(root=ROOT):
    """Every deployment over config_dirs(root), leaving out the account files."""
    names = set()
    for folder in config_dirs(root):
        for path in (folder / 'deployments').glob('*.toml'):
            name = path.stem.removesuffix('.local')
            if not (path.name.endswith('.local.toml') and name in PROVIDERS):
                names.add(name)
    return sorted(names)


def only_keys(where, data, allowed):
    unknown = set(data) - set(allowed)
    if unknown:
        raise ConfigError(f'{where}: unknown {", ".join(sorted(unknown))} (expected: {", ".join(sorted(allowed))})')


def positive_int(where, key, value):
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f'{where}: {key} must be a positive integer, not {value!r}')


class Fabric:
    """A deployment's [fabric] table: which relays peer, how, and with what images."""
    KEYS = {'adapter', 'image', 'topology', 'order', 'hub', 'links', 'wire', 'subnet', 'threads',
            'check_interval_ms', 'draft', 'aiomoqt', 'aiopquic', 'lab_image', 'stack_dir', 'client_python'}
    ADAPTERS = {'moqx-peering'}

    def __init__(self, data, relays, where):
        from fabric import topology
        only_keys(f'{where}: fabric', data, self.KEYS)
        self.adapter = data.get('adapter', 'moqx-peering')
        if self.adapter not in self.ADAPTERS:
            raise ConfigError(f'{where}: fabric adapter must be one of {", ".join(sorted(self.ADAPTERS))}')
        self.image = data.get('image', 'moqx-peering:latest')
        self.order = list(data.get('order') or relays)
        if sorted(self.order) != sorted(relays):
            raise ConfigError(f'{where}: fabric order must name every relay node once: {", ".join(relays)}')
        self.topology = data.get('topology', 'line')
        self.hub = data.get('hub')
        self.custom = [tuple(p) for p in data.get('links') or []]
        self.wire = data.get('wire', 'full')
        if self.wire not in ('full', 'topology'):
            raise ConfigError(f'{where}: fabric wire must be full (a network for every relay pair) or topology')
        self.subnet = data.get('subnet', '10.77.0.0/16')
        self.threads = data.get('threads', 2)
        self.check_interval_ms = data.get('check_interval_ms', 5000)
        self.draft = data.get('draft', 16)
        self.aiomoqt = data.get('aiomoqt', '0.15.0rc1')
        self.aiopquic = data.get('aiopquic', '0.8.0rc1')
        self.lab_image = data.get('lab_image') or f'moqbench-lab:{self.aiomoqt}-{self.aiopquic}'
        self.stack_dir = data.get('stack_dir', 'moqbench-fabric')
        self.client_python = data.get('client_python', 'Projects/aiomoqt/.venv/bin/python')
        for key in ('threads', 'check_interval_ms', 'draft'):
            positive_int(f'{where}: fabric', key, getattr(self, key))
        if self.check_interval_ms < 2000:
            raise ConfigError(f'{where}: fabric check_interval_ms must be at least 2000')
        try:
            self.links = topology.links(self.topology, self.order, self.hub, self.custom)
            topology.addressing(self.order, [], self.subnet, self.wired())
        except (topology.TopologyError, ValueError) as exc:
            raise ConfigError(f'{where}: fabric: {exc}') from exc

    def wired(self, link_list=None):
        """Relay pairs that get a link network: every pair, or only the topology's links."""
        from fabric import topology
        if self.wire == 'full':
            return topology.links('full', self.order)
        return link_list or self.links


class Deployment:
    NODE_KEYS = {'name', 'instance', 'roles', 'vcpus', 'ocpus', 'memory_gb', 'address', 'cpus', 'fabric_role'}
    KEYS = {'provider', 'region', 'zone', 'arch', 'tag', 'network_type', 'nodes', 'host', 'fabric'}

    def __init__(self, name, data, account=None, where=None):
        self.name, self.account = name, account or {}
        where = where or f'deployments/{name}.toml'
        only_keys(where, data, self.KEYS)
        self.provider = data.get('provider')
        if self.provider not in PROVIDERS:
            raise ConfigError(f'{where}: provider must be one of {", ".join(sorted(PROVIDERS))}')
        self.region, self.zone, self.arch = data.get('region'), data.get('zone'), data.get('arch')
        self.tag = data.get('tag', '')
        self.network_type = data.get('network_type')
        if self.network_type and self.provider != 'oci':
            raise ConfigError(f'{where}: network_type applies only to oci')
        self.host = data.get('host')
        if self.provider == 'docker' and not self.host:
            raise ConfigError(f'{where}: a docker deployment needs host: "local" or the ssh target of a Docker host')
        if self.provider == 'docker' and 'fabric' not in data:
            raise ConfigError(f'{where}: a docker deployment needs a [fabric] table')
        self.nodes = data.get('nodes') or []
        if not self.nodes:
            raise ConfigError(f'{where}: no nodes')
        seen = set()
        for node in self.nodes:
            only_keys(f'{where}: node {node.get("name", "?")}', node, self.NODE_KEYS)
            if not node.get('name') or node['name'] in seen:
                raise ConfigError(f'{where}: every node needs its own name ({node.get("name")!r})')
            seen.add(node['name'])
            bad = set(node.get('roles') or ['']) - ROLES
            if bad:
                raise ConfigError(f'{where}: node {node["name"]}: unknown role {", ".join(sorted(bad))} (roles: {", ".join(sorted(ROLES))})')
            key = {'static': 'address', 'docker': None}.get(self.provider, 'instance')
            if key and not node.get(key):
                raise ConfigError(f'{where}: node {node["name"]} needs {key}')
            if node.get('fabric_role', 'edge') not in ('edge', 'via', 'stub'):
                raise ConfigError(f'{where}: node {node["name"]}: fabric_role must be edge, via or stub')
            if 'cpus' in node and not re.fullmatch(r'(\d+(-\d+)?)(,\d+(-\d+)?)*', str(node['cpus'])):
                raise ConfigError(f'{where}: node {node["name"]}: cpus must be a Linux CPU list such as "2-3"')
        relays = [n['name'] for n in self.with_role('relay')]
        self.fabric = Fabric(data['fabric'], relays, where) if 'fabric' in data else None
        if self.fabric is None and len(relays) != 1:
            raise ConfigError(f'{where}: exactly one node has the relay role (several need a [fabric] table)')
        if not self.with_role('subscriber'):
            raise ConfigError(f'{where}: no node has the subscriber role')
        if self.provider in ('aws', 'oci') and len({n['instance'] for n in self.load_nodes}) > 1:
            raise ConfigError(f'{where}: load nodes must share one instance type on {self.provider}')
        if self.provider in ('aws', 'oci') and len({(n['instance'], n.get('ocpus'), n.get('memory_gb'))
                                                    for n in self.with_role('relay')}) > 1:
            raise ConfigError(f'{where}: relay nodes must share one shape on {self.provider}')

    def with_role(self, role):
        return [n for n in self.nodes if role in n['roles']]

    @property
    def load_nodes(self):
        """Hosts the harness calls clients, in inventory order."""
        return [n for n in self.nodes if {'subscriber', 'publisher'} & set(n['roles']) and 'relay' not in n['roles']]

    @property
    def relay(self):
        return self.with_role('relay')[0]

    def role_counts(self):
        return {role: len(self.with_role(role)) for role in ROLES}

    def pub_host(self):
        """The publisher's place in relay-load.sh's terms."""
        nodes = self.with_role('publisher')
        if not nodes or 'relay' in nodes[0]['roles']:
            return 'relay'
        if 'probe' in nodes[0]['roles']:
            return 'probe'
        return f'client:{self.load_nodes.index(nodes[0]) + 1}'

    def sub_hosts(self, count=None):
        """Subscriber placement in relay-load.sh's terms, on the first count subscriber nodes."""
        nodes = self.with_role('subscriber')
        count = count or len(nodes)
        if count > len(nodes):
            raise ConfigError(f'{self.name}: {count} subscriber nodes asked for, it has {len(nodes)}')
        if count == len(self.load_nodes):
            return 'clients'
        if count == 1:
            return f'client:{self.load_nodes.index(nodes[0]) + 1}'
        raise ConfigError(f'{self.name}: subscribers on {count} of {len(self.load_nodes)} load nodes is not supported')

    def relay_vcpus(self):
        return self.relay.get('vcpus')

    def tfvars(self):
        """Terraform variables for this deployment, account settings included."""
        load = self.load_nodes
        probes = self.with_role('probe')
        out = dict(self.account)
        if self.region:
            out['region'] = self.region
        if self.provider == 'aws':
            if self.zone:
                out['availability_zone'] = self.zone
            if self.arch:
                out['arch'] = self.arch
            out.update(relay_instance_type=self.relay['instance'], relay_count=len(self.with_role('relay')),
                       client_count=len(load), probe_count=len(probes))
            if load:
                out['client_instance_type'] = load[0]['instance']
            if probes:
                out['probe_instance_type'] = probes[0]['instance']
        elif self.provider == 'oci':
            # image_ocid_<arch> in the account settings overrides image_ocid for that arch.
            images = {k: out.pop(k) for k in list(out) if k.startswith('image_ocid_')}
            if f'image_ocid_{self.arch}' in images:
                out['image_ocid'] = images[f'image_ocid_{self.arch}']
            if self.zone:
                out['availability_domain'] = self.zone
            if self.network_type:
                out['network_type'] = self.network_type
            out.update(relay_count=len(self.with_role('relay')), client_count=len(load), probe_count=len(probes))
            for prefix, node in (('relay', self.relay), ('client', load[0] if load else None), ('probe', probes[0] if probes else None)):
                if node:
                    out[f'{prefix}_shape'] = node['instance']
                    for key, var in (('ocpus', 'ocpus'), ('memory_gb', 'memory_gb')):
                        if key in node:
                            out[f'{prefix}_{var}'] = node[key]
        else:
            raise ConfigError(f'{self.name}: a {self.provider} deployment has no Terraform variables')
        return out


def load_deployment(name, root=ROOT):
    """deployments/<name>.toml, or <name>.local.toml; deployments/<provider>.local.toml adds account settings."""
    path = need('deployments', [f'{name}.toml', f'{name}.local.toml'], root)
    data = read_toml(path)
    account = {}
    account_file = find('deployments', [f'{data.get("provider")}.local.toml'], root)
    if account_file:
        account = read_toml(account_file).get('account', {})
    return Deployment(name, data, account, where=shown(path, root))


def render_tfvars(deployment):
    lines = [f'{GENERATED}{deployment.name}.toml; edit that instead.']
    for key, value in deployment.tfvars().items():
        lines.append(f'{key} = {json.dumps(value)}')
    return '\n'.join(lines) + '\n'


def write_tfvars(deployment, root=ROOT):
    """Write the environment file bin/testbed.sh reads, refusing to replace one written by hand."""
    path = Path(root) / deployment.provider / 'envs' / f'{deployment.name}.local.tfvars'
    if path.exists() and not path.read_text().startswith(GENERATED):
        raise ConfigError(f'{path} was not written by moqbench; move it away or rename the deployment')
    path.write_text(render_tfvars(deployment))
    return path


class Suite:
    KEYS = {'description', 'requires', 'test'}
    TEST_KEYS = {'type', 'tool', 'video', 'max_per_box', 'seconds', 'suffix', 'subscriber_nodes', 'relay', 'threads', 'note', *SETTINGS}
    FABRIC_KEYS = {'type', 'scenario', 'topology', 'links', 'seconds', 'suffix', 'note', 'draft',
                   'object_size', 'rate', 'group_size', 'pub_ns', 'peering_poll_s', 'slo_ms'}

    def __init__(self, name, data):
        self.name = name
        where = f'suites/{name}.toml'
        only_keys(where, data, self.KEYS)
        self.description = data.get('description', '')
        self.requires = data.get('requires', {})
        bad = set(self.requires) - ROLES
        if bad:
            raise ConfigError(f'{where}: requires an unknown role {", ".join(sorted(bad))}')
        self.tests = data.get('test') or []
        if not self.tests:
            raise ConfigError(f'{where}: no tests')
        shapes = json.loads((ROOT / 'scenarios/shapes.json').read_text())
        for i, test in enumerate(self.tests):
            at = f'{where}: test {i + 1}'
            if test.get('type') == 'fabric':
                self.check_fabric_test(at, test)
                continue
            only_keys(at, test, self.TEST_KEYS)
            if not isinstance(test.get('note', ''), str) or '\n' in test.get('note', ''):
                raise ConfigError(f'{at}: note must be one line of text')
            if test.get('type') not in TEST_TYPES:
                raise ConfigError(f'{at}: type must be one of {", ".join(sorted(TEST_TYPES))}')
            if test.get('tool') not in TOOLS:
                raise ConfigError(f'{at}: tool must be one of {", ".join(sorted(TOOLS))}')
            if test.get('video') not in shapes:
                raise ConfigError(f'{at}: video must be one of {", ".join(sorted(shapes))}')
            if test['type'] == 'ceiling' and test['tool'] != 'moqperf':
                raise ConfigError(f'{at}: a ceiling test needs moqperf; aiomoqt backs off at the SLO')
            if not re.fullmatch(r'(\d+(-\d+)?)(,\d+(-\d+)?)*', str(test.get('relay_cpus', '0'))):
                raise ConfigError(f'{at}: relay_cpus must be a Linux CPU list such as "0-3"')
            if test.get('moqx_stack', 'mvfst') not in ('mvfst', 'picoquic'):
                raise ConfigError(f'{at}: moqx_stack must be mvfst or picoquic')
            for key in ('hold_s', 'confirm_s'):
                if not isinstance(test.get(key, 0), (int, float)) or test.get(key, 0) < 0:
                    raise ConfigError(f'{at}: {key} must be seconds')
            if test.get('hold_s') and test['tool'] != 'aiomoqt':
                raise ConfigError(f'{at}: hold_s needs the aiomoqt tool')
            if 'join_search' in test:
                for key in ('join_search', 'searcher_subs'):
                    if not isinstance(test.get(key), int) or test[key] < 1:
                        raise ConfigError(f'{at}: join_search needs join_search and searcher_subs as positive counts')
                if test['tool'] != 'aiomoqt' or test.get('hold_s'):
                    raise ConfigError(f'{at}: join_search needs the aiomoqt tool and no hold_s')
            if 'aiomoqt_workers' in test:
                positive_int(at, 'aiomoqt_workers', test['aiomoqt_workers'])
            positive_int(at, 'max_per_box', test.get('max_per_box'))
            positive_int(at, 'seconds', test.get('seconds'))


    @staticmethod
    def check_fabric_test(at, test):
        from fabric import topology
        only_keys(at, test, Suite.FABRIC_KEYS)
        if not test.get('scenario') or find('scenarios', [f'fabric/{test["scenario"]}.toml']) is None:
            raise ConfigError(f'{at}: scenario must name a file in scenarios/fabric/, not {test.get("scenario")!r}')
        if test.get('topology', 'line') not in topology.SHAPES:
            raise ConfigError(f'{at}: topology must be one of {", ".join(sorted(topology.SHAPES))}')
        positive_int(at, 'seconds', test.get('seconds'))
        for key in ('draft', 'object_size', 'rate', 'group_size', 'slo_ms'):
            if key in test:
                positive_int(at, key, test[key])
        if not isinstance(test.get('note', ''), str) or '\n' in test.get('note', ''):
            raise ConfigError(f'{at}: note must be one line of text')

    @property
    def fabric_tests(self):
        return [t for t in self.tests if t['type'] == 'fabric']


def load_suite(name, root=ROOT):
    return Suite(name, read_toml(need('suites', [f'{name}.toml'], root)))


class Plan:
    KEYS = {'description', 'deployment', 'relays', 'suites', 'tune', 'budget', 'teardown', 'builds', 'threads', 'slo_ms'}
    BUILD_KEYS = {'moqx', 'aiomoqt', 'aiopquic', 'moxygen', 'python', *SOURCE_RELAYS.values()}

    def __init__(self, name, data, root=ROOT, path=None):
        self.name = name
        self.path = path
        where = shown(path, root) if path else f'plans/{name}.toml'
        only_keys(where, data, self.KEYS)
        self.description = data.get('description', '')
        self.deployment = load_deployment(data.get('deployment') or '', root)
        self.relays = data.get('relays') or ['moqx:pgo']
        self.suites = [s if isinstance(s, Suite) else load_suite(s, root) for s in data.get('suites') or []]
        if not self.suites:
            raise ConfigError(f'{where}: no suites')
        self.tune = data.get('tune', {})
        only_keys(f'{where}: tune', self.tune, {'rps'})
        self.budget_hours = data.get('budget', {}).get('hours')
        self.teardown = data.get('teardown', True)
        self.builds = data.get('builds', {})
        only_keys(f'{where}: builds', self.builds, self.BUILD_KEYS)
        self.threads = data.get('threads', 'all')
        self.slo_ms = data.get('slo_ms', 500)
        if self.deployment.fabric is not None or any(s.fabric_tests for s in self.suites):
            raise ConfigError(f'{where}: fabric deployments and suites run with moqbench fabric run, not in a plan')
        for relay in self.relays:
            relay_name, _, build = relay.partition(':')
            if relay_name in SOURCE_RELAYS and build == 'src':
                if SOURCE_RELAYS[relay_name] not in self.builds:
                    raise ConfigError(f'{where}: relay {relay!r} needs a git ref in builds.{SOURCE_RELAYS[relay_name]}')
            elif relay_name != 'moqx' or build not in BUILDS:
                raise ConfigError(f'{where}: relay {relay!r}: moqbench runs moqx:pgo, moqx:reg, moqdev:src, cf:src and aiomoqt:src; others go through bin/sweep.sh')
        counts = self.deployment.role_counts()
        for suite in self.suites:
            for role, need in suite.requires.items():
                if counts[role] < need:
                    raise ConfigError(f'suite {suite.name} needs {need} {role} node(s); {self.deployment.name} has {counts[role]}')

    def resolved_threads(self):
        if self.threads != 'all':
            positive_int(f'plans/{self.name}.toml', 'threads', self.threads)
            return self.threads
        vcpus = self.deployment.relay_vcpus()
        if not vcpus:
            raise ConfigError(f'threads = "all" needs vcpus on the relay node of {self.deployment.name}')
        return vcpus

    def points(self, suite):
        """One sweep point per test of a suite and relay, with each setting's value and source."""
        out = []
        for relay in self.relays:
            relay_name, _, build = relay.partition(':')
            for test in suite.tests:
                if test.get('relay', relay_name) != relay_name:
                    continue
                if (test['tool'], relay_name) in UNSUPPORTED:
                    raise ConfigError(f'suite {suite.name}: {test["tool"]} cannot run against {relay_name}')
                sub_hosts = self.deployment.sub_hosts(test.get('subscriber_nodes'))
                boxes = 1 if sub_hosts.startswith('client:') else len(self.deployment.load_nodes)
                threads = test.get('threads') or self.resolved_threads()
                env = {}
                for key, (var, default) in SETTINGS.items():
                    env[var] = (test[key], 'test') if key in test else (default, 'default')
                env['THREADS'] = (threads, 'test' if 'threads' in test else 'plan' if self.threads != 'all' else 'relay vCPUs')
                env['LATENCY_MS'] = (self.slo_ms, 'plan')
                env['PUB_HOST'] = (self.deployment.pub_host(), 'deployment')
                env['SUB_HOSTS'] = (sub_hosts, 'deployment' if 'subscriber_nodes' not in test else 'test')
                if relay_name != 'moqx':
                    env['RELAY'] = (relay_name, 'plan')
                video = test['video'].removesuffix('p')
                label = build if relay_name == 'moqx' else relay_name
                run = f'{TOOLS[test["tool"]]}-{label}{threads}-{boxes}b-{video}'
                for part in (self.deployment.tag, test.get('suffix')):
                    if part:
                        run += f'-{part}'
                out.append(dict(run=run, type=test['type'], tool=TOOLS[test['tool']], build=build, shape=test['video'],
                                max_per_box=test['max_per_box'], seconds=test['seconds'], note=test.get('note', ''),
                                env=env))
        return out

    def sweep_text(self, suite):
        """The suite as a bin/sweep.sh plan; every point states its whole environment. Comments carry descriptions and notes."""
        lines = [f'# Written by moqbench from plans/{self.name}.toml and suites/{suite.name}.toml.']
        lines += [f'# {k}: {" ".join(v.split())}' for k, v in (('plan', self.description), ('suite', suite.description)) if v]
        for point in self.points(suite):
            lines += [f'{var}={value}' for var, (value, _) in sorted(point['env'].items())]
            if point['note']:
                lines.append(f'# note: {point["note"]}')
            lines.append(f'{point["tool"]} {point["build"]} {point["shape"]} {point["max_per_box"]} {point["seconds"]} {point["run"]}')
        return '\n'.join(lines) + '\n'

    def seconds(self):
        return sum(p['seconds'] for s in self.suites for p in self.points(s))


def load_plan(name, root=ROOT):
    path = need('plans', [f'{name}.toml'], root)
    return Plan(name, read_toml(path), root, path)
