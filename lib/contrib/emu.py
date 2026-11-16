"""The WAN emulator in the wan namespace: qdisc trees per direction, nft accounting and outages.

Accounting counters see every packet entering wan, before any qdisc, per direction. An outage adds
the direction's ingress interface to an nft set that one counted drop rule matches, so outage drops
are counted apart from netem's. The trees are built once per run; events change them in place.
"""
import json
import time

from fabric import faults

TABLE = 'mbc'
CHAIN = 'mbc_forward'


def nft_script(paths):
    rules = []
    for ln in paths.links:
        rules.append(f'    iifname "{ln["wan_a"]}" oifname "{ln["wan_b"]}" counter comment "fwd{ln["k"]}"')
        rules.append(f'    iifname "{ln["wan_b"]}" oifname "{ln["wan_a"]}" counter comment "rev{ln["k"]}"')
    rules.append('    iifname @blocked counter drop comment "outage"')
    return '\n'.join([f'table inet {TABLE} {{', '  set blocked { type ifname; }',
                      f'  chain {CHAIN} {{ type filter hook forward priority -10; policy accept;',
                      *rules, '  }', '}']) + '\n'


class Emulator:
    """Impairment control through wan_exec(command) -> (rc, stdout)."""

    def __init__(self, paths, wan_exec, mux_bps):
        self.paths, self.exec, self.mux_bps = paths, wan_exec, mux_bps
        self.profile = None
        self.seeds = (None, None)

    def sh(self, cmd):
        rc, out = self.exec(cmd)
        if rc:
            raise RuntimeError(f'wan: {cmd}: exit {rc}: {out[-500:]}')
        return out

    def build(self, profile, repeat=0):
        """Every direction's tree, and the nft table with zeroed counters."""
        self.profile = profile
        self.seeds = profile.seeds(repeat)
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
        k = (self.paths.fwd_dev + self.paths.rev_dev).index(dev) % len(self.paths.links)
        return (self.seeds[0] if dev in self.paths.fwd_dev else self.seeds[1]) + 500 * k

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

    def run_timeline(self, actions, start, log_path, stop=lambda: False):
        """Apply (t_s, action, args) at start + t_s (time.monotonic); every record goes to log_path."""
        done = []
        with open(log_path, 'a') as log:
            for t_s, action, args in actions:
                while time.monotonic() < start + t_s:
                    if stop():
                        return done
                    time.sleep(min(0.01, start + t_s - time.monotonic()))
                rec = self.apply(action, args)
                rec['at_s'] = t_s
                done.append(rec)
                log.write(json.dumps(rec) + '\n')
                log.flush()
        return done

    def counters(self):
        """{comment: {packets, bytes}} from the nft accounting chain."""
        rc, out = self.exec(f'nft -j list chain inet {TABLE} {CHAIN}')
        if rc:
            return {}
        res = {}
        for item in json.loads(out).get('nftables', []):
            rule = item.get('rule')
            if not rule:
                continue
            counter = next((e['counter'] for e in rule.get('expr', []) if 'counter' in e), None)
            if counter is not None:
                res[rule.get('comment', '')] = dict(packets=counter['packets'], bytes=counter['bytes'])
        return res

    def outage_drops(self):
        return self.counters().get('outage', {}).get('packets')

    def qdisc_stats(self):
        """[{dev, kind, handle, packets, bytes, drops, overlimits}] for every qdisc in wan."""
        rc, out = self.exec('tc -s -j qdisc show')
        if rc:
            return []
        return [dict(dev=q.get('dev'), kind=q.get('kind'), handle=q.get('handle'), parent=q.get('parent'),
                     packets=q.get('packets'), bytes=q.get('bytes'), drops=q.get('drops'),
                     overlimits=q.get('overlimits'), options=q.get('options'))
                for q in json.loads(out) if q.get('kind') in ('netem', 'tbf')]

    def clear(self):
        cmds = [f'nft delete table inet {TABLE} 2>/dev/null']
        cmds += [f'tc qdisc del dev {d} root 2>/dev/null' for d in self.paths.fwd_dev + self.paths.rev_dev]
        self.exec('; '.join(cmds) + '; true')

    def leftovers(self):
        rc, out = self.exec(f"tc qdisc show | grep -E 'netem|tbf'; nft list table inet {TABLE} 2>/dev/null | head -1; true")
        return [ln for ln in out.splitlines() if ln.strip()]
