"""The contribution path: src --vA-- wan --vB-- dst, three network namespaces on one clock.

Plain veth pairs join the namespaces; there is no bridge, NAT or conntrack. wan routes between the
two subnets and holds every impairment: the forward profile on its egress toward dst (vb0), the
reverse profile on its egress toward src (va0). With dual_path a second, independent pair of links
(va1/vb1) gives a second path. Each endpoint has a dummy interface for its local multicast groups:
the source fan-out in src, the receiver outputs in dst. Offloads are off on every veth so each
datagram meets the emulator as its own packet.

VmPaths is the same interface for two VMs: the real path between their addresses is the WAN, each
direction impaired on its receiver's ingress through an ifb, and the multicast groups looped back on
a dummy interface on each VM.
"""
import ipaddress

OFFLOADS = 'gso off gro off tso off tx-udp-segmentation off'
MCAST = '239.0.0.0/8'
GROUP_OUT = '239.79.0.2'
PORTS_OUT = {'out': 6000, 'second': 6001, 'raw': 6002}


class Paths:
    """Interfaces and addresses of a contribution topology under a /16."""
    wall_clock = False

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
        """{namespace: interfaces} whose receive queues get RPS, so a flow keeps its order whichever CPU
        sends: the veths, and the dummy that loops the local multicast groups back."""
        return dict(src=[ln['src_if'] for ln in self.links] + ['mc0'], wan=self.fwd_dev + self.rev_dev,
                    dst=[ln['dst_if'] for ln in self.links] + ['mc0'])

    def relay_addresses(self, placement):
        """(namespace the relay runs in, address the publisher dials, address the subscriber dials)."""
        if placement == 'last-mile':
            return 'src', '127.0.0.1', self.src_ip()
        return 'dst', self.dst_ip(), '127.0.0.1'

    def qdisc_samplers(self):
        """[(namespace, sampler name, [(side, device)])] for the 100 ms qdisc samplers."""
        return [('wan', 'qdisc-wan', [('fwd', self.fwd_dev[0]), ('rev', self.rev_dev[0])])]


FWD_IFB, REV_IFB = 'mbcf0', 'mbcr0'
MC_DEV = 'mbcmc0'
IFB_PREF = 300
VM_OFFLOADS = 'gro off lro off ' + OFFLOADS
VM_OFFLOADS_ON = 'gro on gso on tso on'


class VmPaths:
    """Two VMs: src and dst addresses, the interface each reaches the other on (nic(ns), resolved on
    first use), the forward ifb on dst and the reverse ifb on src."""
    wall_clock = True
    dual = False
    fwd_dev = [FWD_IFB]
    rev_dev = [REV_IFB]

    def __init__(self, src_addr, dst_addr, nic):
        self.addr = {'src': src_addr, 'dst': dst_addr}
        self.nic = nic

    @property
    def links(self):
        """The one path, keyed as Paths.links: captures on src's interface and on dst's ifb (after
        the forward tree), the trees on the ifbs."""
        return [dict(k=0, src_if=self.nic('src'), dst_if=FWD_IFB, wan_a=REV_IFB, wan_b=FWD_IFB,
                     src_ip=self.addr['src'], dst_ip=self.addr['dst'])]

    def src_ip(self, k=0):
        return self.addr['src']

    def dst_ip(self, k=0):
        return self.addr['dst']

    @staticmethod
    def peer(ns):
        return 'dst' if ns == 'src' else 'src'

    @staticmethod
    def ifb(ns):
        """The ifb on a node: its ingress from the peer, so dst holds the forward direction."""
        return FWD_IFB if ns == 'dst' else REV_IFB

    @staticmethod
    def node_of(dev):
        return 'dst' if dev == FWD_IFB else 'src'

    def setup_cmds(self, ns):
        """Commands on a node (host network namespace) that loop multicast back locally and send
        everything from the peer through the node's ifb."""
        nic, ifb, peer = self.nic(ns), self.ifb(ns), self.addr[self.peer(ns)]
        return [f'ethtool -K {nic} {VM_OFFLOADS} >/dev/null 2>&1 || true',
                f'ip link add {MC_DEV} type dummy 2>/dev/null || true', f'ip link set {MC_DEV} multicast on up',
                f'ip addr replace 169.254.79.1/32 dev {MC_DEV}', f'ip route replace {MCAST} dev {MC_DEV}',
                f'ip link add {ifb} type ifb 2>/dev/null || true', f'ip link set {ifb} up',
                f'tc qdisc add dev {nic} clsact 2>/dev/null || true',
                f'tc filter del dev {nic} ingress pref {IFB_PREF} 2>/dev/null || true',
                f'tc filter add dev {nic} ingress pref {IFB_PREF} protocol ip flower src_ip {peer} '
                f'action mirred egress redirect dev {ifb}']

    def teardown_cmds(self, nic):
        """Commands that remove everything setup_cmds and the emulator leave on a node."""
        return ['nft delete table inet mbc 2>/dev/null', f'tc filter del dev {nic} ingress pref {IFB_PREF} 2>/dev/null',
                f'ip link del {FWD_IFB} 2>/dev/null', f'ip link del {REV_IFB} 2>/dev/null',
                f'ip link del {MC_DEV} 2>/dev/null', f'ethtool -K {nic} {VM_OFFLOADS_ON} >/dev/null 2>&1']

    def leftover_cmd(self, nic):
        """Shell that prints what setup_cmds or the emulator left on a node, one item per line."""
        return (f"ip -o link show {FWD_IFB} 2>/dev/null | cut -d: -f2; ip -o link show {REV_IFB} 2>/dev/null | cut -d: -f2; "
                f"ip -o link show {MC_DEV} 2>/dev/null | cut -d: -f2; "
                f"tc filter show dev {nic} ingress pref {IFB_PREF} 2>/dev/null | grep -m1 mirred; "
                f"for d in {FWD_IFB} {REV_IFB}; do tc qdisc show dev $d 2>/dev/null | grep -E 'netem|tbf|fq_codel 30:'; done; "
                "nft list table inet mbc 2>/dev/null | head -1; true")

    def relay_addresses(self, placement):
        if placement == 'last-mile':
            return 'src', '127.0.0.1', self.src_ip()
        return 'dst', self.dst_ip(), '127.0.0.1'

    def qdisc_samplers(self):
        return [('dst', 'qdisc-wan', [('fwd', FWD_IFB)]), ('src', 'qdisc-rev', [('rev', REV_IFB)])]


