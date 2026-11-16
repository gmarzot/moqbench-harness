"""The contribution path: src --vA-- wan --vB-- dst, three network namespaces on one clock.

Plain veth pairs join the namespaces; there is no bridge, NAT or conntrack. wan routes between the
two subnets and holds every impairment: the forward profile on its egress toward dst (vb0), the
reverse profile on its egress toward src (va0). With dual_path a second, independent pair of links
(va1/vb1) gives a second path. Each endpoint has a dummy interface for its local multicast groups:
the source fan-out in src, the receiver outputs in dst. Offloads are off on every veth so each
datagram meets the emulator as its own packet.
"""
import ipaddress

OFFLOADS = 'gso off gro off tso off tx-udp-segmentation off'
MCAST = '239.0.0.0/8'
GROUP_OUT = '239.79.0.2'
PORTS_OUT = {'out': 6000, 'second': 6001, 'raw': 6002}


class Paths:
    """Interfaces and addresses of a contribution topology under a /16."""

    def __init__(self, subnet, dual=False):
        base = int(ipaddress.ip_network(subnet).network_address)
        self.dual = dual
        self.links = []
        for k in range(2 if dual else 1):
            a = base + (2 * k + 1) * 256
            b = base + (2 * k + 2) * 256
            self.links.append(dict(
                k=k, src_if=f'wa{k}', wan_a=f'va{k}', wan_b=f'vb{k}', dst_if=f'wb{k}',
                src_ip=str(ipaddress.ip_address(a + 10)), wan_a_ip=str(ipaddress.ip_address(a + 1)),
                wan_b_ip=str(ipaddress.ip_address(b + 1)), dst_ip=str(ipaddress.ip_address(b + 10)),
                a_net=f'{ipaddress.ip_address(a)}/24', b_net=f'{ipaddress.ip_address(b)}/24'))

    @property
    def fwd_dev(self):
        return [ln['wan_b'] for ln in self.links]

    @property
    def rev_dev(self):
        return [ln['wan_a'] for ln in self.links]

    def src_ip(self, k=0):
        return self.links[k]['src_ip']

    def dst_ip(self, k=0):
        return self.links[k]['dst_ip']

    def veth_cmds(self, pids):
        """Host-namespace commands (pids by namespace) that create every veth into its namespace."""
        out = []
        for ln in self.links:
            out.append(f"ip link add {ln['wan_a']} netns {pids['wan']} type veth peer name {ln['src_if']} netns {pids['src']}")
            out.append(f"ip link add {ln['wan_b']} netns {pids['wan']} type veth peer name {ln['dst_if']} netns {pids['dst']}")
        return out

    def ns_cmds(self):
        """{namespace: commands run inside it} that address, route and tune the links."""
        src, wan, dst = [], [], []
        for ln in self.links:
            for cmds, dev, ip in ((src, ln['src_if'], ln['src_ip']), (wan, ln['wan_a'], ln['wan_a_ip']),
                                  (wan, ln['wan_b'], ln['wan_b_ip']), (dst, ln['dst_if'], ln['dst_ip'])):
                cmds += [f'ip addr add {ip}/24 dev {dev}', f'ip link set {dev} mtu 1500 up',
                         f'ethtool -K {dev} {OFFLOADS} >/dev/null 2>&1 || true']
            src.append(f"ip route add {ln['b_net']} via {ln['wan_a_ip']}")
            dst.append(f"ip route add {ln['a_net']} via {ln['wan_b_ip']}")
        for cmds in (src, dst):
            cmds += ['ip link add mc0 type dummy', 'ip link set mc0 multicast on up',
                     'ip addr add 169.254.79.1/32 dev mc0', f'ip route add {MCAST} dev mc0']
        return dict(src=src, wan=wan, dst=dst)

    def rx_devs(self):
        """{namespace: veths} whose receive queues get RPS, so a flow keeps its order whichever CPU sends."""
        return dict(src=[ln['src_if'] for ln in self.links], wan=self.fwd_dev + self.rev_dev,
                    dst=[ln['dst_if'] for ln in self.links])

    def relay_addresses(self, placement):
        """(namespace the relay runs in, address the publisher dials, address the subscriber dials)."""
        if placement == 'last-mile':
            return 'src', '127.0.0.1', self.src_ip()
        return 'dst', self.dst_ip(), '127.0.0.1'


def vm_render(ifaces):
    """Shell per VM for a two-VM path that impairs each direction on its receiver's ingress through an
    ifb, so no emulator VM is needed. Rendered only; VM runs are not enabled."""
    lines = {'src': [], 'dst': []}
    for role in ('dst', 'src'):
        nic = ifaces[role]
        lines[role] += [f'sudo ethtool -K {nic} gro off lro off {OFFLOADS} || true',
                        'sudo modprobe ifb numifbs=1', 'sudo ip link set ifb0 up',
                        f'sudo tc qdisc replace dev {nic} handle ffff: ingress',
                        f'sudo tc filter replace dev {nic} parent ffff: protocol ip prio 10 u32 match ip protocol 17 0xff '
                        'action mirred egress redirect dev ifb0']
    return lines
