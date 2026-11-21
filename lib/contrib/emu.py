"""The WAN emulator in the wan namespace: qdisc trees per direction, nft accounting and outages.

Accounting counters see every packet entering wan, before any qdisc, per direction. An outage adds
the direction's ingress interface to an nft set that one counted drop rule matches, so outage drops
are counted apart from netem's. Cross traffic is also counted on its own, so it can be told apart from
the lane's. The trees are built once per run; events change them in place.

VmEmulator builds the same trees on two VMs: the forward tree on dst's ifb, the reverse on src's.
Accounting counts what each node sends to the other on its egress; an outage adds the far sender's
address to the receiver's nft set, dropped on ingress after the tree.
"""
import json
import time

from contrib.profiles import CROSS_MAX, CROSS_PORT
from fabric import faults

TABLE = 'mbc'
CHAIN = 'mbc_forward'
QDISC_KINDS = ('netem', 'tbf', 'fq_codel')


CHANGE_SEED_STEP = 7919


def nft_script(paths):
    rules = []
    for ln in paths.links:
        rules.append(f'    iifname "{ln["wan_a"]}" oifname "{ln["wan_b"]}" counter comment "fwd{ln["k"]}"')
        rules.append(f'    iifname "{ln["wan_b"]}" oifname "{ln["wan_a"]}" counter comment "rev{ln["k"]}"')
    ports = f'{CROSS_PORT}-{CROSS_PORT + CROSS_MAX - 1}'
    for ln in paths.links:
        rules.append(f'    iifname "{ln["wan_a"]}" oifname "{ln["wan_b"]}" meta l4proto {{ tcp, udp }} th dport {ports} '
                     f'counter comment "cross-fwd{ln["k"]}"')
        rules.append(f'    iifname "{ln["wan_b"]}" oifname "{ln["wan_a"]}" meta l4proto {{ tcp, udp }} th sport {ports} '
                     f'counter comment "cross-rev{ln["k"]}"')
    rules.append('    iifname @blocked counter drop comment "outage"')
    return '\n'.join([f'table inet {TABLE} {{', '  set blocked { type ifname; }',
                      f'  chain {CHAIN} {{ type filter hook forward priority -10; policy accept;',
                      *rules, '  }', '}']) + '\n'


def parse_counters(text):
    """{comment: {packets, bytes}} of every counted rule in `nft -j list` output."""
    res = {}
    for item in json.loads(text).get('nftables', []):
        rule = item.get('rule')
        if not rule:
            continue
        counter = next((e['counter'] for e in rule.get('expr', []) if 'counter' in e), None)
        if counter is not None:
            res[rule.get('comment', '')] = dict(packets=counter['packets'], bytes=counter['bytes'])
    return res


def parse_qdiscs(text):
    return [dict(dev=q.get('dev'), kind=q.get('kind'), handle=q.get('handle'), parent=q.get('parent'),
                 packets=q.get('packets'), bytes=q.get('bytes'), drops=q.get('drops'),
                 overlimits=q.get('overlimits'), backlog=q.get('backlog'), qlen=q.get('qlen'),
                 options=q.get('options'))
            for q in json.loads(text) if q.get('kind') in QDISC_KINDS]


