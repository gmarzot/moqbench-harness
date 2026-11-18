"""Network profiles for contribution runs: per-direction netem and tbf trees, and timed events.

A profile is profiles/contrib/<ID>.toml. Each direction (forward = sender to receiver, reverse) is a
fixed qdisc tree built once per run on the emulator's egress interface for that direction:

  root 1: netem  delay, slot jitter, rate (keeps jitter FIFO), loss, seed
  10:     netem  delay 1ms reorder P%          (only with reorder_pct)
  20:     tbf    rate = cap x stream L2 rate   (only with cap)
  30:     fq_codel                             (only with cap_aqm = "fq_codel"; replaces tbf's FIFO)

Events change the tree in place (`tc qdisc change`, restating every netem parameter) or add nft drop
rules for outages; nothing replaces a root qdisc during a run. A cross event runs an iperf3 flow from
src to dst through the same tree, so it competes with the lane for the bottleneck and its buffer.
"""
from dataclasses import dataclass, field

import config

# Holds the longest delay line at the highest tier with retransmissions: 40 Mb/s x 450 ms is ~2,000.
NETEM_LIMIT = 100000
ORDER_RATE = '1gbit'
L2_OVERHEAD = 14 + 20 + 8
DATAGRAM = 1316
SIDE_KEYS = {'delay_ms', 'slot_ms', 'rate', 'loss', 'reorder_pct', 'cap', 'cap_mbit', 'cap_latency_ms',
             'cap_burst', 'cap_aqm', 'seed', 'limit'}
AQMS = ('fifo', 'fq_codel')
CROSS_PORT, CROSS_MAX = 5201, 8
PROFILE_KEYS = {'id', 'name', 'description', 'basis', 'rtt_ms', 'run_s', 'base', 'forward', 'reverse', 'event'}
EVENT_KINDS = {'outage': {'at_s', 'every_s', 'for_ms', 'direction', 'label'},
               'step': {'at_s', 'every_s', 'delta_ms', 'for_s', 'direction', 'label'},
               'rate': {'at_s', 'every_s', 'factor', 'for_s', 'label'},
               'cross': {'at_s', 'every_s', 'for_s', 'proto', 'load', 'streams', 'cc', 'label'}}
DIRECTIONS = {'fwd', 'rev', 'both'}


def ge_mean(p, r, bad_loss=100.0, good_loss=0.0):
    """Stationary loss in percent of netem `loss gemodel p r 1-h 1-k` (all in percent)."""
    pb = p / (p + r) if p + r else 0.0
    return (1 - pb) * good_loss + pb * bad_loss


def ge_for(mean_pct, burst_pkts):
    """Simple Gilbert parameters (p, r, 100, 0) in percent for a mean loss and mean burst length."""
    r = 1.0 / burst_pkts
    loss = mean_pct / 100.0
    return (round(100 * loss * r / (1 - loss), 6), round(100 * r, 6), 100.0, 0.0)


def l2_bps(mux_bps):
    """Wire rate (L2, Ethernet framing) of a TS stream in 1316-byte UDP datagrams."""
    return mux_bps * (DATAGRAM + L2_OVERHEAD) / DATAGRAM


