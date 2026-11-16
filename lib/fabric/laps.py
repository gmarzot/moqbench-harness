"""LAPS (Quicr): command line, lapsAdmin control and state.

LAPS takes its peers on the command line (--peer, one per link it dials) and
has no peer-metric flag. Control is JSON-RPC over MoQ through lapsAdmin, which
runs in a sidecar container in the relay's network namespace; every call is
MoQ traffic that floods the mesh, so callers keep them sparse. LAPS clients
speak MoQT draft 18 only. The client listener binds 127.0.0.1 unless -b is
given, and moq-peering writes a sequence file beside the certificate.
"""
import json
import shlex
import uuid

from fabric import topology
from fabric.adapter import Adapter

PEERING_PORT = 33434
CLIENT_PORT = 33435
CERT_DIR = '/certs'
ADMIN_BIN = '/ws/build/src/lapsAdmin'


def admin_image(image):
    """The builder image beside a runtime one: laps:TAG -> laps:builder-TAG."""
    repo, _, tag = image.rpartition(':')
    return f'{repo}:builder-{tag}' if repo else f'{image}:builder'


class Laps(Adapter):
    name = 'laps'
    caps = frozenset({'peer.add', 'peer.remove', 'peer.restart', 'peer.metric'})
    client_port = CLIENT_PORT
    client_path = ''
    drafts = (18,)

    def command(self, node, ctx):
        fab = ctx['fabric']
        peers = topology.dial_lists(ctx['links'], fab.order, ctx.get('dialers', ()))[node]
        argv = ['/usr/local/bin/lapsRelay', '-b', '0.0.0.0', '-p', str(CLIENT_PORT), '-e', node,
                '--peer_port', str(PEERING_PORT)]
        for peer in peers:
            argv += ['--peer', f'{topology.link_address(ctx["addressing"], node, peer, peer)}:{PEERING_PORT}']
        if ctx['roles'].get(node, 'edge') != 'edge':
            argv += ['--node_type', ctx['roles'][node]]
        return argv + ['-c', f'{CERT_DIR}/relay.pem', '-k', f'{CERT_DIR}/relay.key']

    def render(self, node, ctx):
        return {'command.json': json.dumps(self.command(node, ctx)) + '\n'}

    def service(self, node, ctx):
        return {
            'image': ctx['images'][node],
            'command': self.command(node, ctx),
            'volumes': [f'./nodes/{node}/certs:{CERT_DIR}'],
            # LAPS ignores SIGTERM.
            'stop_grace_period': '2s',
        }

    def sidecar(self, node, ctx):
        image = ctx.get('admin_images', {}).get(node) or admin_image(ctx['images'][node])
        return {'image': image, 'entrypoint': ['sleep'], 'command': ['infinity'], 'init': True}

    def call(self, fab, node, *methods):
        """One lapsAdmin call: {t0, t1, status (200 ok, 0 failed), body (the JSON document)}."""
        argv = ['--uri', f'moq://127.0.0.1:{CLIENT_PORT}', '--endpoint-id', f'mb-admin-{uuid.uuid4().hex[:8]}',
                *methods]
        # lapsAdmin on PATH (an admin image), else where the builder stage leaves it.
        cmd = f'"$(command -v lapsAdmin || echo {ADMIN_BIN})" ' + ' '.join(shlex.quote(a) for a in argv)
        r = fab.sidecar_exec(node, cmd)
        try:
            doc = json.loads(r['out'])
            ok = r['rc'] == 0 and 'results' in doc
        except ValueError:
            doc, ok = None, False
        return {'t0': r['t0'], 't1': r['t1'], 'status': 200 if ok else 0,
                'body': json.dumps(doc) if doc is not None else r['out']}

    def ready(self, fab, node):
        return fab.listening(node, CLIENT_PORT)

    def peer_state(self, fab, node):
        r = self.call(fab, node, 'peers')
        if r['status'] != 200:
            return None
        return peer_rows(json.loads(r['body']), fab.names_by_address())

    def state(self, fab, node):
        r = self.call(fab, node, 'state', 'peers', 'publish_namespaces', 'publishes')
        return json.loads(r['body']) if r['status'] == 200 else None

    def namespaces(self, fab, node):
        doc = self.state(fab, node)
        return namespaces_in(doc) if doc else None

    def session(self, fab, node, peer):
        """(host, port) of node's session with peer: the dialed port, or the peer's port if it dialed in."""
        host = fab.link_address(node, peer)
        rows = self.peer_state(fab, node) or []
        for r in rows:
            if r['host'] == host and r['status'] == 'connected':
                return host, r['port']
        return host, PEERING_PORT

    def peer_add(self, fab, node, peer, metric=None):
        target = f'{fab.link_address(node, peer)}:{PEERING_PORT}' + (f':{metric}' if metric is not None else '')
        return self.call(fab, node, 'add-peer', target)

    def peer_remove(self, fab, node, peer):
        host, port = self.session(fab, node, peer)
        return self.call(fab, node, 'remove-peer', f'{host}:{port}')

    def peer_restart(self, fab, node, peer):
        host, port = self.session(fab, node, peer)
        return self.call(fab, node, 'restart-peer', f'{host}:{port}')

    def peer_metric(self, fab, node, peer, metric):
        host, port = self.session(fab, node, peer)
        return self.call(fab, node, 'set-peer-metric', f'{host}:{port}:{metric}')


def peer_rows(doc, names=None):
    """Sessions from lapsAdmin peers, in the same shape as moqx's /peering rows."""
    names = names or {}
    peers = (((doc or {}).get('results') or {}).get('peers') or {}).get('peers', [])
    rows = []
    for p in peers:
        m = p.get('metrics') or {}
        rows.append(dict(peer=names.get(p.get('host'), p.get('contact') or p.get('host')), host=p.get('host'),
                         port=p.get('port'), origin=p.get('role'), status=p.get('status'),
                         identified=bool(p.get('contact')), srtt_us=m.get('srtt_us'), metric=p.get('metric'),
                         node_metric=p.get('node_metric'), connect_time_us=m.get('connect_time_us'),
                         subscribe_tracks=m.get('subscribe_tracks'), publishes=m.get('publishes'),
                         active_streams=m.get('active_streams')))
    return rows


def namespaces_in(doc):
    """Every track namespace a lapsAdmin document names, '/'-joined."""
    found = set()

    def walk(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in ('track_namespace', 'namespace') and value:
                    found.add('/'.join(map(str, value)) if isinstance(value, list) else str(value).strip('/'))
                else:
                    walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)
    walk((doc or {}).get('results'))
    return found
