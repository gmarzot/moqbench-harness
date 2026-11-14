"""Topology shapes, link lists, dial direction, hop counts and addressing for a relay fabric.

A topology is a set of undirected links between relay nodes named in `order`.
Each link is dialed from one side only: the node earlier in `order` dials.
"""
from collections import deque
import ipaddress

SHAPES = {'line', 'star', 'ring', 'full', 'custom'}


class TopologyError(ValueError):
    """A topology that cannot be built, and why."""


def links(shape, order, hub=None, custom=()):
    """Links of a shape over the nodes in order, each as (dialer, listener)."""
    order = list(order)
    if len(order) < 2:
        raise TopologyError('a fabric needs at least two relays')
    if len(set(order)) != len(order):
        raise TopologyError(f'order repeats a node: {order}')
    if shape not in SHAPES:
        raise TopologyError(f'topology must be one of {", ".join(sorted(SHAPES))}, not {shape!r}')
    if shape == 'line':
        pairs = list(zip(order, order[1:]))
    elif shape == 'ring':
        if len(order) < 3:
            raise TopologyError('a ring needs at least three relays')
        pairs = list(zip(order, order[1:])) + [(order[-1], order[0])]
    elif shape == 'star':
        hub = hub or order[0]
        if hub not in order:
            raise TopologyError(f'hub {hub!r} is not in order')
        pairs = [(hub, n) for n in order if n != hub]
    elif shape == 'full':
        pairs = [(a, b) for i, a in enumerate(order) for b in order[i + 1:]]
    else:
        pairs = [tuple(p) for p in custom]
        if not pairs:
            raise TopologyError('a custom topology needs links')
    return normalize(pairs, order)


def normalize(pairs, order):
    """Pairs as (dialer, listener), sorted by position in order, without duplicates."""
    pos = {n: i for i, n in enumerate(order)}
    out = set()
    for pair in pairs:
        if len(pair) != 2 or pair[0] == pair[1]:
            raise TopologyError(f'a link joins two different nodes, not {pair!r}')
        for n in pair:
            if n not in pos:
                raise TopologyError(f'link {pair!r} names {n!r}, which is not in order')
        a, b = sorted(pair, key=pos.get)
        out.add((a, b))
    return sorted(out, key=lambda p: (pos[p[0]], pos[p[1]]))


def dial_lists(link_list, order):
    """The peers each node dials: every link's earlier node dials the later one."""
    out = {n: [] for n in order}
    for a, b in normalize(link_list, order):
        out[a].append(b)
    return out


def hops(link_list, source):
    """Hop count from source to every node it can reach."""
    adj = {}
    for a, b in link_list:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    dist, queue = {source: 0}, deque([source])
    while queue:
        n = queue.popleft()
        for m in sorted(adj.get(n, ())):
            if m not in dist:
                dist[m] = dist[n] + 1
                queue.append(m)
    return dist


def connected(link_list, order):
    return set(hops(link_list, order[0])) == set(order)


def has_cycle(link_list, order):
    parent = {n: n for n in order}

    def root(n):
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n
    for a, b in normalize(link_list, order):
        ra, rb = root(a), root(b)
        if ra == rb:
            return True
        parent[ra] = rb
    return False


def resolve(name, order):
    """A node by name, or by role: ingest (first), egress (last), transit (second of three or more)."""
    if name in order:
        return name
    if name == 'ingest':
        return order[0]
    if name == 'egress':
        return order[-1]
    if name == 'transit' and len(order) >= 3:
        return order[1]
    raise TopologyError(f'{name!r} is neither a node nor ingest, egress or transit')


def addressing(order, clients, subnet='10.77.0.0/16', wire=None):
    """Static addresses: one access network for clients and relays, one /24 per wired relay pair.

    wire is the set of pairs that get a link network, by default every pair, so a
    link can be added at runtime. Returns {'access': {...}, 'links': {(a, b): {...}}}.
    """
    net = ipaddress.ip_network(subnet)
    if net.prefixlen > 16:
        raise TopologyError(f'subnet {subnet} must be a /16 or larger')
    base = int(net.network_address)
    access = {'subnet': f'{ipaddress.ip_address(base)}/24', 'hosts': {}}
    for i, c in enumerate(clients):
        access['hosts'][c] = str(ipaddress.ip_address(base + 10 + i))
    for i, r in enumerate(order):
        access['hosts'][r] = str(ipaddress.ip_address(base + 101 + i))
    pairs = normalize(wire if wire is not None else links('full', order), order)
    if len(pairs) > 250:
        raise TopologyError('too many link networks for one /16')
    out = {}
    for k, (a, b) in enumerate(pairs, 1):
        third = base + k * 256
        out[(a, b)] = {'name': f'l-{a}-{b}', 'subnet': f'{ipaddress.ip_address(third)}/24',
                       a: str(ipaddress.ip_address(third + 11)), b: str(ipaddress.ip_address(third + 12))}
    return {'access': access, 'links': out}


def link_address(addr, a, b, of):
    """The address `of` (a or b) holds on the link network joining a and b."""
    for pair, link in addr['links'].items():
        if set(pair) == {a, b}:
            return link[of]
    raise TopologyError(f'no link network joins {a} and {b}')
