"""VM provider: each relay on its own VM, clients on the reference and load VMs.

Hosts come from the selected testbed's inventory (.testbed/inventory.json): its
relays in order are the fabric's relay nodes in deployment order, the first
probe is the reference host (one clock for every reference client), and the
clients are the load hosts. Relays run the adapter's image with host
networking; fabric links are the relays' private addresses.

An impairment never touches the NIC's tuned qdiscs: a flower filter on the
peer's address, on the NIC's clsact egress hook (sending side) or ingress hook
(receiving side), redirects that peer's packets through an ifb device whose
root is netem. An nft table counts bytes per peer in each direction, so link
rates are exact.
"""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shlex
import subprocess
import time

from fabric import adapter as adapters
from fabric import certs, faults, topology
from fabric.docker import ROOT, REMOTE_BIN, FabricError, Host

NFT_TABLE = 'moqbench_fabric'
DOCKER = 'sudo docker'


def load_inventory(deployment):
    path = ROOT / '.testbed' / 'inventory.json'
    if not path.is_file():
        raise FabricError(f'no hosts selected: ./bin/testbed.sh {deployment.name} use')
    inv = json.loads(path.read_text())
    if inv.get('deployment') != deployment.name:
        raise FabricError(f'the selected hosts are {inv.get("deployment")!r}, not {deployment.name}: '
                          f'./bin/testbed.sh {deployment.name} use')
    return inv


def nft_rules(peers):
    """An nft script that (re)creates the per-peer counter table; peers is {name: address}."""
    out = [f'add table inet {NFT_TABLE}', f'delete table inet {NFT_TABLE}', f'table inet {NFT_TABLE} {{']
    for chain, hook, field in (('out', 'output', 'daddr'), ('in', 'input', 'saddr')):
        out.append(f'  chain {chain} {{ type filter hook {hook} priority 0; policy accept;')
        out += [f'    ip {field} {ip} counter comment "{name}"' for name, ip in sorted(peers.items())]
        out.append('  }')
    out.append('}')
    return '\n'.join(out) + '\n'


def run_args(service, root):
    """docker run arguments for a compose-style service, with ./ paths under root."""
    args = []
    for vol in service.get('volumes', []):
        args += ['-v', vol.replace('./', f'"$HOME/{root}/"', 1) if vol.startswith('./') else vol]
    for k, v in sorted((service.get('environment') or {}).items()):
        args += ['-e', f'{k}={v}']
    return args


