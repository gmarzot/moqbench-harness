"""VM provider for contribution runs: src and dst are two VMs reached over ssh, and the real path
between them is the WAN.

Each node runs the same images as the Docker provider with host networking, pinned to the node's
cpus, under its own work folder. A holder container per node (host network, NET_ADMIN) runs the
network commands: the ifb that impairs what arrives from the peer, the dummy interface that loops the
multicast groups back, and the emulator's trees and nft table. A run folder has the same path under
each node's work folder; collect() merges both into the local results folder. Every container carries
the label org.moqbench.contrib=<deployment>.
"""
import json
import re
import shlex
import time

from fabric.docker import Host, FabricError
from contrib import emu, topology
from contrib.docker import BIN, LABEL, run_argv, stack_work

PING_RE = re.compile(r'= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms')


def chrony(text):
    """{offset_ms, last_offset_ms, rms_offset_ms, stratum, leap, synced} from `chronyc tracking`;
    offset_ms is signed, positive when the system clock is ahead of NTP time."""
    out = dict(offset_ms=None, last_offset_ms=None, rms_offset_ms=None, stratum=None, leap=None, synced=False)
    m = re.search(r'System time\s*:\s*([\d.]+) seconds (fast|slow)', text)
    if m:
        out['offset_ms'] = round(float(m[1]) * 1e3 * (1 if m[2] == 'fast' else -1), 6)
    for key, label in (('last_offset_ms', 'Last offset'), ('rms_offset_ms', 'RMS offset')):
        m = re.search(label + r'\s*:\s*([-+\d.]+) seconds', text)
        if m:
            out[key] = round(float(m[1]) * 1e3, 6)
    m = re.search(r'Stratum\s*:\s*(\d+)', text)
    if m:
        out['stratum'] = int(m[1])
    m = re.search(r'Leap status\s*:\s*(.+)', text)
    if m:
        out['leap'] = m[1].strip()
    out['synced'] = out['offset_ms'] is not None and out['leap'] not in (None, 'Not synchronised')
    return out


def ping_stats(text):
    m = PING_RE.search(text)
    return dict(zip(('min', 'avg', 'max', 'mdev'), map(float, m.groups()))) if m else None


class Node:
    """One VM: its ssh host, work folder, uid:gid and holder container."""

    def __init__(self, ns, spec, dep, prefix):
        self.ns, self.dep = ns, dep
        self.host = Host(spec['host'])
        self.address = spec['address']
        self.cpus = spec.get('cpus')
        self.holder = f'{prefix}-{ns}'
        self._work = None
        self._nic = None
        self.user = None

    def sh(self, script, check=True, timeout=300, input=None):
        return self.host.sh(script, check=check, timeout=timeout, input=input)

    @property
    def work(self):
        if self._work is None:
            self._work = stack_work(self.sh, self.dep.stack_dir, self.dep.name, self.host.target)
        return self._work

    def ids(self):
        if self.dep.uid is not None:
            self.user = f'{self.dep.uid}:{self.dep.gid if self.dep.gid is not None else self.dep.uid}'
        else:
            out = self.sh('id -u; id -g').stdout.split()
            self.user = f'{out[0]}:{out[1]}'
        return self.user

    def nic(self, peer_address):
        """The interface this node reaches its peer on."""
        if self._nic is None:
            out = self.sh(f'ip -o route get {shlex.quote(peer_address)}').stdout
            m = re.search(r'\bdev (\S+)', out)
            if not m:
                raise FabricError(f'{self.ns}: no route to {peer_address}: {out.strip()}')
            self._nic = m[1]
        return self._nic

    def exec(self, cmd, check=False, timeout=120):
        r = self.sh(f'docker exec {self.holder} sh -c {shlex.quote(cmd)}', check=check, timeout=timeout)
        return r.returncode, r.stdout + r.stderr

    def chrony(self):
        r = self.sh('chronyc -n tracking', check=False, timeout=30)
        return chrony(r.stdout) if r.returncode == 0 else dict(chrony(''), error=(r.stderr or r.stdout).strip()[-300:])


