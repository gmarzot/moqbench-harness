"""Docker provider: every relay, a network-namespace holder per relay, and the clients, on one host.

Each relay runs in its own container inside a holder's network namespace, as a
pod does. The holder owns the relay's addresses, and its networks and qdiscs
survive the relay's kill and restart. Relays share an access network with the
clients and have one bridge network per wired relay pair, so impairing a link's
interface impairs exactly that link. The host is "local" or an ssh target; every
command runs there.
"""
import json
from pathlib import Path
import shlex
import subprocess
import time

from fabric import adapter as adapters
from fabric import certs, faults, topology

ROOT = Path(__file__).resolve().parents[2]
REMOTE_BIN = ROOT / 'remote' / 'fabric'
LAB_DOCKERFILE = ROOT / 'build' / 'lab-node.Dockerfile'


class FabricError(RuntimeError):
    """A fabric command that failed, with what it printed."""


class Host:
    def __init__(self, target, key=None):
        self.target = target
        self.key = key

    @property
    def local(self):
        return self.target == 'local'

    def argv(self, script):
        if self.local:
            return ['bash', '-c', script]
        # One multiplexed connection: a run issues hundreds of short commands.
        key = ['-i', self.key, '-o', 'StrictHostKeyChecking=accept-new'] if self.key else []
        return ['ssh', *key, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', '-o', 'ControlMaster=auto',
                '-o', 'ControlPath=~/.ssh/mb-%C', '-o', 'ControlPersist=300', self.target,
                'bash -c ' + shlex.quote(script)]

    def sh(self, script, check=True, timeout=300, input=None):
        for attempt in range(3):
            r = subprocess.run(self.argv(script), capture_output=True, text=True, timeout=timeout, input=input)
            if self.local or r.returncode != 255:
                break
            time.sleep(2 * (attempt + 1))
        if check and r.returncode:
            raise FabricError(f'{self.target}: exit {r.returncode}: {script[:200]}\n{r.stderr.strip()[-2000:]}')
        return r

    def push(self, local_dir, remote_dir):
        """Copy local_dir's contents into remote_dir, replacing files of the same name."""
        if self.local and Path(local_dir).resolve() == Path(remote_dir).resolve():
            return
        tar = subprocess.Popen(['tar', '-C', str(local_dir), '-cf', '-', '.'], stdout=subprocess.PIPE)
        r = subprocess.run(self.argv(f'mkdir -p {shlex.quote(remote_dir)} && tar -C {shlex.quote(remote_dir)} -xf -'),
                           stdin=tar.stdout, capture_output=True, text=True, timeout=300)
        tar.wait()
        if r.returncode or tar.returncode:
            raise FabricError(f'copy to {self.target}:{remote_dir} failed: {r.stderr.strip()}')

    def pull(self, remote_dir, local_dir):
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        src = subprocess.Popen(self.argv(f'tar -C {shlex.quote(remote_dir)} -cf - .'), stdout=subprocess.PIPE)
        r = subprocess.run(['tar', '-C', str(local_dir), '-xf', '-'], stdin=src.stdout, capture_output=True, text=True)
        src.wait()
        if r.returncode or src.returncode:
            raise FabricError(f'copy from {self.target}:{remote_dir} failed: {r.stderr.strip()}')


