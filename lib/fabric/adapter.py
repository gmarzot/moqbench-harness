"""The relay-neutral fabric adapter: what the harness needs from a relay to run it as a fabric.

An adapter renders each node's configuration and the relay's container, changes
peers at runtime and reads peer and route state through the fabric's control
handle (fab), which runs commands beside each relay. A verb the relay cannot
perform raises Unsupported, which a scenario reports as SKIP.
"""


class Unsupported(Exception):
    """The relay has no way to do this."""


class Adapter:
    name = ''
    # Verbs this relay performs at runtime: peer.add, peer.remove, peer.restart, peer.metric.
    caps = frozenset()
    client_port = 4433
    client_path = '/'
    # MoQT drafts its clients may use; empty means any.
    drafts = ()
    # Peer state can be read often without disturbing the fabric.
    cheap_state = False

    def render(self, node, ctx):
        """{relative path: text} for one relay node under nodes/<node>/."""
        raise NotImplementedError

    def service(self, node, ctx):
        """Compose fields for the relay container: image, volumes, environment, user."""
        raise NotImplementedError

    def sidecar(self, node, ctx):
        """Compose fields for a container beside the relay that runs its control client, or None."""
        return None

    def ready(self, fab, node):
        """True once the relay answers its control interface."""
        raise NotImplementedError

    def peer_state(self, fab, node):
        """[{peer, host, status, srtt_us, metric, subscribe_tracks, publishes, active_streams, ...}]."""
        raise Unsupported(f'{self.name}: peer state')

    def namespaces(self, fab, node):
        """Namespaces the relay knows, as '/'-joined strings."""
        raise Unsupported(f'{self.name}: namespace state')

    def state(self, fab, node):
        """The relay's raw state document, for the record."""
        raise Unsupported(f'{self.name}: state')

    def metrics(self, fab, node):
        raise Unsupported(f'{self.name}: metrics')

    def peer_add(self, fab, node, peer, metric=None):
        raise Unsupported(f'{self.name}: peer.add')

    def peer_remove(self, fab, node, peer):
        raise Unsupported(f'{self.name}: peer.remove')

    def peer_restart(self, fab, node, peer):
        raise Unsupported(f'{self.name}: peer.restart')

    def peer_metric(self, fab, node, peer, metric):
        raise Unsupported(f'{self.name}: peer.metric')

    def shared_dirs(self, node):
        """{folder under nodes/<node>/: path} mounted in both the relay and its holder."""
        return {}

    def poll_urls(self, node, fab=None):
        """{name: url} a poller beside the relay may fetch: name is peering or state."""
        return {}

    def sample(self, doc, namespace, track):
        """One polled peering document read for the run's track, with the keys it can tell:
        tx_bytes (cumulative bytes sent to peers), streams (data streams open on peer sessions),
        subscribed (a fabric subscribe held for the track), own_subscribe (one for local viewers)."""
        return {}


def get(name):
    if name == 'moqx-peering':
        from fabric.moqx_peering import MoqxPeering
        return MoqxPeering()
    if name == 'laps':
        from fabric.laps import Laps
        return Laps()
    if name == 'aiomoqt-relay':
        from fabric.aiomoqt_relay import AiomoqtRelay
        return AiomoqtRelay()
    raise Unsupported(f'no fabric adapter named {name!r}')
