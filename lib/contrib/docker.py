"""Docker provider for contribution runs: one holder container per namespace, plain veths between them,
and each lane process in its own container joined to a holder's network namespace.

Every container carries the label org.moqbench.contrib=<deployment>; up, down and cleanup touch only
those. The run folder is bind-mounted at /out and remote/contrib at /contrib/bin.
"""
import json
from pathlib import Path
import shlex
import time

from fabric.docker import Host, FabricError
from contrib import topology

ROOT = Path(__file__).resolve().parents[2]
BIN = ROOT / 'remote' / 'contrib'
LABEL = 'org.moqbench.contrib'


def cpu_mask(cpus):
    """Hex CPU mask of a Linux CPU list such as "2-5,8"; every CPU when none is given."""
    if not cpus:
        return 'f' * 32
    bits = 0
    for part in str(cpus).split(','):
        lo, _, hi = part.partition('-')
        for c in range(int(lo), int(hi or lo) + 1):
            bits |= 1 << c
    return f'{bits:x}'


class DockerContrib:
    kind = 'docker'

    def __init__(self, dep, dual=False):
        self.dep = dep
        self.host = Host(dep.host)
        self.paths = topology.Paths(str(dep.subnet), dual=dual)
        self.prefix = f'mbc-{dep.name}'.lower()
        self.local_dir = ROOT / '.testbed' / 'contrib' / dep.name
        self.work = str(self.local_dir) if self.host.local else f'{dep.stack_dir}/{dep.name}'
        self.user = None

    def holder(self, ns):
        return f'{self.prefix}-{ns}'

    def image(self, key):
        return self.dep.images[key]

    def sh(self, script, check=True, timeout=300):
        return self.host.sh(script, check=check, timeout=timeout)

    def ids(self):
        if self.dep.uid is not None:
            self.user = f'{self.dep.uid}:{self.dep.gid if self.dep.gid is not None else self.dep.uid}'
        else:
            out = self.sh('id -u; id -g').stdout.split()
            self.user = f'{out[0]}:{out[1]}'
        return self.user

    def bin_dir(self):
        if self.host.local:
            return str(BIN)
        self.sh(f'mkdir -p {shlex.quote(self.work)}/bin')
        self.host.push(BIN, f'{self.work}/bin')
        return f'{self.work}/bin'

    def exec(self, ns, cmd, check=False, timeout=120):
        r = self.sh(f'docker exec {self.holder(ns)} sh -c {shlex.quote(cmd)}', check=check, timeout=timeout)
        return r.returncode, r.stdout + r.stderr

    def wan_exec(self, cmd):
        return self.exec('wan', cmd)

    def up(self):
        """Holders, veths, addresses, routes, offloads off; idempotent (rebuilds from scratch)."""
        self.down()
        tools = self.image('tools')
        for ns in ('src', 'wan', 'dst'):
            sysctl = ['--sysctl', 'net.ipv4.ip_forward=1'] if ns == 'wan' else []
            sysctl += ['--sysctl', 'net.ipv4.conf.all.rp_filter=0', '--sysctl', 'net.ipv4.conf.default.rp_filter=0']
            cpus = self.dep.cpus(ns)
            args = ['docker', 'run', '-d', '--name', self.holder(ns), '--hostname', ns, '--label', f'{LABEL}={self.dep.name}',
                    '--network', 'none', '--cap-add', 'NET_ADMIN', '--cap-add', 'NET_RAW', *sysctl,
                    *(['--cpuset-cpus', str(cpus)] if cpus else []), '--init', tools, 'sleep', 'infinity']
            self.sh(' '.join(shlex.quote(a) for a in args))
        pids = {ns: self.sh(f"docker inspect -f '{{{{.State.Pid}}}}' {self.holder(ns)}").stdout.strip()
                for ns in ('src', 'wan', 'dst')}
        plumb = '; '.join(self.paths.veth_cmds(pids))
        self.sh(f'docker run --rm --privileged --pid=host --network host --label {LABEL}={self.dep.name} '
                f'{tools} sh -c {shlex.quote("set -e; " + plumb)}')
        for ns, cmds in self.paths.ns_cmds().items():
            rc, out = self.exec(ns, 'set -e; ' + '; '.join(cmds))
            if rc:
                raise FabricError(f'{ns}: network setup failed: {out[-800:]}')
        rps = []
        for ns, devs in self.paths.rx_devs().items():
            mask = cpu_mask(self.dep.cpus(ns))
            writes = '; '.join(f'echo {mask} > /sys/class/net/{d}/queues/rx-0/rps_cpus' for d in devs)
            rps.append(f"nsenter -t {pids[ns]} -n -m sh -c 'mount -o remount,rw /sys && {writes}; "
                       "mount -o remount,ro /sys'")
        self.sh(f'docker run --rm --privileged --pid=host --network host --label {LABEL}={self.dep.name} '
                f'{tools} sh -c {shlex.quote("set -e; " + "; ".join(rps))}')
        return self.check()

    def check(self):
        """Ping across wan both ways and read the offload state; raises on failure."""
        rc, out = self.exec('src', f'ping -c 2 -W 1 {self.paths.dst_ip()}')
        if rc:
            raise FabricError(f'src cannot reach dst through wan:\n{out[-800:]}')
        offloads, rps = {}, {}
        for ns, devs in (('src', [ln['src_if'] for ln in self.paths.links]),
                         ('wan', self.paths.fwd_dev + self.paths.rev_dev),
                         ('dst', [ln['dst_if'] for ln in self.paths.links])):
            for dev in devs:
                _, text = self.exec(ns, f"ethtool -k {dev} | grep -E '^(generic-segmentation|generic-receive|tcp-segmentation|tx-udp-segmentation)-offload'")
                offloads[f'{ns}:{dev}'] = ' '.join(ln.split(':')[1].split()[0] for ln in text.splitlines() if ':' in ln)
                rps[f'{ns}:{dev}'] = self.exec(ns, f'cat /sys/class/net/{dev}/queues/rx-0/rps_cpus')[1].strip()
        return dict(ping=out.strip().splitlines()[-1] if out.strip() else '', offloads=offloads, rps=rps)

    def down(self):
        r = self.sh(f'docker ps -aq --filter label={LABEL}={self.dep.name}', check=False)
        ids = r.stdout.split()
        if ids:
            self.sh(f'docker rm -f {" ".join(ids)} >/dev/null', check=False)

    def run_dir(self, label, local_out):
        """Where the run's containers write: the local results folder itself, or a folder on the Docker
        host that collect() pulls back."""
        d = str(local_out) if self.host.local else f'{self.work}/runs/{label}'
        self.sh(f'rm -rf {shlex.quote(d)} && mkdir -p {shlex.quote(d)} && chmod 777 {shlex.quote(d)}')
        return d

    def write(self, run_dir, name, text):
        r = self.host.sh(f'cat > {shlex.quote(run_dir)}/{shlex.quote(name)}', input=text)
        if r.returncode:
            raise FabricError(f'cannot write {name}: {r.stderr}')

    def start(self, proc, run_dir, bin_dir, cell, sources_dir=None):
        """Start one process in its namespace; returns the container name."""
        name = f'{self.prefix}-{cell}'
        env = []
        for k, v in sorted(proc.env.items()):
            env += ['-e', f'{k}={v}']
        cpus = self.dep.cpus(proc.ns)
        base = ['docker', 'run', '-d', '--init', '-e', 'TINI_KILL_PROCESS_GROUP=1', '--name', name,
                '--label', f'{LABEL}={self.dep.name}',
                '--network', f'container:{self.holder(proc.ns)}', '-v', f'{run_dir}:/out',
                '-v', f'{bin_dir}:/contrib/bin:ro', *(['-v', f'{sources_dir}:/sources:ro'] if sources_dir else []),
                *env, *(['--cpuset-cpus', str(cpus)] if cpus else [])]
        if proc.image != 'moqx':
            base += ['--user', self.user or self.ids()]
        img = self.image(proc.image)
        if proc.entrypoint:
            # The shell outlives every process of its job, so a signal reaches each of a pipeline (tini
            # forwards it to the group) and each can flush before the container stops.
            args = base + ['--entrypoint', 'sh', img, '-c', f"{proc.cmd} & trap '' TERM INT; wait"]
        else:
            args = base + [img] + (shlex.split(proc.cmd) if proc.cmd else [])
        self.sh(' '.join(shlex.quote(a) for a in args))
        return name

    def stop(self, names, signal='TERM', grace_s=5):
        if not names:
            return
        self.sh(f'docker kill -s {signal} {" ".join(names)} >/dev/null 2>&1; true', check=False)
        end = time.time() + grace_s
        while time.time() < end:
            running = self.sh('docker inspect -f "{{.State.Running}}" ' + ' '.join(names) + ' 2>/dev/null',
                              check=False).stdout.split()
            if 'true' not in running:
                break
            time.sleep(0.5)

    def logs(self, name):
        r = self.sh(f'docker logs -t {name} 2>&1', check=False, timeout=120)
        return r.stdout

    def state(self, name):
        r = self.sh(f"docker inspect -f '{{{{json .State}}}}' {name}", check=False)
        try:
            return json.loads(r.stdout)
        except ValueError:
            return {}

    def remove(self, names):
        if names:
            self.sh(f'docker rm -f {" ".join(names)} >/dev/null 2>&1; true', check=False)

    def collect(self, run_dir, out_dir):
        if not self.host.local:
            self.host.pull(run_dir, str(out_dir))

    def image_info(self, key):
        r = self.sh(f"docker image inspect {shlex.quote(self.image(key))} --format '{{{{json .}}}}'", check=False)
        if r.returncode:
            return None
        doc = json.loads(r.stdout)
        return {'id': doc.get('Id'), 'tags': doc.get('RepoTags'), 'digests': doc.get('RepoDigests'),
                'labels': (doc.get('Config') or {}).get('Labels')}

    def probe(self, image_key, cmd):
        """Run a command in an image (no network); returns its output."""
        r = self.sh(f'docker run --rm --network none --entrypoint sh {shlex.quote(self.image(image_key))} '
                    f'-c {shlex.quote(cmd)}', check=False, timeout=120)
        return r.stdout + r.stderr