class VMFabric:
    kind = 'vm'

    def __init__(self, deployment, link_list=None, inventory=None):
        self.dep = deployment
        self.spec = deployment.fabric
        self.adapter = adapters.get(self.spec.adapter)
        self.order = self.spec.order
        self.links = topology.normalize(link_list if link_list is not None else self.spec.links, self.order)
        inv = inventory if inventory is not None else load_inventory(deployment)
        relays = inv.get('relays') or [inv['relay']]
        relay_nodes = [n['name'] for n in deployment.nodes if 'relay' in n['roles']]
        if len(relays) != len(relay_nodes):
            raise FabricError(f'{deployment.name} has {len(relay_nodes)} relay nodes; the inventory has {len(relays)} relays')
        self.hosts = dict(zip(relay_nodes, relays))
        ref = (inv.get('probes') or inv.get('clients') or [None])[0]
        if ref is None:
            raise FabricError(f'{deployment.name}: no probe or client host for the reference clients')
        self.hosts['ref'] = ref
        self.loads = []
        for i, c in enumerate(inv.get('clients') or [], 1):
            self.hosts[f'load-{i}'] = c
            self.loads.append(f'load-{i}')
        self.clients = ['ref']
        self.nodes = {n['name']: n for n in deployment.nodes}
        self.user_name = inv.get('user') or 'ubuntu'
        self.key = inv.get('ssh_key_file')
        self.project = f'mb-{deployment.name}'.lower()
        self.root = f'{self.spec.stack_dir}/{deployment.name}'
        self.bin_dir = f'{self.root}/bin'
        self.python = self.spec.client_python
        self.local_dir = ROOT / '.testbed' / 'fabric' / deployment.name
        self.addr = {'access': {'subnet': None, 'hosts': {t: h['private'] for t, h in self.hosts.items()}},
                     'links': {pair: {'name': '-'.join(pair), pair[0]: self.hosts[pair[0]]['private'],
                                      pair[1]: self.hosts[pair[1]]['private']}
                               for pair in topology.links('full', self.order)}}
        self.host = self.hostfor('ref')
        self.user = None

    def hostfor(self, token):
        return Host(f'{self.user_name}@{self.hosts[token]["public"]}', key=self.key)

    def holder(self, node):
        return node

    def relay(self, node):
        return f'{self.project}-{node}-relay'

    def client(self, name=None):
        return name or 'ref'

    def load_targets(self):
        return list(self.loads) or ['ref']

    def results_dir(self, label):
        return f'{self.root}/results/{label}'

    def access(self, node):
        return self.hosts[node]['private']

    def link_address(self, node, peer):
        return self.hosts[peer]['private']

    def names_by_address(self):
        return {h['private']: t for t, h in self.hosts.items()}

    def client_url(self, node):
        return f'moqt://{self.access(node)}:{self.adapter.client_port}{self.adapter.client_path}'

    def ctx(self):
        roles = {n: self.nodes[n].get('fabric_role', 'edge') for n in self.order}
        return dict(fabric=self.spec, addressing=self.addr, links=self.links, roles=roles)

    def exec(self, target, command, user=None, detach=False, check=True, timeout=120):
        script = command
        if detach:
            script = f'nohup setsid bash -c {shlex.quote(command)} >/dev/null 2>&1 </dev/null &'
        return self.hostfor(target).sh(script, check=check, timeout=timeout)

    def each(self, targets, fn):
        with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
            return dict(zip(targets, pool.map(fn, targets)))

    def render(self, uid=1000, gid=1000):
        """Each relay's config and certificate, and the helper scripts, under .testbed/fabric/<deployment>/stack."""
        stack = self.local_dir / 'stack'
        (stack / 'bin').mkdir(parents=True, exist_ok=True)
        ctx = self.ctx()
        for n in self.order:
            folder = stack / 'nodes' / n
            (folder / 'certs').mkdir(parents=True, exist_ok=True)
            for rel, text in self.adapter.render(n, ctx).items():
                (folder / rel).write_text(text)
            pem, key = certs.ensure_leaf(self.local_dir / 'certs', n,
                                         [self.hosts[n]['private'], self.hosts[n]['public']])
            for src, dst in ((pem, 'relay.pem'), (key, 'relay.key')):
                (folder / 'certs' / dst).write_bytes(src.read_bytes())
            (folder / 'certs' / 'relay.key').chmod(0o600)
        for script in [*REMOTE_BIN.glob('*.py'), ROOT / 'remote' / 'load-monitor.py']:
            (stack / 'bin' / script.name).write_bytes(script.read_bytes())
        return stack

    def ids(self):
        out = self.hostfor(self.order[0]).sh('id -u; id -g').stdout.split()
        self.user = f'{out[0]}:{out[1]}'
        return int(out[0]), int(out[1])

    def start_cmd(self, node):
        svc = self.adapter.service(node, self.ctx())
        cpus = self.nodes[node].get('cpus')
        args = ['-d', '--name', self.relay(node), '--network', 'host', '--user', self.user, '--restart', 'no',
                '--ulimit', 'nofile=1048576:1048576', *(['--cpuset-cpus', str(cpus)] if cpus else []),
                *run_args(svc, self.root), svc['image']]
        return (f'{DOCKER} rm -f {self.relay(node)} >/dev/null 2>&1; '
                f'{DOCKER} run {" ".join(args)}')

    def prepare(self, node):
        """Docker on the relay host, and the relay image already there."""
        host = self.hostfor(node)
        host.sh('command -v docker >/dev/null || { sudo DEBIAN_FRONTEND=noninteractive apt-get '
                '-o DPkg::Lock::Timeout=300 install -y -q docker.io >/dev/null; }; '
                'command -v nft >/dev/null || sudo DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 '
                'install -y -q nftables >/dev/null', timeout=900)
        if host.sh(f'{DOCKER} image inspect {shlex.quote(self.spec.image)} >/dev/null 2>&1', check=False).returncode:
            raise FabricError(f'{node}: no image {self.spec.image}; moqbench fabric deploy {self.dep.name}')

    def counters(self, node):
        peers = {p: self.hosts[p]['private'] for p in self.order if p != node}
        self.hostfor(node).sh(f'printf %s {shlex.quote(nft_rules(peers))} | sudo nft -f -')

    def up(self, wait_s=90):
        uid, gid = self.ids()
        stack = self.render(uid, gid)

        def relay_up(n):
            self.prepare(n)
            host = self.hostfor(n)
            host.sh(f'mkdir -p {self.root} && rm -rf {self.root}/nodes/{n}')
            tmp = stack.parent / f'push-{n}'
            subprocess.run(['rm', '-rf', str(tmp)], check=True)
            (tmp / 'nodes').mkdir(parents=True)
            subprocess.run(['cp', '-a', str(stack / 'nodes' / n), str(tmp / 'nodes' / n)], check=True)
            subprocess.run(['cp', '-a', str(stack / 'bin'), str(tmp / 'bin')], check=True)
            host.push(tmp, self.root)
            host.sh(self.start_cmd(n))
            self.counters(n)

        def client_up(t):
            host = self.hostfor(t)
            host.sh(f'mkdir -p {self.root}')
            tmp = stack.parent / f'push-{t}'
            subprocess.run(['rm', '-rf', str(tmp)], check=True)
            tmp.mkdir(parents=True)
            subprocess.run(['cp', '-a', str(stack / 'bin'), str(tmp / 'bin')], check=True)
            host.push(tmp, self.root)

        self.each(self.order, relay_up)
        self.each(['ref', *self.loads], client_up)
        self.wait_ready(wait_s)
        return self.wait_links(wait_s)

    def down(self):
        def relay_down(n):
            self.hostfor(n).sh(f'{DOCKER} rm -f {self.relay(n)} >/dev/null 2>&1; '
                               f'sudo nft delete table inet {NFT_TABLE} 2>/dev/null; true', check=False)
        self.each(self.order, relay_down)
        self.clear_impairments()

    def deploy(self, source=None):
        """Copy the relay image to every relay host that lacks it, from source (an ssh target) or the
        first relay host that has it. Returns the hosts it copied to."""
        image = shlex.quote(self.spec.image)
        have = self.each(self.order, lambda n: self.hostfor(n).sh(
            f'{DOCKER} image inspect {image} --format "{{{{.Id}}}}" 2>/dev/null', check=False).stdout.strip())
        src = Host(source, key=self.key) if source else next(
            (self.hostfor(n) for n in self.order if have[n]), None)
        if src is None:
            raise FabricError(f'no relay host has {self.spec.image}: ./bin/moqx-peering-build.sh {self.dep.name}, '
                              'or name a Docker host with --from')
        want = src.sh(f'{DOCKER if not source else "docker"} image inspect {image} --format "{{{{.Id}}}}"').stdout.strip()
        copied = []
        for n in self.order:
            if have[n] == want:
                continue
            self.prepare_docker(n)
            save = subprocess.Popen(src.argv(f'{DOCKER if not source else "docker"} save {image} | gzip -1'),
                                    stdout=subprocess.PIPE)
            load = subprocess.run(self.hostfor(n).argv(f'gunzip | {DOCKER} load >/dev/null'), stdin=save.stdout,
                                  capture_output=True, text=True, timeout=3600)
            save.wait()
            if load.returncode or save.returncode:
                raise FabricError(f'{n}: image copy failed: {load.stderr.strip()[-500:]}')
            copied.append(n)
        return copied

    def prepare_docker(self, node):
        self.hostfor(node).sh('command -v docker >/dev/null || sudo DEBIAN_FRONTEND=noninteractive apt-get '
                              '-o DPkg::Lock::Timeout=300 install -y -q docker.io >/dev/null', timeout=900)

    def wait_ready(self, limit_s):
        end = time.time() + limit_s
        pending = list(self.order)
        while pending:
            pending = [n for n in pending if not self.safe(lambda n=n: self.adapter.ready(self, n))]
            if pending and time.time() > end:
                raise FabricError(f'relays not ready after {limit_s} s: {", ".join(pending)}\n' +
                                  '\n'.join(f'--- {n}\n{self.logs(n, tail=30)}' for n in pending))
            if pending:
                time.sleep(1)

    def link_up(self, a, b):
        rows = self.adapter.peer_state(self, a) or []
        return any(r['peer'] == b and r['status'] == 'connected' for r in rows)

    def wait_links(self, limit_s, link_list=None):
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

    def admin(self, node, method, path, body=None):
        url = f'http://127.0.0.1:8000{path}'
        arg = f' {shlex.quote(json.dumps(body))}' if body is not None else ''
        r = self.exec(node, f'python3 {self.bin_dir}/fabric-admin.py {method} {url}{arg}')
        return json.loads(r.stdout)

    def logs(self, node, tail=None):
        t = f' --tail {int(tail)}' if tail else ''
        return self.hostfor(node).sh(f'{DOCKER} logs{t} {self.relay(node)} 2>&1', check=False).stdout

    def now(self):
        return float(self.host.sh('date +%s.%N').stdout)

    def image_info(self, image):
        r = self.hostfor(self.order[0]).sh(f"{DOCKER} image inspect {shlex.quote(image)} --format '{{{{json .}}}}'",
                                           check=False)
        if r.returncode:
            return None
        doc = json.loads(r.stdout)
        return {'id': doc.get('Id'), 'tags': doc.get('RepoTags'), 'labels': (doc.get('Config') or {}).get('Labels')}

    def targets(self):
        return [*self.order, 'ref', *self.loads]

    def reset(self, label):
        d = self.results_dir(label)
        self.each(self.targets(), lambda t: self.exec(t, f'rm -rf {d} && mkdir -p {d}'))

    def done(self, label, target):
        r = self.exec(target, f'cd {self.results_dir(label)} 2>/dev/null && ls *.rc 2>/dev/null || true')
        return {Path(x).stem for x in r.stdout.split()}

    def telemetry(self, label, seconds, peering_s=5, netdev_s=0.25, state_ms=None, state_s=None):
        """load-monitor on every host (relays add /peering every peering_s and the nft counters every
        sample), and fast /state polling beside each relay when state_ms is given."""
        out = self.results_dir(label)
        interval = max(0.25, netdev_s)

        def start(t):
            extra = ''
            if t in self.order:
                urls = self.adapter.poll_urls(t)
                extra = (f'--comm moqx --nft {NFT_TABLE} --json-interval {peering_s} '
                         + ' '.join(f'--json-url {k}={u}' for k, u in urls.items() if k == 'peering'))
            self.exec(t, f'mkdir -p {out} && rm -f {out}/monitor.stop && python3 -u {self.bin_dir}/load-monitor.py '
                      f'--duration {int(seconds)} --interval {interval} --stop-file {out}/monitor.stop {extra} '
                      f'> {out}/monitor-{t}.jsonl 2> {out}/monitor-{t}.err', detach=True)
            if state_ms and t in self.order and 'state' in self.adapter.poll_urls(t):
                self.exec(t, f'python3 {self.bin_dir}/fabric-poll.py --url state={self.adapter.poll_urls(t)["state"]} '
                          f'--interval {state_ms / 1000} --duration {state_s or seconds} '
                          f'--out {out}/poll-state-{t}.jsonl', detach=True)
        self.each(self.targets(), start)

    def clock(self, label, tag):
        """chronyc tracking on every host, into the run's results."""
        out = self.results_dir(label)
        self.each(self.targets(), lambda t: self.exec(
            t, f'mkdir -p {out} && chronyc -n tracking > {out}/clock-{t}-{tag}.txt 2>&1', check=False))

    def link_action(self, verb, sender, peer, spec=None, side='send'):
        """Impair the traffic sender sends to peer: on sender's egress (send) or peer's ingress (receive)."""
        at, other = (sender, peer) if side == 'send' else (peer, sender)
        index = self.order.index(other) + 1
        own, far = self.hosts[at]['private'], self.hosts[other]['private']
        if verb == 'link.clear':
            cmd = faults.ifb_clear_cmd(own, index, side)
        else:
            cmd = faults.ifb_impair_cmd(own, far, index, side, spec if verb == 'link.impair' else 'loss 100%')
        r = self.exec(at, faults.timed(cmd))
        return {'node': at, 'side': side, **json.loads(r.stdout)}

    def node_action(self, verb, node, signal='KILL', grace_s=5):
        r = self.hostfor(node).sh(faults.timed(faults.node_cmd(DOCKER, verb, self.relay(node), signal, grace_s)))
        return json.loads(r.stdout)

    def leftovers(self):
        found = self.each(self.order, lambda n: self.exec(n, faults.IFB_CHECK_CMD, check=False).stdout.strip())
        return {n: text.splitlines() for n, text in found.items() if text}

    def clear_impairments(self):
        def clear(n):
            own = self.hosts[n]['private']
            for i in range(1, len(self.order) + 1):
                for side in ('send', 'receive'):
                    self.exec(n, faults.ifb_clear_cmd(own, i, side), check=False)
        self.each(self.order, clear)
        return self.leftovers()

    def collect(self, label, out_dir):
        def stop(t):
            self.exec(t, f'touch {self.results_dir(label)}/monitor.stop', check=False)
        self.each(self.targets(), stop)
        time.sleep(2)
        for t in self.targets():
            try:
                self.hostfor(t).pull(self.results_dir(label), out_dir)
            except FabricError:
                pass