@dataclass
class Side:
    delay_ms: float = 0.0
    slot_ms: tuple = None
    rate: str = ORDER_RATE
    loss: dict = None
    reorder_pct: float = 0.0
    cap: float = None
    cap_mbit: float = None
    cap_latency_ms: float = 60.0
    cap_burst: str = '64kb'
    cap_aqm: str = 'fifo'
    seed: int = None
    limit: int = NETEM_LIMIT

    def mean_loss_pct(self):
        if not self.loss:
            return 0.0
        if 'gemodel' in self.loss:
            return ge_mean(*self.loss['gemodel'])
        return float(self.loss.get('random', 0.0))

    def cap_bps(self, mux_bps):
        if self.cap_mbit:
            return self.cap_mbit * 1e6
        if self.cap:
            return self.cap * l2_bps(mux_bps)
        return None

    def netem_args(self, delay_ms=None, seed=None):
        """Every netem parameter of the root, so a `change` restates the whole set."""
        d = self.delay_ms if delay_ms is None else delay_ms
        parts = [f'limit {self.limit}', f'delay {fmt(d)}ms']
        if self.slot_ms:
            parts.append(f'slot {fmt(self.slot_ms[0])}ms {fmt(self.slot_ms[1])}ms')
        parts.append(f'rate {self.rate}')
        if self.loss:
            if 'gemodel' in self.loss:
                parts.append('loss gemodel ' + ' '.join(f'{fmt(v)}%' for v in self.loss['gemodel']))
            elif self.loss.get('random'):
                parts.append(f"loss random {fmt(self.loss['random'])}%")
        s = self.seed if seed is None else seed
        if s is not None:
            parts.append(f'seed {int(s)}')
        return ' '.join(parts)

    def tbf_args(self, mux_bps, factor=1.0):
        bps = self.cap_bps(mux_bps)
        return f'rate {int(bps * factor)}bit burst {self.cap_burst} latency {fmt(self.cap_latency_ms)}ms'

    def tbf_parent(self):
        return '10:1' if self.reorder_pct else '1:1'

    def build(self, dev, mux_bps, seed=None):
        """Commands that build this side's tree on dev from nothing."""
        cmds = [f'tc qdisc del dev {dev} root 2>/dev/null; true',
                f'tc qdisc add dev {dev} root handle 1: netem {self.netem_args(seed=seed)}']
        if self.reorder_pct:
            cmds.append(f'tc qdisc add dev {dev} parent 1:1 handle 10: netem limit {self.limit} delay 1ms '
                        f'reorder {fmt(self.reorder_pct)}%')
        if self.cap_bps(mux_bps):
            cmds.append(f'tc qdisc add dev {dev} parent {self.tbf_parent()} handle 20: tbf {self.tbf_args(mux_bps)}')
            if self.cap_aqm == 'fq_codel':
                cmds.append(f'tc qdisc add dev {dev} parent 20:1 handle 30: fq_codel')
        return cmds

    def change_delay(self, dev, delay_ms, seed=None):
        return f'tc qdisc change dev {dev} root handle 1: netem {self.netem_args(delay_ms, seed)}'

    def change_rate(self, dev, mux_bps, factor):
        return f'tc qdisc change dev {dev} parent {self.tbf_parent()} handle 20: tbf {self.tbf_args(mux_bps, factor)}'


def fmt(v):
    v = float(v)
    return str(int(v)) if v == int(v) else f'{v:g}'