class Emulator:
    """Impairment control through wan_exec(command) -> (rc, stdout)."""

    def __init__(self, paths, wan_exec, mux_bps):
        self.paths, self.exec, self.mux_bps = paths, wan_exec, mux_bps
        self.profile = None
        self.seeds = (None, None)
        self.changes = {}

    def sh(self, cmd):
        rc, out = self.exec(cmd)
        if rc:
            raise RuntimeError(f'wan: {cmd}: exit {rc}: {out[-500:]}')
        return out

    def build(self, profile, repeat=0):
        """Every direction's tree, and the nft table with zeroed counters."""
        self.profile = profile
        self.seeds = profile.seeds(repeat)
        self.changes = {}
        cmds = [f'nft delete table inet {TABLE} 2>/dev/null; true']
        for k, (fdev, rdev) in enumerate(zip(self.paths.fwd_dev, self.paths.rev_dev)):
            fseed, rseed = (s + 500 * k for s in self.seeds)
            cmds += profile.forward.build(fdev, self.mux_bps, fseed)
            cmds += profile.reverse.build(rdev, self.mux_bps, rseed)
        for c in cmds:
            self.sh(c)
        script = nft_script(self.paths).replace("'", "'\\''")
        self.sh(f"printf '%s' '{script}' | nft -f -")

    def side(self, dev):
        return self.profile.forward if dev in self.paths.fwd_dev else self.profile.reverse

    def seed_of(self, dev):
        """A fresh seed for each change of a device's netem: netem restarts its PRNG at the seed a
        change restates, so reusing one would replay the loss pattern after every event."""
        k = (self.paths.fwd_dev + self.paths.rev_dev).index(dev) % len(self.paths.links)
        n = self.changes[dev] = self.changes.get(dev, 0) + 1
        return (self.seeds[0] if dev in self.paths.fwd_dev else self.seeds[1]) + 500 * k + CHANGE_SEED_STEP * n

    def devs(self, direction):
        f, r = self.paths.fwd_dev[:1], self.paths.rev_dev[:1]
        return {'fwd': f, 'rev': r, 'both': f + r}[direction]

    def ingress(self, direction):
        ln = self.paths.links[0]
        return {'fwd': [ln['wan_a']], 'rev': [ln['wan_b']], 'both': [ln['wan_a'], ln['wan_b']]}[direction]

    def command(self, action, args):
        """The shell for one timeline action."""
        if action in ('outage_on', 'outage_off'):
            verb = 'add' if action == 'outage_on' else 'delete'
            elems = ', '.join(f'"{d}"' for d in self.ingress(args['direction']))
            return f'nft {verb} element inet {TABLE} blocked {{ {elems} }}'
        if action == 'delay':
            cmds = []
            for dev in self.devs(args['direction']):
                side = self.side(dev)
                cmds.append(side.change_delay(dev, side.delay_ms + args['delta_ms'], self.seed_of(dev)))
            return ' && '.join(cmds)
        if action == 'rate':
            return self.profile.forward.change_rate(self.paths.fwd_dev[0], self.mux_bps, args['factor'])
        raise ValueError(f'unknown emulator action {action!r}')

    def apply(self, action, args):
        """Run one action, timed on the host clock; returns the record logged to faults.jsonl."""
        cmd = self.command(action, args)
        rc, out = self.exec(faults.timed(cmd))
        rec = dict(action=action, **args, command=cmd)
        try:
            rec.update(json.loads(out.strip().splitlines()[-1]))
        except (ValueError, IndexError):
            rec.update(rc=rc, t0=time.time(), t1=time.time())
        if action == 'outage_off':
            rec['drops'] = self.outage_drops()
        return rec

    def run_timeline(self, actions, start, log_path, stop=lambda: False, handlers=None):
        """Apply (t_s, action, args) at start + t_s (time.monotonic); every record goes to log_path.
        handlers maps an action the emulator does not run itself (cross traffic) to fn(args) -> record."""
        handlers = handlers or {}
        done = []
        with open(log_path, 'a') as log:
            for t_s, action, args in actions:
                while time.monotonic() < start + t_s:
                    if stop():
                        return done
                    time.sleep(min(0.01, start + t_s - time.monotonic()))
                rec = handlers[action](args) if action in handlers else self.apply(action, args)
                rec['at_s'] = t_s
                done.append(rec)
                log.write(json.dumps(rec) + '\n')
                log.flush()
        return done

    def counters(self):
        """{comment: {packets, bytes}} from the nft accounting chain."""
        rc, out = self.exec(f'nft -j list chain inet {TABLE} {CHAIN}')
        return parse_counters(out) if rc == 0 else {}

    def outage_drops(self):
        return self.counters().get('outage', {}).get('packets')

    def qdisc_stats(self):
        """[{dev, kind, handle, packets, bytes, drops, overlimits, backlog, qlen}] for every qdisc in wan."""
        rc, out = self.exec('tc -s -j qdisc show')
        return parse_qdiscs(out) if rc == 0 else []

    def clear(self):
        cmds = [f'nft delete table inet {TABLE} 2>/dev/null']
        cmds += [f'tc qdisc del dev {d} root 2>/dev/null' for d in self.paths.fwd_dev + self.paths.rev_dev]
        self.exec('; '.join(cmds) + '; true')

    def leftovers(self):
        rc, out = self.exec(f"tc qdisc show | grep -E 'netem|tbf|fq_codel'; nft list table inet {TABLE} 2>/dev/null | head -1; true")
        return [ln for ln in out.splitlines() if ln.strip()]


def vm_nft_script(paths, ns):
    """A node's table: what it sends to its peer counted on egress (fwd0 from src, rev0 from dst, and
    cross traffic apart), and outage drops of what arrives from a blocked address."""
    ln = paths.links[0]
    way, far = ('fwd', ln['dst_ip']) if ns == 'src' else ('rev', ln['src_ip'])
    port = 'dport' if ns == 'src' else 'sport'
    ports = f'{CROSS_PORT}-{CROSS_PORT + CROSS_MAX - 1}'
    return '\n'.join([f'table inet {TABLE} {{', '  set blocked { type ipv4_addr; }',
                      '  chain mbc_out { type filter hook output priority -10; policy accept;',
                      f'    ip daddr {far} counter comment "{way}0"',
                      f'    ip daddr {far} meta l4proto {{ tcp, udp }} th {port} {ports} counter comment "cross-{way}0"',
                      '  }',
                      '  chain mbc_in { type filter hook prerouting priority -10; policy accept;',
                      '    ip saddr @blocked counter drop comment "outage"',
                      '  }', '}']) + '\n'


