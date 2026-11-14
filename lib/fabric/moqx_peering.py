"""moqx with moq-peering: per-node config, admin API peer verbs and state.

Every listener offers drafts 16 and 18, the cache is on, and the admin server
listens on the node's loopback only. Peering listens on UDP 33434; each link is
dialed by its earlier node, at the listener's address on their link network.
"""
import json

from fabric import topology
from fabric.adapter import Adapter

PEERING_PORT = 33434
ADMIN = 'http://127.0.0.1:8000'
CERT_DIR = '/etc/moqx/certs'


class MoqxPeering(Adapter):
    name = 'moqx-peering'
    caps = frozenset({'peer.add', 'peer.remove', 'peer.restart', 'peer.metric'})
    client_port = 4433
    client_path = '/moq-relay'

    def config(self, node, ctx):
        fab = ctx['fabric']
        addr = ctx['addressing']
        peers = [{'host': topology.link_address(addr, node, peer, peer), 'port': PEERING_PORT}
                 for peer in topology.dial_lists(ctx['links'], fab.order)[node]]
        return {
            'relay_id': node,
            'threads': fab.threads,
            'use_local_forwarders': True,
            'listeners': {'relay': {
                'quic_stack': 'mvfst',
                'moqt_versions': [16, 18],
                'udp': {'socket': {'address': '0.0.0.0', 'port': self.client_port}},
                'tls': {'cert_file': f'{CERT_DIR}/relay.pem', 'key_file': f'{CERT_DIR}/relay.key', 'insecure': False},
                'endpoint': self.client_path,
            }},
            'services': {'default': {
                'match': [{'authority': {'any': True}, 'path': {'exact': self.client_path}},
                          {'authority': {'any': True}, 'path': {'prefix': '/'}}],
                'cache': {'enabled': True, 'max_tracks': 1000, 'max_groups_per_track': 100},
                'max_subscription_buffer_kb': 51200,
            }},
            'admin': {'address': '127.0.0.1', 'port': 8000, 'plaintext': True, 'track_metrics_enabled': True},
            'peering': {'method': 'moq-peering', 'moq_peering': {
                'node_id': node,
                'node_type': ctx['roles'].get(node, 'edge'),
                'node_metric': 100,
                'listen_port': PEERING_PORT,
                'tls': {'cert_file': f'{CERT_DIR}/relay.pem', 'key_file': f'{CERT_DIR}/relay.key'},
                'peers': peers,
                'check_interval_ms': fab.check_interval_ms,
            }},
        }

    def render(self, node, ctx):
        # JSON is YAML: no YAML library needed to write or test it.
        return {'config.yaml': json.dumps(self.config(node, ctx), indent=2) + '\n'}

    def service(self, node, ctx):
        return {
            'image': ctx['fabric'].image,
            'volumes': [f'./nodes/{node}/config.yaml:/etc/moqx/config.yaml:ro', f'./nodes/{node}/certs:{CERT_DIR}'],
            'environment': {'MOQX_JEMALLOC': 'auto'},
        }

    def poll_urls(self, node):
        return {'peering': f'{ADMIN}/peering', 'state': f'{ADMIN}/state'}

    def ready(self, fab, node):
        r = fab.admin(node, 'GET', '/peering')
        return r['status'] == 200

    def peer_state(self, fab, node):
        r = fab.admin(node, 'GET', '/peering')
        if r['status'] != 200:
            return None
        return peer_rows(json.loads(r['body']), fab.names_by_address())

    def state(self, fab, node):
        r = fab.admin(node, 'GET', '/state')
        return json.loads(r['body']) if r['status'] == 200 else None

    def namespaces(self, fab, node):
        doc = self.state(fab, node)
        return namespaces_in(doc) if doc else None

    def metrics(self, fab, node):
        r = fab.admin(node, 'GET', '/metrics')
        return r['body'] if r['status'] == 200 else None

    def _post(self, fab, node, verb, peer, metric=None):
        body = {'host': fab.link_address(node, peer), 'port': PEERING_PORT}
        if metric is not None:
            body['metric'] = metric
        return fab.admin(node, 'POST', f'/peering/{verb}', body)

    def peer_add(self, fab, node, peer, metric=None):
        return self._post(fab, node, 'add-peer', peer, metric)

    def peer_remove(self, fab, node, peer):
        return self._post(fab, node, 'remove-peer', peer)

    def peer_restart(self, fab, node, peer):
        return self._post(fab, node, 'restart-peer', peer)

    def peer_metric(self, fab, node, peer, metric):
        return self._post(fab, node, 'set-peer-metric', peer, metric)


def peer_rows(doc, names=None):
    """Sessions from GET /peering, each named by the fabric node that holds its address."""
    names = names or {}
    rows = []
    for p in (doc or {}).get('peers', []):
        rows.append(dict(peer=names.get(p.get('host'), p.get('contact') or p.get('host')),
                         host=p.get('host'), port=p.get('port'), origin=p.get('origin'), status=p.get('status'),
                         identified=p.get('identified'), srtt_us=p.get('srtt_us'), metric=p.get('metric'),
                         node_metric=p.get('node_metric'), connect_time_us=p.get('connect_time_us'),
                         subscribe_tracks=p.get('subscribe_tracks'), publishes=p.get('publishes'),
                         active_streams=p.get('active_streams')))
    return rows


def namespaces_in(doc):
    """Namespaces registered in a /state document's namespace trees (not merely cached), '/'-joined."""
    found = set()

    def walk(node):
        if not isinstance(node, dict):
            return
        ns = node.get('full_namespace')
        if ns and (node.get('publisher') or node.get('routes')):
            found.add('/'.join(map(str, ns)))
        for child in (node.get('children') or {}).values():
            walk(child)
    for svc in ((doc or {}).get('services') or {}).values():
        walk(svc.get('namespace_tree'))
    return found
