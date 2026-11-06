"""Deployments, suites and plans: load, validate, and resolve into sweep points.

A deployment says what hardware exists and what each node does. A suite says
which tests to run, with no hardware in it. A plan pairs them with the relays,
builds and tuning of one series, and is what `moqbench run` executes.
"""
import json
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]
ROLES = {'relay', 'subscriber', 'publisher', 'probe'}
PROVIDERS = {'aws', 'oci', 'static'}
TOOLS = {'aiomoqt': 'aio', 'moqperf': 'mp'}
TEST_TYPES = {'capacity': 'ramp until the latency SLO is crossed',
              'ceiling': 'ramp past the SLO to the egress plateau'}
BUILDS = {'pgo', 'reg'}
# moq-rs closes the moqperf publisher's session at moxygen's 30 s keepalive.
UNSUPPORTED = {('moqperf', 'cf')}
GENERATED = '# Written by moqbench from deployments/'

# Settings a test can set, the environment variable each becomes, and its default.
SETTINGS = {
    'draft': ('DRAFT', 18), 'step_subs': ('STEP_SUBS', 250), 'join_s': ('JOIN', 10), 'stagger_s': ('STAGGER', 0.02),
    'shortfall': ('SHORTFALL', 0.7), 'delivery_timeout_ms': ('DELIVERY_TIMEOUT', 500), 'mp_threads': ('MP_THREADS', 32),
    'mp_ramp': ('MP_RAMP', 64), 'moqx_send': ('MOQX_SEND', 16), 'moqx_recv': ('MOQX_RECV', 256), 'moqx_gro': ('MOQX_GRO', ''),
    'aiomoqt_env': ('AIOMOQT_ENV', ''),
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


def only_keys(where, data, allowed):
    unknown = set(data) - set(allowed)
    if unknown:
        raise ConfigError(f'{where}: unknown {", ".join(sorted(unknown))} (expected: {", ".join(sorted(allowed))})')


def positive_int(where, key, value):
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f'{where}: {key} must be a positive integer, not {value!r}')


class Deployment:
    NODE_KEYS = {'name', 'instance', 'roles', 'vcpus', 'ocpus', 'memory_gb', 'address'}
    KEYS = {'provider', 'region', 'zone', 'arch', 'tag', 'nodes'}

    def __init__(self, name, data, account=None, where=None):
        self.name, self.account = name, account or {}
        where = where or f'deployments/{name}.toml'
        only_keys(where, data, self.KEYS)
        self.provider = data.get('provider')
        if self.provider not in PROVIDERS:
            raise ConfigError(f'{where}: provider must be one of {", ".join(sorted(PROVIDERS))}')
        self.region, self.zone, self.arch = data.get('region'), data.get('zone'), data.get('arch')
        self.tag = data.get('tag', '')
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
            key = 'address' if self.provider == 'static' else 'instance'
            if not node.get(key):
                raise ConfigError(f'{where}: node {node["name"]} needs {key}')
        if len(self.with_role('relay')) != 1:
            raise ConfigError(f'{where}: exactly one node has the relay role')
        if not self.with_role('subscriber'):
            raise ConfigError(f'{where}: no node has the subscriber role')
        if self.provider != 'static' and len({n['instance'] for n in self.load_nodes}) > 1:
            raise ConfigError(f'{where}: load nodes must share one instance type on {self.provider}')

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
            out.update(relay_instance_type=self.relay['instance'], client_count=len(load), probe_count=len(probes))
            if load:
                out['client_instance_type'] = load[0]['instance']
            if probes:
                out['probe_instance_type'] = probes[0]['instance']
        elif self.provider == 'oci':
            if self.zone:
                out['availability_domain'] = self.zone
            out.update(client_count=len(load), probe_count=len(probes))
            for prefix, node in (('relay', self.relay), ('client', load[0] if load else None), ('probe', probes[0] if probes else None)):
                if node:
                    out[f'{prefix}_shape'] = node['instance']
                    for key, var in (('ocpus', 'ocpus'), ('memory_gb', 'memory_gb')):
                        if key in node:
                            out[f'{prefix}_{var}'] = node[key]
        else:
            raise ConfigError(f'{self.name}: a static deployment has no Terraform variables')
        return out