class VmContrib:
    kind = 'vm'

    def __init__(self, dep, dual=False):
        self.dep = dep
        self.prefix = f'mbc-{dep.name}'.lower()
        self.nodes = {ns: Node(ns, dep.nodes[ns], dep, self.prefix) for ns in ('src', 'dst')}
        self.paths = topology.VmPaths(self.nodes['src'].address, self.nodes['dst'].address, self.nic)
        self.where = {}
        self.user = None

    def nic(self, ns):
        return self.nodes[ns].nic(self.nodes[topology.VmPaths.peer(ns)].address)

    @property
    def work(self):
        """The src node's work folder: sources and run folders are addressed by their src path."""
        return self.nodes['src'].work

    def on(self, ns, path):
        """A path under src's work folder, as the same path under ns's."""
        src = self.nodes['src'].work
        if ns == 'src' or not (path == src or path.startswith(src + '/')):
            return path
        return self.nodes[ns].work + path[len(src):]

    def holder(self, ns):
        return self.nodes[ns].holder

    def image(self, key):
        return self.dep.images[key]

    def sh(self, script, check=True, timeout=300):
        """A shell on src, which holds the sources and the source FIFO."""
        return self.nodes['src'].sh(script, check=check, timeout=timeout)

    def ids(self):
        for node in self.nodes.values():
            node.ids()
        self.user = self.nodes['src'].user
        return self.user

    def bin_dir(self):
        for node in self.nodes.values():
            node.sh(f'mkdir -p {shlex.quote(node.work)}/bin')
            node.host.push(BIN, f'{node.work}/bin')
        return f'{self.work}/bin'

    def exec(self, ns, cmd, check=False, timeout=120):
        return self.nodes[ns].exec(cmd, check=check, timeout=timeout)

    def wan_exec(self, cmd):
        raise FabricError('a VM deployment has no wan namespace')

    def emulator(self, mux_bps):
        return emu.VmEmulator(self.paths, self.exec, mux_bps)

    def pending(self, lane, test, features):
        """Why a lane cannot run on two VMs, or None."""
        if lane.name == 'moqt-aiomoqt-dual' or test.dual_path:
            return 'dual_path needs the docker provider'
        if lane.name == 'moqt-aiomoqt-loc' and not features.get('frame_log'):
            return 'the LOC fallback tap is on CLOCK_MONOTONIC; VM runs need sub_media --frame-log'
        return None

    def holder_argv(self, node):
        return ['docker', 'run', '-d', '--name', node.holder, '--label', f'{LABEL}={self.dep.name}',
                '--network', 'host', '--cap-add', 'NET_ADMIN', '--cap-add', 'NET_RAW',
                *(['--cpuset-cpus', str(node.cpus)] if node.cpus else []), '--init', self.image('tools'),
                'sleep', 'infinity']

    def preflight(self, node):
        """Docker, every image and a synchronised chrony on a node; raises listing what is missing."""
        problems = []
        r = node.sh("docker version --format '{{.Server.Version}}'", check=False, timeout=60)
        if r.returncode:
            problems.append(f'docker: {(r.stderr or r.stdout).strip()[-200:]}')
        else:
            images = ' '.join(shlex.quote(i) for i in sorted(set(self.dep.images.values())))
            r = node.sh(f'for i in {images}; do docker image inspect "$i" >/dev/null 2>&1 || echo "$i"; done',
                        check=False, timeout=120)
            problems += [f'image {i}' for i in r.stdout.split()]
        c = node.chrony()
        if not c['synced']:
            problems.append(f"chrony not synchronised ({c.get('leap') or c.get('error') or 'no tracking'})")
        if problems:
            raise FabricError(f'{node.ns} ({node.host.target}): ' + '; '.join(problems))

    def up(self):
        """Holders, multicast loopback and the ifb on both nodes; idempotent (rebuilds from scratch)."""
        self.down()
        for ns, node in self.nodes.items():
            self.preflight(node)
            node.sh('sudo -n modprobe ifb numifbs=0', check=False)
            node.sh(' '.join(shlex.quote(a) for a in self.holder_argv(node)))
            rc, out = node.exec('set -e; ' + '; '.join(self.paths.setup_cmds(ns)))
            if rc:
                raise FabricError(f'{ns}: network setup failed: {out[-800:]}')
        return self.check()

    def check(self):
        """Ping each way, offloads, clock sync and the deployment's network state on both nodes (what
        up made, or what down left); raises when a node cannot reach the other. Runs without up."""
        out = dict(nodes={})
        for ns, node in self.nodes.items():
            peer = self.nodes[topology.VmPaths.peer(ns)].address
            nic = self.nic(ns)
            text = self.oneshot(node, f'ping -c 3 -i 0.2 -W 1 {peer} && echo PING-OK')
            if 'PING-OK' not in text:
                raise FabricError(f'{ns} cannot reach {peer}:\n{text[-800:]}')
            off = self.oneshot(node, f"ethtool -k {nic} | grep -E '^(generic-segmentation|generic-receive|"
                                     "large-receive|tcp-segmentation|tx-udp-segmentation)-offload'")
            state = self.oneshot(node, self.paths.leftover_cmd(nic))
            out['nodes'][ns] = dict(host=node.host.target, nic=nic, ping=ping_stats(text),
                                    offloads=' '.join(ln.split(':')[1].split()[0] for ln in off.splitlines() if ':' in ln),
                                    chrony=node.chrony(), state=[ln.strip() for ln in state.splitlines() if ln.strip()])
        return out

    def oneshot(self, node, script):
        """A shell in a throwaway tools container on a node's host network."""
        r = node.sh(f'docker run --rm --network host --cap-add NET_ADMIN --cap-add NET_RAW '
                    f'--label {LABEL}={self.dep.name} {shlex.quote(self.image("tools"))} sh -c {shlex.quote(script)}',
                    check=False, timeout=120)
        return r.stdout + r.stderr

    def down(self):
        """Every container of the deployment, then the ifb, filter, dummy, nft table and offloads."""
        for ns, node in self.nodes.items():
            r = node.sh(f'docker ps -aq --filter label={LABEL}={self.dep.name}', check=False)
            ids = r.stdout.split()
            if ids:
                node.sh(f'docker rm -f {" ".join(ids)} >/dev/null', check=False)
            self.oneshot(node, '; '.join(self.paths.teardown_cmds(self.nic(ns))) + '; true')

    def run_dir(self, label, local_out):
        d = f'{self.work}/runs/{label}'
        for ns, node in self.nodes.items():
            n = shlex.quote(self.on(ns, d))
            node.sh(f'rm -rf {n} && mkdir -p {n} && chmod 777 {n}')
        return d

    def write(self, run_dir, name, text):
        for ns, node in self.nodes.items():
            r = node.sh(f'cat > {shlex.quote(self.on(ns, run_dir))}/{shlex.quote(name)}', check=False, input=text)
            if r.returncode:
                raise FabricError(f'{ns}: cannot write {name}: {r.stderr}')

    def start(self, proc, run_dir, bin_dir, cell, sources_dir=None):
        """Start one process on its node with host networking; returns the container name."""
        if proc.ns not in self.nodes:
            raise FabricError(f'{proc.name}: a VM deployment has no {proc.ns} namespace')
        node = self.nodes[proc.ns]
        name = f'{self.prefix}-{cell}'
        rd = self.on(proc.ns, run_dir)
        qlog = proc.env.get('AIOPQUIC_QLOG_DIR', '')
        if qlog.startswith('/out/'):
            node.sh(f'mkdir -p -m 777 {shlex.quote(rd + qlog[4:])}')
        user = None if proc.image == 'moqx' or proc.root else node.user or node.ids()
        # Sources exist on src only.
        args = run_argv(proc, self.image(proc.image), name, f'{LABEL}={self.dep.name}', 'host', rd,
                        self.on(proc.ns, bin_dir), user, node.cpus, sources_dir if proc.ns == 'src' else None)
        node.sh(' '.join(shlex.quote(a) for a in args))
        self.where[name] = proc.ns
        return name

    def by_node(self, names):
        out = {}
        for n in names:
            out.setdefault(self.where.get(n, 'src'), []).append(n)
        return out

    def stop(self, names, signal='TERM', grace_s=5):
        groups = self.by_node(names)
        for ns, group in groups.items():
            self.nodes[ns].sh(f'docker kill -s {signal} {" ".join(group)} >/dev/null 2>&1; true', check=False)
        end = time.time() + grace_s
        while groups and time.time() < end:
            for ns in list(groups):
                running = self.nodes[ns].sh('docker inspect -f "{{.State.Running}}" ' + ' '.join(groups[ns]) +
                                            ' 2>/dev/null', check=False).stdout.split()
                if 'true' not in running:
                    del groups[ns]
            if groups:
                time.sleep(0.5)

    def logs(self, name):
        return self.nodes[self.where.get(name, 'src')].sh(f'docker logs -t {name} 2>&1', check=False, timeout=120).stdout

    def state(self, name):
        r = self.nodes[self.where.get(name, 'src')].sh(f"docker inspect -f '{{{{json .State}}}}' {name}", check=False)
        try:
            return json.loads(r.stdout)
        except ValueError:
            return {}

    def remove(self, names):
        for ns, group in self.by_node(names).items():
            self.nodes[ns].sh(f'docker rm -f {" ".join(group)} >/dev/null 2>&1; true', check=False)
            for n in group:
                self.where.pop(n, None)

    def collect(self, run_dir, out_dir):
        """Both nodes' run folders into one local folder; their file names do not overlap."""
        for ns, node in self.nodes.items():
            node.host.pull(self.on(ns, run_dir), str(out_dir))

    def image_info(self, key):
        """The image on src, with each node's image id."""
        ids = {}
        doc = None
        for ns, node in self.nodes.items():
            r = node.sh(f"docker image inspect {shlex.quote(self.image(key))} --format '{{{{json .}}}}'", check=False)
            if r.returncode:
                ids[ns] = None
                continue
            d = json.loads(r.stdout)
            ids[ns] = d.get('Id')
            if ns == 'src':
                doc = d
        if doc is None:
            return None
        return {'id': doc.get('Id'), 'tags': doc.get('RepoTags'), 'digests': doc.get('RepoDigests'),
                'labels': (doc.get('Config') or {}).get('Labels'), 'node_ids': ids}

    def probe(self, image_key, cmd):
        r = self.sh(f'docker run --rm --network none --entrypoint sh {shlex.quote(self.image(image_key))} '
                    f'-c {shlex.quote(cmd)}', check=False, timeout=120)
        return r.stdout + r.stderr

    def cell_meta(self):
        """Each node's chrony tracking and the unimpaired RTT src to dst, for a run's meta."""
        rc, text = self.nodes['src'].exec(f'ping -c 5 -i 0.2 -W 1 {self.paths.dst_ip()}')
        return dict(clock={ns: node.chrony() for ns, node in self.nodes.items()},
                    base_rtt_ms=ping_stats(text) if rc == 0 else None, t=time.time())