class DockerFabric:
    kind = 'docker'
    bin_dir = '/fabric/bin'
    python = 'python3'

    def __init__(self, deployment, link_list=None, host=None):
        self.dep = deployment
        self.spec = deployment.fabric
        self.adapter = adapters.get(self.spec.adapter)
        self.order = self.spec.order
        self.links = topology.normalize(link_list if link_list is not None else self.spec.links, self.order)
        self.clients = [n['name'] for n in deployment.nodes if 'relay' not in n['roles']]
        self.nodes = {n['name']: n for n in deployment.nodes}
        self.addr = topology.addressing(self.order, self.clients, self.spec.subnet, self.spec.wired(self.links))
        self.project = f'mb-{deployment.name}'.lower()
        self.host = host or Host(deployment.host)
        self.local_dir = ROOT / '.testbed' / 'fabric' / deployment.name
        self.user = None
        self.stack = (str(self.local_dir / 'stack') if self.host.local
                      else f'{self.spec.stack_dir}/{deployment.name}')

    def holder(self, node):
        return f'{self.project}-{node}'

    def relay(self, node):
        return f'{self.project}-{node}-relay'

    def client(self, name=None):
        return f'{self.project}-{name or self.clients[0]}'

    def load_targets(self):
        """Where subscriber populations run: every client container."""
        return [self.client(c) for c in self.clients]

    def results_dir(self, label):
        return f'/fabric/results/{label}'

    def access(self, node):
        return self.addr['access']['hosts'][node]

    def link_address(self, node, peer):
        """The address node dials to reach peer: peer's end of their link network."""
        return topology.link_address(self.addr, node, peer, peer)

    def names_by_address(self):
        out = {ip: n for n, ip in self.addr['access']['hosts'].items()}
        for pair, link in self.addr['links'].items():
            for n in pair:
                out[link[n]] = n
        return out

    def client_url(self, node):
        return f'moqt://{self.access(node)}:{self.adapter.client_port}{self.adapter.client_path}'

    def ctx(self):
        roles = {n: self.nodes[n].get('fabric_role', 'edge') for n in self.order}
        return dict(fabric=self.spec, addressing=self.addr, links=self.links, roles=roles)

    def compose(self, uid, gid):
        a = self.addr
        nets = {'access': {'internal': True, 'ipam': {'config': [{'subnet': a['access']['subnet']}]}}}
        for link in a['links'].values():
            nets[link['name']] = {'internal': True, 'ipam': {'config': [{'subnet': link['subnet']}]}}
        common = {'image': self.spec.lab_image, 'command': ['sleep', 'infinity'], 'init': True,
                  'volumes': ['./bin:/fabric/bin:ro', './results:/fabric/results']}
        services = {}
        ctx = self.ctx()
        for n in self.order:
            attach = {'access': {'ipv4_address': a['access']['hosts'][n]}}
            for pair, link in a['links'].items():
                if n in pair:
                    attach[link['name']] = {'ipv4_address': link[n]}
            services[n] = {**common, 'container_name': self.holder(n), 'hostname': n, 'cap_add': ['NET_ADMIN'],
                           'networks': attach}
            relay = self.adapter.service(n, ctx)
            relay.update({'container_name': self.relay(n), 'network_mode': f'service:{n}', 'depends_on': [n],
                          'user': f'{uid}:{gid}', 'restart': 'no',
                          'ulimits': {'nofile': {'soft': 1048576, 'hard': 1048576}}})
            if self.nodes[n].get('cpus'):
                relay['cpuset'] = str(self.nodes[n]['cpus'])
            services[f'{n}-relay'] = relay
        for c in self.clients:
            svc = {**common, 'container_name': self.client(c), 'hostname': c,
                   'networks': {'access': {'ipv4_address': a['access']['hosts'][c]}}}
            if self.nodes[c].get('cpus'):
                svc['cpuset'] = str(self.nodes[c]['cpus'])
            services[c] = svc
        return {'name': self.project, 'services': services, 'networks': nets}

    def render(self, uid=1000, gid=1000):
        """Write the stack to .testbed/fabric/<deployment>/stack; returns that folder."""
        stack = self.local_dir / 'stack'
        (stack / 'bin').mkdir(parents=True, exist_ok=True)
        (stack / 'build').mkdir(exist_ok=True)
        (stack / 'results').mkdir(exist_ok=True)
        (stack / 'compose.yaml').write_text(json.dumps(self.compose(uid, gid), indent=2) + '\n')
        ctx = self.ctx()
        for n in self.order:
            folder = stack / 'nodes' / n
            (folder / 'certs').mkdir(parents=True, exist_ok=True)
            for rel, text in self.adapter.render(n, ctx).items():
                (folder / rel).write_text(text)
            addrs = [a for pair, link in self.addr['links'].items() if n in pair for a in [link[n]]]
            pem, key = certs.ensure_leaf(self.local_dir / 'certs', n, [self.access(n), *addrs])
            for src, dst in ((pem, 'relay.pem'), (key, 'relay.key')):
                (folder / 'certs' / dst).write_bytes(src.read_bytes())
            (folder / 'certs' / 'relay.key').chmod(0o600)
        for script in REMOTE_BIN.glob('*.py'):
            (stack / 'bin' / script.name).write_bytes(script.read_bytes())
        (stack / 'build' / LAB_DOCKERFILE.name).write_bytes(LAB_DOCKERFILE.read_bytes())
        return stack

    def ids(self):
        out = self.host.sh('id -u; id -g').stdout.split()
        self.user = f'{out[0]}:{out[1]}'
        return int(out[0]), int(out[1])

    def compose_cmd(self, args):
        return f'cd {shlex.quote(self.stack)} && docker compose -p {self.project} -f compose.yaml {args}'

    def ensure_lab_image(self):
        if self.host.sh(f'docker image inspect {self.spec.lab_image} >/dev/null 2>&1', check=False).returncode == 0:
            return False
        self.host.sh(f'cd {shlex.quote(self.stack)} && docker build -q -t {self.spec.lab_image} '
                     f'--build-arg AIOMOQT={self.spec.aiomoqt} --build-arg AIOPQUIC={self.spec.aiopquic} '
                     f'-f build/{LAB_DOCKERFILE.name} build', timeout=1800)
        return True

    def up(self, wait_s=90):
        uid, gid = self.ids()
        stack = self.render(uid, gid)
        self.host.sh(self.compose_cmd('down -t 2 --remove-orphans') + ' >/dev/null 2>&1 || true', check=False)
        self.host.sh(f'mkdir -p {shlex.quote(self.stack)} && rm -rf {shlex.quote(self.stack)}/nodes')
        self.host.push(stack, self.stack)
        self.ensure_lab_image()
        self.host.sh(self.compose_cmd('up -d --force-recreate --remove-orphans'), timeout=300)
        self.wait_ready(wait_s)
        return self.wait_links(wait_s)

    def down(self):
        self.host.sh(self.compose_cmd('down -t 5 --remove-orphans'), check=False)

    def wait_ready(self, limit_s):
        end = time.time() + limit_s
        pending = list(self.order)
        while pending:
            pending = [n for n in pending if not self.safe(lambda n=n: self.adapter.ready(self, n))]
            if pending and time.time() > end:
                logs = {n: self.logs(n, tail=30) for n in pending}
                raise FabricError(f'relays not ready after {limit_s} s: {", ".join(pending)}\n' +
                                  '\n'.join(f'--- {n}\n{t}' for n, t in logs.items()))
            if pending:
                time.sleep(1)

    def link_up(self, a, b):
        rows = self.adapter.peer_state(self, a) or []
        return any(r['peer'] == b and r['status'] in ('connected', 'kConnected') for r in rows)

    def wait_links(self, limit_s, link_list=None):
        """Wait until every link's dialer reports its peer connected; returns seconds taken."""
        start = time.time()
        pending = list(link_list if link_list is not None else self.links)
        while pending:
            pending = [p for p in pending if not self.safe(lambda p=p: self.link_up(*p))]
            if pending and time.time() - start > limit_s:
                raise FabricError(f'links not connected after {limit_s} s: {pending}')
            if pending:
                time.sleep(0.5)
        return time.time() - start

    @staticmethod
    def safe(fn):
        try:
            return fn()
        except (FabricError, ValueError, KeyError):
            return False

    def exec(self, container, command, user=None, detach=False, check=True, timeout=120):
        flags = ['-d'] if detach else []
        if user:
            flags += ['-u', user, '-e', 'HOME=/tmp']
        script = f'docker exec {" ".join(flags)} {container} sh -c {shlex.quote(command)}'
        return self.host.sh(script, check=check, timeout=timeout)

    def admin(self, node, method, path, body=None):
        """One call to a relay's loopback control API, timed on the host clock inside its namespace."""
        url = f'http://127.0.0.1:8000{path}'
        arg = f' {shlex.quote(json.dumps(body))}' if body is not None else ''
        r = self.exec(self.holder(node), f'python3 /fabric/bin/fabric-admin.py {method} {url}{arg}')
        return json.loads(r.stdout)

    def reset(self, label):
        """An empty results folder for the run."""
        d = shlex.quote(f'{self.stack}/results/{label}')
        self.host.sh(f'rm -rf {d} && mkdir -p {d}')

    def done(self, label, target=None):
        """Names whose <name>.rc exists in the run's results."""
        r = self.host.sh(f'cd {shlex.quote(self.stack)}/results/{label} && ls *.rc 2>/dev/null || true')
        return {Path(x).stem for x in r.stdout.split()}

    def telemetry(self, label, seconds, peering_s=5, netdev_s=0.25, state_ms=None, state_s=None):
        """Pollers beside each relay: /peering every peering_s, interface counters every netdev_s,
        and /state every state_ms for state_s seconds when given."""
        out = self.results_dir(label)
        for n in self.order:
            urls = self.adapter.poll_urls(n)
            jobs = [(f'--url peering={urls["peering"]}', peering_s, seconds, '')] if 'peering' in urls else []
            jobs.append(('--netdev', netdev_s, seconds, '-net'))
            if state_ms and 'state' in urls:
                jobs.append((f'--url state={urls["state"]}', state_ms / 1000, state_s or seconds, '-state'))
            for args, interval, secs, suffix in jobs:
                self.exec(self.holder(n), f'python3 {self.bin_dir}/fabric-poll.py {args} --interval {interval} '
                          f'--duration {secs} --out {out}/poll{suffix}-{n}.jsonl', user=self.user, detach=True)

    def clock(self, label, tag):
        """One host, one clock: nothing to record."""

    def link_action(self, verb, sender, peer, spec=None, side='send'):
        """netem on sender's interface on its link to peer; returns that end's host times."""
        if side != 'send':
            raise faults.FaultError('a docker fabric impairs the sending side only')
        ip = topology.link_address(self.addr, sender, peer, sender)
        cmd = {'link.impair': lambda: faults.netem_cmd(ip, spec),
               'link.down': lambda: faults.netem_cmd(ip, 'loss 100%'),
               'link.clear': lambda: faults.clear_cmd(ip)}[verb]()
        r = self.exec(self.holder(sender), faults.timed(cmd))
        return {'node': sender, **json.loads(r.stdout)}

    def node_action(self, verb, node, signal='KILL', grace_s=5):
        r = self.host.sh(faults.timed(faults.node_cmd('docker', verb, self.relay(node), signal, grace_s)))
        return json.loads(r.stdout)

    def leftovers(self):
        left = {}
        for n in self.order:
            r = self.exec(self.holder(n), faults.CHECK_CMD, check=False)
            if r.stdout.strip():
                left[n] = r.stdout.strip().splitlines()
        return left

    def clear_impairments(self):
        for (a, b), link in self.addr['links'].items():
            for n in (a, b):
                self.exec(self.holder(n), faults.clear_cmd(link[n]), check=False)
        return self.leftovers()

    def collect(self, label, out_dir):
        self.host.pull(f'{self.stack}/results/{label}', out_dir)

    def logs(self, node, tail=None):
        t = f' --tail {int(tail)}' if tail else ''
        r = self.host.sh(f'docker logs{t} {self.relay(node)} 2>&1', check=False)
        return r.stdout

    def now(self):
        return float(self.host.sh('date +%s.%N').stdout)

    def image_info(self, image):
        r = self.host.sh(f"docker image inspect {shlex.quote(image)} --format '{{{{json .}}}}'", check=False)
        if r.returncode:
            return None
        doc = json.loads(r.stdout)
        return {'id': doc.get('Id'), 'tags': doc.get('RepoTags'), 'labels': (doc.get('Config') or {}).get('Labels')}