def load_deployment(name, root=ROOT):
    """deployments/<name>.toml, or <name>.local.toml; deployments/<provider>.local.toml adds account settings."""
    folder = Path(root) / 'deployments'
    path = folder / f'{name}.toml'
    if not path.exists():
        path = folder / f'{name}.local.toml'
    data = read_toml(path)
    account = {}
    account_file = folder / f'{data.get("provider")}.local.toml'
    if account_file.exists():
        account = read_toml(account_file).get('account', {})
    return Deployment(name, data, account, where=str(path.relative_to(root)))


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
    TEST_KEYS = {'type', 'tool', 'video', 'max_per_box', 'seconds', 'suffix', 'subscriber_nodes', *SETTINGS}

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
            only_keys(at, test, self.TEST_KEYS)
            if test.get('type') not in TEST_TYPES:
                raise ConfigError(f'{at}: type must be one of {", ".join(sorted(TEST_TYPES))}')
            if test.get('tool') not in TOOLS:
                raise ConfigError(f'{at}: tool must be one of {", ".join(sorted(TOOLS))}')
            if test.get('video') not in shapes:
                raise ConfigError(f'{at}: video must be one of {", ".join(sorted(shapes))}')
            if test['type'] == 'ceiling' and test['tool'] != 'moqperf':
                raise ConfigError(f'{at}: a ceiling test needs moqperf; aiomoqt backs off at the SLO')
            positive_int(at, 'max_per_box', test.get('max_per_box'))
            positive_int(at, 'seconds', test.get('seconds'))


def load_suite(name, root=ROOT):
    return Suite(name, read_toml(Path(root) / 'suites' / f'{name}.toml'))


class Plan:
    KEYS = {'description', 'deployment', 'relays', 'suites', 'tune', 'budget', 'teardown', 'builds', 'threads', 'slo_ms'}
    BUILD_KEYS = {'moqx', 'aiomoqt', 'aiopquic', 'moxygen'}

    def __init__(self, name, data, root=ROOT):
        self.name = name
        where = f'plans/{name}.toml'
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
        for relay in self.relays:
            relay_name, _, build = relay.partition(':')
            if relay_name != 'moqx' or build not in BUILDS:
                raise ConfigError(f'{where}: relay {relay!r}: moqbench runs moqx:pgo and moqx:reg today; others go through bin/sweep.sh')
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
                if (test['tool'], relay_name) in UNSUPPORTED:
                    raise ConfigError(f'suite {suite.name}: {test["tool"]} cannot run against {relay_name}')
                sub_hosts = self.deployment.sub_hosts(test.get('subscriber_nodes'))
                boxes = 1 if sub_hosts.startswith('client:') else len(self.deployment.load_nodes)
                threads = self.resolved_threads()
                env = {}
                for key, (var, default) in SETTINGS.items():
                    env[var] = (test[key], 'test') if key in test else (default, 'default')
                env['THREADS'] = (threads, 'plan' if self.threads != 'all' else 'relay vCPUs')
                env['LATENCY_MS'] = (self.slo_ms, 'plan')
                env['PUB_HOST'] = (self.deployment.pub_host(), 'deployment')
                env['SUB_HOSTS'] = (sub_hosts, 'deployment' if 'subscriber_nodes' not in test else 'test')
                video = test['video'].removesuffix('p')
                run = f'{TOOLS[test["tool"]]}-{build}{threads}-{boxes}b-{video}'
                for part in (self.deployment.tag, test.get('suffix')):
                    if part:
                        run += f'-{part}'
                out.append(dict(run=run, type=test['type'], tool=TOOLS[test['tool']], build=build, shape=test['video'],
                                max_per_box=test['max_per_box'], seconds=test['seconds'], env=env))
        return out

    def sweep_text(self, suite):
        """The suite as a bin/sweep.sh plan; every point states its whole environment."""
        lines = [f'# Written by moqbench from plans/{self.name}.toml and suites/{suite.name}.toml.']
        for point in self.points(suite):
            lines += [f'{var}={value}' for var, (value, _) in sorted(point['env'].items())]
            lines.append(f'{point["tool"]} {point["build"]} {point["shape"]} {point["max_per_box"]} {point["seconds"]} {point["run"]}')
        return '\n'.join(lines) + '\n'

    def seconds(self):
        return sum(p['seconds'] for s in self.suites for p in self.points(s))


def load_plan(name, root=ROOT):
    return Plan(name, read_toml(Path(root) / 'plans' / f'{name}.toml'), root)