@dataclass
class Profile:
    id: str
    name: str
    rtt_ms: float
    forward: Side
    reverse: Side
    events: list = field(default_factory=list)
    run_s: float = 0.0
    description: str = ''
    basis: str = ''

    def seeds(self, repeat):
        """(forward, reverse) seeds for a repeat: every lane of a repeat sees the same loss pattern."""
        f = self.forward.seed if self.forward.seed is not None else 1
        r = self.reverse.seed if self.reverse.seed is not None else 2
        return f + 10 * repeat, r + 10 * repeat

    def timeline(self, seconds, start_s=0.0):
        """Event actions within [start_s, start_s + seconds), sorted: (t_s, action, args)."""
        out = []
        for i, ev in enumerate(self.events):
            t = ev['at_s']
            every = ev.get('every_s')
            k = 0
            while t < seconds:
                label = ev.get('label') or f"{ev['kind']}-{i + 1}"
                name = f'{label}#{k + 1}' if every else label
                at = start_s + t
                if ev['kind'] == 'outage':
                    d = ev.get('direction', 'both')
                    out.append((at, 'outage_on', dict(name=name, direction=d)))
                    out.append((at + ev['for_ms'] / 1000, 'outage_off', dict(name=name, direction=d)))
                elif ev['kind'] == 'step':
                    d = ev.get('direction', 'both')
                    out.append((at, 'delay', dict(name=name, direction=d, delta_ms=ev['delta_ms'])))
                    out.append((at + ev['for_s'], 'delay', dict(name=name, direction=d, delta_ms=0)))
                elif ev['kind'] == 'rate':
                    out.append((at, 'rate', dict(name=name, factor=ev['factor'])))
                    out.append((at + ev['for_s'], 'rate', dict(name=name, factor=1.0)))
                else:
                    port = CROSS_PORT + self.cross_events().index(i)
                    flow = dict(proto=ev.get('proto', 'tcp'), load=ev.get('load'), streams=ev.get('streams', 1),
                                cc=ev.get('cc'), for_s=ev['for_s'])
                    out.append((at, 'cross_on', dict(name=name, port=port, **flow)))
                    out.append((at + ev['for_s'], 'cross_off', dict(name=name, port=port)))
                if not every:
                    break
                t += every
                k += 1
        return sorted(out, key=lambda x: x[0])

    def cross_events(self):
        """Indices of the cross events; the k-th gets iperf3 port CROSS_PORT + k."""
        return [i for i, ev in enumerate(self.events) if ev['kind'] == 'cross']

    def summary(self, mux_bps):
        cap = self.forward.cap_bps(mux_bps)
        return dict(id=self.id, name=self.name, rtt_ms=self.rtt_ms,
                    fwd_loss_pct=round(self.forward.mean_loss_pct(), 4),
                    rev_loss_pct=round(self.reverse.mean_loss_pct(), 4),
                    fwd_netem=self.forward.netem_args(), rev_netem=self.reverse.netem_args(),
                    cap_mbit=round(cap / 1e6, 3) if cap else None,
                    cap_latency_ms=self.forward.cap_latency_ms if cap else None,
                    cap_aqm=self.forward.cap_aqm if cap else None, events=len(self.events),
                    cross=len(self.cross_events()))


def side_from(where, data, default_delay):
    config.only_keys(where, data, SIDE_KEYS)
    s = Side(delay_ms=float(data.get('delay_ms', default_delay)))
    if 'slot_ms' in data:
        slot = data['slot_ms']
        if not (isinstance(slot, list) and len(slot) == 2 and 0 <= slot[0] <= slot[1]):
            raise config.ConfigError(f'{where}: slot_ms is [min, max] in ms')
        s.slot_ms = tuple(slot)
    s.rate = data.get('rate', ORDER_RATE)
    loss = data.get('loss')
    if loss is not None:
        if 'gemodel' in loss:
            ge = loss['gemodel']
            if not (isinstance(ge, list) and 1 <= len(ge) <= 4 and all(0 <= v <= 100 for v in ge)):
                raise config.ConfigError(f'{where}: loss.gemodel is [p, r, 1-h, 1-k] in percent')
            full = list(ge) + [None] * (4 - len(ge))
            p = full[0]
            r = full[1] if full[1] is not None else 100 - p
            s.loss = {'gemodel': [p, r, full[2] if full[2] is not None else 100.0, full[3] or 0.0]}
        elif 'random' in loss:
            s.loss = {'random': float(loss['random'])}
        else:
            raise config.ConfigError(f'{where}: loss is {{ gemodel = [...] }} or {{ random = PCT }}')
    for key in ('reorder_pct', 'cap', 'cap_mbit', 'cap_latency_ms', 'limit'):
        if key in data:
            setattr(s, key, data[key])
    s.cap_burst = data.get('cap_burst', s.cap_burst)
    s.cap_aqm = data.get('cap_aqm', s.cap_aqm)
    if s.cap_aqm not in AQMS:
        raise config.ConfigError(f'{where}: cap_aqm is one of {", ".join(AQMS)}')
    if s.cap_aqm != 'fifo' and not s.cap_bps(1e6):
        raise config.ConfigError(f'{where}: cap_aqm needs cap or cap_mbit')
    s.seed = data.get('seed')
    return s


