"""Fault verbs and the timeline that applies them during a run.

Verbs are relay-neutral: link.impair / link.down / link.clear act on the
interfaces of one link network (both ends unless `direction` names one sender),
node.* act on a relay's container, and peer.* go through the adapter. Every
applied event records the host-clock times bracketing it, so it can be laid
over the clients' timelines.
"""
import json
import re
import shlex
import time

from fabric import topology
from fabric.adapter import Unsupported

NETEM_LIMIT = 200000
SAFE_SPEC = re.compile(r'^[A-Za-z0-9.% ]+$')
VERBS = {'link.impair', 'link.down', 'link.clear', 'node.kill', 'node.stop', 'node.start', 'node.freeze',
         'node.thaw', 'peer.add', 'peer.remove', 'peer.restart', 'peer.metric'}


class FaultError(ValueError):
    """An event that cannot be applied as written."""


def iface(ip):
    """Shell that prints the interface holding ip in this namespace."""
    return f"ip -o -4 addr show | awk -v ip={shlex.quote(ip)} '{{split($4, a, \"/\"); if (a[1] == ip) print $2}}'"


def netem_cmd(ip, spec):
    if not SAFE_SPEC.match(spec):
        raise FaultError(f'netem spec has characters it does not need: {spec!r}')
    return (f'dev=$({iface(ip)}); [ -n "$dev" ] || {{ echo "no interface holds {ip}" >&2; exit 3; }}; '
            f'tc qdisc replace dev "$dev" root netem {spec} limit {NETEM_LIMIT}')


def clear_cmd(ip):
    return f'dev=$({iface(ip)}); [ -n "$dev" ] || exit 3; tc qdisc del dev "$dev" root 2>/dev/null; true'


CHECK_CMD = "tc qdisc show | grep -E 'netem|tbf' || true"


def node_cmd(docker, verb, container, signal='KILL', grace_s=5):
    """The docker command for a node verb on a relay container."""
    if not re.fullmatch(r'[A-Z0-9]+', str(signal)):
        raise FaultError(f'signal must be a name such as KILL or TERM, not {signal!r}')
    return {'node.kill': f'{docker} kill -s {signal} {container}',
            'node.stop': f'{docker} stop -t {int(grace_s)} {container}',
            'node.start': f'{docker} start {container}',
            'node.freeze': f'{docker} pause {container}',
            'node.thaw': f'{docker} unpause {container}'}[verb]


def ifb_name(direction, index):
    """The ifb device that holds one peer's impairment: mbo<k> sending, mbi<k> receiving."""
    return f'mb{"o" if direction == "send" else "i"}{index}'


def nic(ip):
    """Shell that prints the interface holding ip (the host's private address)."""
    return iface(ip)


def ifb_impair_cmd(own_ip, peer_ip, index, side, spec):
    """Redirect packets to (send) or from (receive) peer_ip through an ifb with netem; the NIC's
    own qdiscs stay as tuned."""
    if not SAFE_SPEC.match(spec):
        raise FaultError(f'netem spec has characters it does not need: {spec!r}')
    dev = ifb_name(side, index)
    hook, match, pref = (('egress', 'dst_ip', 100 + index) if side == 'send' else ('ingress', 'src_ip', 200 + index))
    return (f'IF=$({nic(own_ip)}); [ -n "$IF" ] || {{ echo "no interface holds {own_ip}" >&2; exit 3; }}; '
            f'sudo modprobe ifb numifbs=0 2>/dev/null; sudo ip link add {dev} type ifb 2>/dev/null; '
            f'sudo ip link set {dev} up && sudo tc qdisc replace dev {dev} root netem {spec} limit {NETEM_LIMIT} && '
            f'{{ sudo tc qdisc add dev "$IF" clsact 2>/dev/null; true; }} && '
            f'{{ sudo tc filter del dev "$IF" {hook} pref {pref} 2>/dev/null; true; }} && '
            f'sudo tc filter add dev "$IF" {hook} pref {pref} protocol ip flower {match} {peer_ip} '
            f'action mirred egress redirect dev {dev}')


def ifb_clear_cmd(own_ip, index, side):
    dev = ifb_name(side, index)
    hook, pref = ('egress', 100 + index) if side == 'send' else ('ingress', 200 + index)
    return (f'IF=$({nic(own_ip)}); [ -n "$IF" ] || exit 3; sudo tc filter del dev "$IF" {hook} pref {pref} 2>/dev/null; '
            f'sudo ip link del {dev} 2>/dev/null; true')


IFB_CHECK_CMD = ("ip -o link show type ifb 2>/dev/null | awk -F': ' '$2 ~ /^mb[oi][0-9]+/ {print \"ifb \" $2}'; "
                 "for d in $(ip -o link show | awk -F': ' '{print $2}' | cut -d@ -f1); do "
                 "sudo tc filter show dev $d egress 2>/dev/null | grep -q mirred && echo \"egress filter on $d\"; "
                 "sudo tc filter show dev $d ingress 2>/dev/null | grep -q mirred && echo \"ingress filter on $d\"; "
                 "done; sudo tc qdisc show | grep -E 'netem|tbf' || true")


