"""aiomoqt-relay as a moq-fabric node: command line, control socket, peer verbs and state.

The relay serves raw QUIC clients on 4433 and moq-fabric on UDP 33434, with
transit on. Its control API is HTTP on a unix socket in a folder shared with
the holder, so the harness and the pollers reach it from the holder and keep
reaching it across a relay restart. A track left with no local viewer is
withdrawn from the fabric after PAUSE_AFTER_S. The image is the peer image:
the lab image plus aiomoqt from `peer_pip`.
"""
import json
import urllib.parse

from fabric import topology
from fabric.adapter import Adapter, Unsupported

PEERING_PORT = 33434
CLIENT_PORT = 4433
CERT_DIR = '/certs'
RUN_DIR = '/fabric/run'
CONTROL = f'{RUN_DIR}/control.sock'
PAUSE_AFTER_S = 5


class AiomoqtRelay(Adapter):
    name = 'aiomoqt-relay'
    caps = frozenset({'peer.add', 'peer.remove', 'peer.restart', 'peer.metric'})
    client_port = CLIENT_PORT
    client_path = ''
    cheap_state = True

    def command(self, node, ctx):
        fab = ctx['fabric']
        argv = ['aiomoqt-relay', '--quic', '--bind', '0.0.0.0', '--port', str(CLIENT_PORT),
                '--cert', f'{CERT_DIR}/relay.pem', '--key', f'{CERT_DIR}/relay.key', '--control', CONTROL,
                '--pause-after', str(PAUSE_AFTER_S), '--node-id', node, '--node-metric', str(fab.node_metric(node)),
                '--peer-port', str(PEERING_PORT)]
        for peer in topology.dial_lists(ctx['links'], fab.order, ctx.get('dialers', ()))[node]:
            argv += ['--peer', f'{topology.link_address(ctx["addressing"], node, peer, peer)}:{PEERING_PORT}']
        return argv

    def render(self, node, ctx):
        return {'command.json': json.dumps(self.command(node, ctx)) + '\n'}

    def service(self, node, ctx):
        return {
            'image': ctx['images'][node],
            'command': self.command(node, ctx),
            'volumes': [f'./nodes/{node}/certs:{CERT_DIR}'],
            'environment': {'HOME': '/tmp', 'PYTHONUNBUFFERED': '1'},
            'init': True,
        }

    def shared_dirs(self, node):
        return {'run': RUN_DIR}

    @staticmethod
    def base(fab, node):
        """The control socket's URL where the harness and the pollers run: the holder, or the relay's VM."""
        folder = fab.shared_dir(node, 'run', RUN_DIR) if fab is not None else RUN_DIR
        return 'http+unix://' + urllib.parse.quote(f'{folder}/control.sock', safe='')

    def poll_urls(self, node, fab=None):
        return {'peering': f'{self.base(fab, node)}/moq-fabric'}

    def call(self, fab, node, method, path):
        return fab.admin(node, method, path, base=self.base(fab, node))

    def fabric(self, fab, node):
        r = self.call(fab, node, 'GET', '/moq-fabric')
        return json.loads(r['body']) if r['status'] == 200 else None

    def ready(self, fab, node):
        return self.call(fab, node, 'GET', '/moq-fabric')['status'] == 200

    def peer_state(self, fab, node):
        doc = self.fabric(fab, node)
        return None if doc is None else peer_rows(doc, fab.names_by_address())

    def state(self, fab, node):
        return self.fabric(fab, node)

    def namespaces(self, fab, node):
        doc = self.fabric(fab, node)
        return namespaces_in(doc) if doc is not None else None

    def sample(self, doc, namespace, track):
        return sample(doc, namespace, track)

    def session(self, fab, node, peer):
        """(host, port) of node's session with peer: the port it reports, else the peering port."""
        host = fab.link_address(node, peer)
        rows = [r for r in self.peer_state(fab, node) or [] if r['host'] == host]
        rows.sort(key=lambda r: r['status'] != 'connected')
        return host, (rows[0]['port'] if rows and rows[0].get('port') else PEERING_PORT)

    def _peer(self, fab, node, op, host, port, metric=None):
        query = {'op': op, 'host': host, 'port': port}
        if metric is not None:
            query['metric'] = metric
        return self.call(fab, node, 'POST', '/moq-fabric/peer?' + urllib.parse.urlencode(query))

    def peer_add(self, fab, node, peer, metric=None):
        return self._peer(fab, node, 'add', fab.link_address(node, peer), PEERING_PORT, metric)

    def peer_remove(self, fab, node, peer):
        return self._peer(fab, node, 'remove', *self.session(fab, node, peer))

    def peer_restart(self, fab, node, peer):
        r = self._peer(fab, node, 'restart', *self.session(fab, node, peer))
        if r['status'] == 400:
            raise Unsupported(f'{self.name}: peer.restart (the relay refused op=restart: {r["body"][:120]})')
        return r

    def peer_metric(self, fab, node, peer, metric):
        return self._peer(fab, node, 'metric', *self.session(fab, node, peer), metric)


def peer_rows(doc, names=None):
    """Sessions from GET /moq-fabric, in the shape of moqx's /peering rows plus the data counters."""
    names = names or {}
    rows = []
    for p in (doc or {}).get('peers', []):
        rows.append(dict(peer=names.get(p.get('host'), p.get('contact') or p.get('host')),
                         host=p.get('host'), port=p.get('port'), origin=p.get('origin'), status=p.get('status'),
                         identified=p.get('identified'), srtt_us=p.get('srtt_us'), metric=p.get('metric'),
                         node_metric=p.get('node_metric'), connect_time_us=p.get('connect_time_us'),
                         subscribe_tracks=p.get('subscribe_tracks'), publishes=p.get('publishes'),
                         active_streams=p.get('active_streams'), remote_node_id=p.get('remote_node_id'),
                         **{k: p.get(k) for k in ('tx_streams', 'rx_streams', 'tx_bytes', 'rx_bytes',
                                                  'tx_datagrams', 'rx_datagrams')}))
    return rows


def namespaces_in(doc):
    """Namespaces the relay learned over the fabric or holds a track in, '/'-joined."""
    found = {r['namespace'].strip('/') for r in (doc or {}).get('announces', []) if r.get('namespace')}
    return found | {r['namespace'].strip('/') for r in (doc or {}).get('tracks', []) if r.get('namespace')}


def _is(row, namespace, track):
    return (row.get('namespace') or '').strip('/') == namespace and row.get('name') == track


def sample(doc, namespace, track):
    """One /fabric document read for the run's track: bytes sent to peers (cumulative), whether the
    relay holds a fabric subscribe for it (its own or one it forwards), and its own subscribe."""
    doc = doc or {}
    own = any(_is(t, namespace, track) and t.get('fabric') == 'subscribed' for t in doc.get('tracks', []))
    return {'tx_bytes': sum(p.get('tx_bytes') or 0 for p in doc.get('peers', [])),
            'subscribed': own or any(_is(s, namespace, track) for s in doc.get('subscribes', [])),
            'own_subscribe': own}