def parse(data, where, base=None):
    config.only_keys(where, data, PROFILE_KEYS)
    rtt = float(data.get('rtt_ms', base.rtt_ms if base else 0))
    half = rtt / 2
    fwd = data.get('forward')
    rev = data.get('reverse')
    p = Profile(id=data.get('id') or (base.id if base else ''), name=data.get('name', base.name if base else ''),
                rtt_ms=rtt,
                forward=side_from(f'{where}: forward', fwd, half) if fwd is not None else (base.forward if base else Side(half)),
                reverse=side_from(f'{where}: reverse', rev, half) if rev is not None else (base.reverse if base else Side(half)),
                events=list(base.events) if base and 'event' not in data else [],
                run_s=float(data.get('run_s', base.run_s if base else 0)),
                description=data.get('description', ''), basis=data.get('basis', ''))
    if not p.id:
        raise config.ConfigError(f'{where}: a profile needs an id')
    for i, ev in enumerate(data.get('event', []), 1):
        at = f'{where}: event {i}'
        kind = ev.get('kind')
        if kind not in EVENT_KINDS:
            raise config.ConfigError(f'{at}: kind is one of {", ".join(sorted(EVENT_KINDS))}')
        config.only_keys(at, ev, EVENT_KINDS[kind] | {'kind'})
        if not isinstance(ev.get('at_s'), (int, float)) or ev['at_s'] < 0:
            raise config.ConfigError(f'{at}: at_s is seconds from the start of measurement')
        if ev.get('direction', 'both') not in DIRECTIONS:
            raise config.ConfigError(f'{at}: direction is fwd, rev or both')
        need = {'outage': 'for_ms', 'step': 'delta_ms', 'rate': 'factor', 'cross': 'for_s'}[kind]
        if need not in ev:
            raise config.ConfigError(f'{at}: a {kind} event needs {need}')
        if kind != 'outage' and 'for_s' not in ev:
            raise config.ConfigError(f'{at}: a {kind} event needs for_s')
        if kind == 'rate' and not p.forward.cap_bps(1e6):
            raise config.ConfigError(f'{at}: a rate event needs a forward cap')
        if kind == 'cross':
            if ev.get('proto', 'tcp') not in ('tcp', 'udp'):
                raise config.ConfigError(f'{at}: proto is tcp or udp')
            if ev.get('proto') == 'udp' and not (isinstance(ev.get('load'), (int, float)) and ev['load'] > 0):
                raise config.ConfigError(f'{at}: a udp cross event needs load, its rate as a multiple of the stream\'s')
            if ev.get('proto', 'tcp') == 'tcp' and 'load' in ev:
                raise config.ConfigError(f'{at}: a tcp cross event takes what its controller gets; load is for udp')
            if not (isinstance(ev.get('streams', 1), int) and ev.get('streams', 1) >= 1):
                raise config.ConfigError(f'{at}: streams is a positive integer')
        p.events.append(dict(ev))
    if len(p.cross_events()) > CROSS_MAX:
        raise config.ConfigError(f'{where}: at most {CROSS_MAX} cross events')
    return p


def load(name, root=config.ROOT):
    """profiles/contrib/<name>.toml over the config folders; `base` names a profile it extends."""
    path = config.need('profiles', [f'contrib/{name}.toml'], root)
    data = config.read_toml(path)
    base = load(data['base'], root) if data.get('base') else None
    return parse(data, config.shown(path, root), base)


def names(root=config.ROOT):
    out = set()
    for folder in config.config_dirs(root):
        out.update(p.stem for p in (folder / 'profiles' / 'contrib').glob('*.toml'))
    return sorted(out)