def timed(script):
    """Wrap a host shell command so it prints its start, end and exit status as JSON."""
    return (f't0=$(date +%s.%N); {{ {script}; }} >/dev/null 2>&1; rc=$?; t1=$(date +%s.%N); '
            'printf \'{"t0": %s, "t1": %s, "rc": %s}\\n\' "$t0" "$t1" "$rc"')


def ends(fab, ev):
    """The link's nodes and the senders of the impaired traffic: both, or only on's or peer's."""
    a = topology.resolve(ev['on'], fab.order)
    b = topology.resolve(ev['peer'], fab.order)
    direction = ev.get('direction', 'both')
    senders = {'both': [a, b], 'on': [a], 'peer': [b]}.get(direction)
    if senders is None:
        raise FaultError(f'direction must be both, on or peer, not {direction!r}')
    if ev.get('side', 'send') not in ('send', 'receive'):
        raise FaultError(f'side must be send or receive, not {ev.get("side")!r}')
    return a, b, senders


def apply(fab, ev):
    """Apply one event; returns the event with its resolved nodes, host times and outcome."""
    verb = ev['verb']
    if verb not in VERBS:
        raise FaultError(f'unknown verb {verb!r} (verbs: {", ".join(sorted(VERBS))})')
    out = dict(ev)
    try:
        if verb.startswith('link.'):
            a, b, senders = ends(fab, ev)
            out.update(on=a, peer=b, ends=[])
            for s in senders:
                other = b if s == a else a
                out['ends'].append(fab.link_action(verb, s, other, ev.get('netem'), ev.get('side', 'send')))
            out.update(t0=out['ends'][0]['t0'], t1=out['ends'][-1]['t1'],
                       ok=all(e['rc'] == 0 for e in out['ends']))
        elif verb.startswith('node.'):
            n = topology.resolve(ev['on'], fab.order)
            out.update(on=n, **fab.node_action(verb, n, ev.get('signal', 'KILL'), ev.get('grace_s', 5)))
            out['ok'] = out['rc'] == 0
        else:
            a = topology.resolve(ev['on'], fab.order)
            b = topology.resolve(ev['peer'], fab.order)
            ad = fab.adapter
            call = {'peer.add': lambda: ad.peer_add(fab, a, b, ev.get('metric')),
                    'peer.remove': lambda: ad.peer_remove(fab, a, b),
                    'peer.restart': lambda: ad.peer_restart(fab, a, b),
                    'peer.metric': lambda: ad.peer_metric(fab, a, b, ev['metric'])}[verb]
            r = call()
            out.update(on=a, peer=b, t0=r['t0'], t1=r['t1'], status=r['status'], response=r['body'][:500],
                       ok=200 <= r['status'] < 300)
    except Unsupported as exc:
        out.update(ok=None, skipped=str(exc))
    return out


def check(fab):
    """{node: leftover lines} for every relay that still has an impairment."""
    return fab.leftovers()


def clear_all(fab):
    return fab.clear_impairments()


def validate(events, order):
    for i, ev in enumerate(events):
        at = f'event {i + 1}'
        if ev.get('verb') not in VERBS:
            raise FaultError(f'{at}: verb must be one of {", ".join(sorted(VERBS))}')
        if not isinstance(ev.get('at_s'), (int, float)) or ev['at_s'] < 0:
            raise FaultError(f'{at}: at_s must be seconds from the run start')
        topology.resolve(ev.get('on', ''), order)
        if ev['verb'].startswith(('link.', 'peer.')):
            topology.resolve(ev.get('peer', ''), order)
        if ev['verb'] == 'link.impair' and not SAFE_SPEC.match(ev.get('netem', '') or '-'):
            raise FaultError(f'{at}: link.impair needs a netem spec such as "delay 20ms loss 1%"')
        if ev['verb'] == 'peer.metric' and not isinstance(ev.get('metric'), int):
            raise FaultError(f'{at}: peer.metric needs an integer metric')


class Timeline:
    """Apply events at their offsets from start (controller monotonic), logging each to a JSONL file."""

    def __init__(self, events, log_path):
        self.events = sorted(events, key=lambda e: e['at_s'])
        self.log_path = log_path

    def run(self, fab, start, stop=lambda: False):
        done = []
        with open(self.log_path, 'a') as log:
            for ev in self.events:
                while time.monotonic() < start + ev['at_s']:
                    if stop():
                        return done
                    time.sleep(min(0.05, start + ev['at_s'] - time.monotonic()))
                rec = apply(fab, ev)
                done.append(rec)
                log.write(json.dumps(rec) + '\n')
                log.flush()
        return done