class VmEmulator(Emulator):
    """Impairment control through node_exec(ns, command) -> (rc, stdout) on the src and dst VMs."""

    def __init__(self, paths, node_exec, mux_bps):
        super().__init__(paths, None, mux_bps)
        self.node_exec = node_exec

    def on(self, ns, cmd):
        rc, out = self.node_exec(ns, cmd)
        if rc:
            raise RuntimeError(f'{ns}: {cmd}: exit {rc}: {out[-500:]}')
        return out

    def build(self, profile, repeat=0):
        self.profile = profile
        self.seeds = profile.seeds(repeat)
        self.changes = {}
        for ns, side, seed in (('dst', profile.forward, self.seeds[0]), ('src', profile.reverse, self.seeds[1])):
            self.on(ns, f'nft delete table inet {TABLE} 2>/dev/null; true')
            for c in side.build(self.paths.ifb(ns), self.mux_bps, seed):
                self.on(ns, c)
            script = vm_nft_script(self.paths, ns).replace("'", "'\\''")
            self.on(ns, f"printf '%s' '{script}' | nft -f -")

    def commands(self, action, args):
        """[(node, shell)] for one timeline action."""
        if action in ('outage_on', 'outage_off'):
            verb = 'add' if action == 'outage_on' else 'delete'
            nodes = {'fwd': ['dst'], 'rev': ['src'], 'both': ['dst', 'src']}[args['direction']]
            return [(ns, f'nft {verb} element inet {TABLE} blocked {{ {self.paths.addr[self.paths.peer(ns)]} }}')
                    for ns in nodes]
        if action == 'delay':
            out = []
            for dev in self.devs(args['direction']):
                side = self.side(dev)
                out.append((self.paths.node_of(dev),
                            side.change_delay(dev, side.delay_ms + args['delta_ms'], self.seed_of(dev))))
            return out
        if action == 'rate':
            return [('dst', self.profile.forward.change_rate(self.paths.fwd_dev[0], self.mux_bps, args['factor']))]
        raise ValueError(f'unknown emulator action {action!r}')

    def command(self, action, args):
        return '; '.join(f'{ns}: {cmd}' for ns, cmd in self.commands(action, args))

    def apply(self, action, args):
        """Run one action on its nodes in turn; t0 and t1 span them, each on its node's clock."""
        rec = dict(action=action, **args, command=self.command(action, args))
        parts = []
        for ns, cmd in self.commands(action, args):
            rc, out = self.node_exec(ns, faults.timed(cmd))
            try:
                parts.append(json.loads(out.strip().splitlines()[-1]))
            except (ValueError, IndexError):
                parts.append(dict(rc=rc, t0=time.time(), t1=time.time()))
        rec.update(t0=min(p['t0'] for p in parts), t1=max(p['t1'] for p in parts),
                   rc=max(abs(p.get('rc') or 0) for p in parts))
        if action == 'outage_off':
            rec['drops'] = self.outage_drops()
        return rec

    def counters(self):
        """Both nodes' counters; outage drops summed."""
        res = {}
        for ns in ('src', 'dst'):
            rc, out = self.node_exec(ns, f'nft -j list table inet {TABLE}')
            if rc:
                continue
            for name, c in parse_counters(out).items():
                if name in res:
                    c = dict(packets=res[name]['packets'] + c['packets'], bytes=res[name]['bytes'] + c['bytes'])
                res[name] = c
        return res

    def qdisc_stats(self):
        out = []
        for ns in ('dst', 'src'):
            dev = self.paths.ifb(ns)
            rc, text = self.node_exec(ns, f'tc -s -j qdisc show dev {dev}')
            if rc == 0:
                out += [dict(q, dev=q.get('dev') or dev) for q in parse_qdiscs(text)]
        return out

    def clear(self):
        for ns in ('src', 'dst'):
            self.node_exec(ns, f'nft delete table inet {TABLE} 2>/dev/null; '
                               f'tc qdisc del dev {self.paths.ifb(ns)} root 2>/dev/null; true')

    def leftovers(self):
        out = []
        for ns in ('src', 'dst'):
            _, text = self.node_exec(ns, f"tc qdisc show dev {self.paths.ifb(ns)} 2>/dev/null | grep -E 'netem|tbf|fq_codel 30:'; "
                                         f'nft list table inet {TABLE} 2>/dev/null | head -1; true')
            out += [f'{ns}: {ln}' for ln in text.splitlines() if ln.strip()]
        return out
