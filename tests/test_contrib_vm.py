"""Contribution runs on two VMs: spec, placement, rendered commands, emulator and clocks, without ssh."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
import config  # noqa: E402
from contrib import calibrate, cli, explain, lanes, profiles, report, run, sources, spec, topology  # noqa: E402
from contrib import vm as contrib_vm  # noqa: E402
from contrib.docker import DockerContrib  # noqa: E402
from contrib.lanes import Proc  # noqa: E402

SRC, DST = '192.0.2.10', '198.51.100.20'
CHRONY = '''Reference ID    : C0000201 (192.0.2.1)
Stratum         : 3
Ref time (UTC)  : Thu Nov 19 10:00:00 2026
System time     : 0.000412000 seconds slow of NTP time
Last offset     : -0.000021000 seconds
RMS offset      : 0.000034000 seconds
Frequency       : 1.234 ppm fast
Leap status     : Normal
'''
PING = '5 packets transmitted, 5 received, 0% packet loss\nrtt min/avg/max/mdev = 31.002/31.250/31.900/0.300 ms\n'


def vm_data(**over):
    nodes = [dict(name='src', host='user@src-vm', address=SRC, cpus='0-3'),
             dict(name='dst', host='user@dst-vm', address=DST)]
    data = dict(provider='oci', contribution=dict(stack_dir='mbc'), nodes=nodes)
    data.update(over)
    return data


class FakeHost:
    def __init__(self, target, home, nic, nft=None):
        self.target, self.local = target, False
        self.home, self.nic, self.nft = home, nic, nft
        self.cmds, self.inputs, self.pulled, self.pushed = [], [], [], []

    def sh(self, script, check=True, timeout=300, input=None):
        self.cmds.append(script)
        if input is not None:
            self.inputs.append(input)
        out = ''
        if script == 'printf %s "$HOME"':
            out = self.home
        elif script.startswith('ip -o route get'):
            out = f'{script.split()[-1]} via 10.0.0.1 dev {self.nic} src 10.0.0.5 uid 1000'
        elif script == 'id -u; id -g':
            out = '1001\n1002\n'
        elif script.startswith('chronyc'):
            out = CHRONY
        elif 'ping' in script:
            out = PING + ('PING-OK\n' if 'PING-OK' in script else '')
        elif 'nft -j list table' in script and self.nft:
            out = json.dumps(self.nft)
        return SimpleNamespace(stdout=out, stderr='', returncode=0)

    def push(self, local, remote):
        self.pushed.append(remote)

    def pull(self, remote, local):
        self.pulled.append((remote, local))


def nft_doc(*rules):
    return {'nftables': [{'rule': {'comment': c, 'expr': [{'counter': {'packets': p, 'bytes': b}}]}} for c, p, b in rules]}


def provider(src_nft=None, dst_nft=None):
    dep = spec.Deployment('vmx', vm_data(), 'vmx')
    prov = contrib_vm.VmContrib(dep)
    prov.nodes['src'].host = FakeHost('user@src-vm', '/home/s', 'ens3', src_nft)
    prov.nodes['dst'].host = FakeHost('user@dst-vm', '/home/d', 'ens5', dst_nft)
    return prov


def cmds(prov, ns):
    return prov.nodes[ns].host.cmds


class Spec(unittest.TestCase):
    def test_a_vm_deployment_names_hosts_and_addresses(self):
        dep = spec.Deployment('vmx', vm_data(), 'vmx')
        self.assertEqual(dep.nodes['src']['address'], SRC)
        self.assertEqual(dep.cpus('src'), '0-3')
        self.assertIsInstance(cli.provider(dep), contrib_vm.VmContrib)
        docker = spec.Deployment('d', dict(provider='docker', host='local', contribution={}), 'd')
        self.assertIsInstance(cli.provider(docker), DockerContrib)

    def test_rejects(self):
        base = vm_data()['nodes']
        cases = [[base[0]], base + [dict(name='wan')], [dict(base[0], address=''), base[1]],
                 [dict(base[0], address='src-vm.example'), base[1]], [base[0], dict(base[1], host='')],
                 [base[0], dict(base[1], address=SRC)]]
        for nodes in cases:
            with self.subTest(nodes=nodes), self.assertRaises(config.ConfigError):
                spec.Deployment('x', vm_data(nodes=nodes), 'x')


class Placement(unittest.TestCase):
    def test_procs_run_on_their_node_with_host_networking(self):
        prov = provider()
        run_dir = prov.run_dir('001-srt', '/local')
        self.assertEqual(run_dir, '/home/s/mbc/vmx/runs/001-srt')
        self.assertIn('mkdir -p /home/d/mbc/vmx/runs/001-srt', '\n'.join(cmds(prov, 'dst')))
        bin_dir = prov.bin_dir()
        self.assertEqual(prov.nodes['dst'].host.pushed, ['/home/d/mbc/vmx/bin'])
        tx = Proc('srt-snd', 'src', 'tools', 'srt-live-transmit a b')
        rx = Proc('aiomoqt-sub', 'dst', 'aiomoqt', 'sub', env=dict(AIOPQUIC_QLOG_DIR='/out/qlog-aiomoqt-sub'))
        n_tx = prov.start(tx, run_dir, bin_dir, '001-srt-snd', sources_dir='/home/s/mbc/vmx/sources')
        n_rx = prov.start(rx, run_dir, bin_dir, '001-aiomoqt-sub', sources_dir='/home/s/mbc/vmx/sources')
        src_run = cmds(prov, 'src')[-1]
        dst_run = cmds(prov, 'dst')[-1]
        self.assertTrue(src_run.startswith('docker run -d --init'))
        for part in ('--network host', '-v /home/s/mbc/vmx/runs/001-srt:/out', '-v /home/s/mbc/vmx/sources:/sources:ro',
                     '--cpuset-cpus 0-3', '--user 1001:1002', '--label org.moqbench.contrib=vmx', 'moqbench-contrib-tools:dev'):
            self.assertIn(part, src_run)
        for part in ('--network host', '-v /home/d/mbc/vmx/runs/001-srt:/out', '-v /home/d/mbc/vmx/bin:/contrib/bin:ro'):
            self.assertIn(part, dst_run)
        self.assertNotIn('/sources', dst_run)
        self.assertNotIn('--cpuset-cpus', dst_run)
        self.assertIn('mkdir -p -m 777 /home/d/mbc/vmx/runs/001-srt/qlog-aiomoqt-sub', cmds(prov, 'dst'))
        prov.stop([n_tx, n_rx])
        self.assertTrue(any(c.startswith(f'docker kill -s TERM {n_rx}') for c in cmds(prov, 'dst')))
        self.assertFalse(any(n_rx in c for c in cmds(prov, 'src') if c.startswith('docker kill')))
        prov.logs(n_rx)
        self.assertIn(f'docker logs -t {n_rx} 2>&1', cmds(prov, 'dst'))
        prov.collect(run_dir, '/local/001-srt')
        self.assertEqual(prov.nodes['src'].host.pulled, [('/home/s/mbc/vmx/runs/001-srt', '/local/001-srt')])
        self.assertEqual(prov.nodes['dst'].host.pulled, [('/home/d/mbc/vmx/runs/001-srt', '/local/001-srt')])
        with self.assertRaises(contrib_vm.FabricError):
            prov.start(Proc('qdisc', 'wan', 'tools', 'x'), run_dir, bin_dir, 'x')

    def test_lane_files_reach_both_nodes(self):
        prov = provider()
        prov.write('/home/s/mbc/vmx/runs/r', 'srt.json', '{}\n')
        self.assertIn('cat > /home/s/mbc/vmx/runs/r/srt.json', cmds(prov, 'src'))
        self.assertIn('cat > /home/d/mbc/vmx/runs/r/srt.json', cmds(prov, 'dst'))

    def test_lanes_dial_the_peer_address(self):
        prov = provider()
        c = lanes.Ctx(paths=prov.paths, placement='last-mile', tier=sources.TIERS['mpeg2-20m'], rtt_ms=0,
                      variant=dict(D=200, relay_cc='bbr2'), playout_ms=[300])
        srt = {p.name: p for p in lanes.LANES['srt'].procs(c)}
        self.assertIn(f'srt://{DST}:9000?mode=caller', srt['srt-snd'].cmd)
        moq = {p.name: p for p in lanes.LANES['moqt-moq2ts'].procs(c)}
        self.assertEqual(moq['relay'].ns, 'src')
        self.assertIn('moqt://127.0.0.1:4433/moq-relay', moq['moq2ts-pub'].cmd)
        self.assertIn(f'moqt://{SRC}:4433/moq-relay', moq['moq2ts-rcv'].cmd)

    def test_pending(self):
        prov = provider()
        test = SimpleNamespace(dual_path=False)
        self.assertIsNone(prov.pending(lanes.LANES['srt'], test, {}))
        self.assertIn('frame-log', prov.pending(lanes.LANES['moqt-aiomoqt-loc'], test, {}))
        self.assertIsNone(prov.pending(lanes.LANES['moqt-aiomoqt-loc'], test, {'frame_log': True}))
        self.assertIn('docker', prov.pending(lanes.LANES['moqt-aiomoqt-dual'], test, {}))
        self.assertIn('docker', prov.pending(lanes.LANES['srt'], SimpleNamespace(dual_path=True), {}))


class UpDown(unittest.TestCase):
    def test_up_sets_up_each_node_in_its_holder(self):
        prov = provider()
        result = prov.up()
        for ns, nic, ifb, peer in (('src', 'ens3', 'mbcr0', DST), ('dst', 'ens5', 'mbcf0', SRC)):
            text = '\n'.join(cmds(prov, ns))
            self.assertIn('sudo -n modprobe ifb numifbs=0', text)
            self.assertIn(f'docker run -d --name mbc-vmx-{ns} --label org.moqbench.contrib=vmx --network host '
                          '--cap-add NET_ADMIN', text)
            self.assertIn(f'tc filter add dev {nic} ingress pref 300 protocol ip flower src_ip {peer} '
                          f'action mirred egress redirect dev {ifb}', text)
            self.assertIn('ip route replace 239.0.0.0/8 dev mbcmc0', text)
            self.assertIn(f'ethtool -K {nic} gro off lro off gso off', text)
            self.assertIn('chronyc -n tracking', text)
            self.assertEqual(result['nodes'][ns]['nic'], nic)
            self.assertEqual(result['nodes'][ns]['ping']['avg'], 31.25)
            self.assertTrue(result['nodes'][ns]['chrony']['synced'])
        self.assertIn('--cpuset-cpus 0-3', '\n'.join(cmds(prov, 'src')))

    def test_down_removes_the_network_setup(self):
        prov = provider()
        prov.down()
        for ns, nic in (('src', 'ens3'), ('dst', 'ens5')):
            text = '\n'.join(cmds(prov, ns))
            self.assertIn('docker ps -aq --filter label=org.moqbench.contrib=vmx', text)
            for part in ('docker run --rm --network host', 'nft delete table inet mbc',
                         f'tc filter del dev {nic} ingress pref 300', 'ip link del mbcf0', 'ip link del mbcr0',
                         'ip link del mbcmc0', f'ethtool -K {nic} gro on gso on tso on'):
                self.assertIn(part, text)

    def test_unsynchronised_chrony_stops_up(self):
        prov = provider()
        host = prov.nodes['dst'].host
        orig = host.sh

        def sh(script, check=True, timeout=300, input=None):
            r = orig(script, check, timeout, input)
            if script.startswith('chronyc'):
                r.stdout = CHRONY.replace('Normal', 'Not synchronised')
            return r
        host.sh = sh
        with self.assertRaises(contrib_vm.FabricError):
            prov.up()

    def test_render_vm(self):
        dep = spec.Deployment('vmx', vm_data(), 'vmx')
        out = cli.render_vm(dep)
        self.assertTrue(any(f'flower src_ip {SRC} action mirred egress redirect dev mbcf0' in c for c in out['dst']))
        self.assertTrue(any(f'flower src_ip {DST} action mirred egress redirect dev mbcr0' in c for c in out['src']))

    def test_host_info_per_node(self):
        prov = provider()
        info = calibrate.host_info(prov)
        self.assertEqual(set(info), {'src', 'dst'})
        self.assertIn('cat /proc/sys/net/core/rmem_max 2>/dev/null', cmds(prov, 'dst'))


class Emulator(unittest.TestCase):
    def test_trees_land_on_each_receiver(self):
        prov = provider()
        e = prov.emulator(20e6)
        p = profiles.load('cellular-handover')
        e.build(p, repeat=1)
        src, dst = '\n'.join(cmds(prov, 'src')), '\n'.join(cmds(prov, 'dst'))
        self.assertIn('tc qdisc add dev mbcf0 root handle 1: netem', dst)
        self.assertIn(f'seed {p.forward.seed + 10}', dst)
        self.assertIn('tc qdisc add dev mbcr0 root handle 1: netem', src)
        self.assertNotIn('mbcf0', src)
        self.assertIn('nft -f -', src)
        on = e.commands('outage_on', dict(direction='fwd'))
        self.assertEqual(on, [('dst', f'nft add element inet mbc blocked {{ {SRC} }}')])
        both = e.commands('outage_off', dict(direction='both'))
        self.assertEqual(both, [('dst', f'nft delete element inet mbc blocked {{ {SRC} }}'),
                                ('src', f'nft delete element inet mbc blocked {{ {DST} }}')])
        step = e.commands('delay', dict(direction='both', delta_ms=60))
        self.assertEqual([ns for ns, _ in step], ['dst', 'src'])
        self.assertIn('dev mbcf0 root handle 1: netem', step[0][1])
        self.assertEqual(e.commands('rate', dict(factor=0.5))[0][0], 'dst')
        rec = e.apply('outage_on', dict(name='o', direction='both'))
        self.assertEqual(rec['action'], 'outage_on')
        self.assertIn('dst: nft add element', rec['command'])
        e.clear()
        self.assertIn('tc qdisc del dev mbcr0 root', cmds(prov, 'src')[-1])
        self.assertIn('tc qdisc del dev mbcf0 root', cmds(prov, 'dst')[-1])

    def test_nft_counts_each_direction_on_its_sender(self):
        paths = topology.VmPaths(SRC, DST, lambda ns: 'eth0')
        from contrib import emu
        src, dst = emu.vm_nft_script(paths, 'src'), emu.vm_nft_script(paths, 'dst')
        self.assertIn(f'ip daddr {DST} counter comment "fwd0"', src)
        self.assertIn(f'ip daddr {DST} meta l4proto {{ tcp, udp }} th dport 5201-5208 counter comment "cross-fwd0"', src)
        self.assertIn(f'ip daddr {SRC} counter comment "rev0"', dst)
        self.assertIn('th sport 5201-5208 counter comment "cross-rev0"', dst)
        for s in (src, dst):
            self.assertIn('type filter hook output', s)
            self.assertIn('ip saddr @blocked counter drop comment "outage"', s)
            self.assertIn('set blocked { type ipv4_addr; }', s)

    def test_counters_merge_both_nodes(self):
        prov = provider(src_nft=nft_doc(('fwd0', 10, 1000), ('cross-fwd0', 1, 100), ('outage', 2, 20)),
                        dst_nft=nft_doc(('rev0', 5, 500), ('cross-rev0', 0, 0), ('outage', 3, 30)))
        c = prov.emulator(20e6).counters()
        self.assertEqual(c['fwd0'], dict(packets=10, bytes=1000))
        self.assertEqual(c['rev0'], dict(packets=5, bytes=500))
        self.assertEqual(c['outage'], dict(packets=5, bytes=50))
        self.assertEqual(set(c), {'fwd0', 'cross-fwd0', 'rev0', 'cross-rev0', 'outage'})

    def test_report_reads_the_ifb_qdiscs(self):
        paths = topology.VmPaths(SRC, DST, lambda ns: 'ens3')
        qd = [dict(dev='mbcf0', kind='netem', handle='1:', packets=100, drops=4),
              dict(dev='mbcr0', kind='netem', handle='1:', packets=50, drops=1)]
        meta = dict(topology=dict(links=paths.links), emulator_final=dict(qdisc=qd, nft={}))
        out = report.impairment(meta, Path('/nonexistent'))
        self.assertEqual(out['fwd0']['netem_drops'], 4)
        self.assertEqual(out['rev0']['netem_drops'], 1)


class Cells(unittest.TestCase):
    def test_vm_helpers_stamp_wall_clock_on_both_nodes(self):
        prov = provider()
        test = spec.Test(dict(type='contribution', lanes=['srt'], profiles=['cloud-path'], capture='headers'), 't', {})
        cell = run.Cell(prov, test, spec.load_scenario('standard'), lanes.LANES['srt'], profiles.load('cloud-path'),
                        dict(D=200), 0, '001-srt', '/nonexistent')
        procs = {p.name: p for p in cell.helpers()}
        self.assertTrue(cell.wall)
        self.assertIn('--clock real', procs['tap-in'].cmd)
        self.assertIn('--clock real', procs['tap-out'].cmd)
        self.assertEqual(procs['pcap-src0'].cmd.split()[:3], ['tcpdump', '-i', 'ens3'])
        self.assertEqual((procs['pcap-dst0'].ns, procs['pcap-dst0'].cmd.split()[2]), ('dst', 'mbcf0'))
        self.assertEqual(procs['qdisc-wan'].ns, 'dst')
        self.assertIn('--cmd fwd="tc -s -j qdisc show dev mbcf0"', procs['qdisc-wan'].cmd)
        self.assertEqual(procs['qdisc-rev'].ns, 'src')
        self.assertTrue(all(p.ns in ('src', 'dst') for p in procs.values()))

    def test_cell_meta(self):
        prov = provider()
        m = prov.cell_meta()
        self.assertEqual(m['clock']['dst']['offset_ms'], -0.412)
        self.assertEqual(m['base_rtt_ms']['min'], 31.002)
        self.assertTrue(any(f'ping -c 5 -i 0.2 -W 1 {DST}' in c for c in cmds(prov, 'src')))


class Clocks(unittest.TestCase):
    def test_chrony_parse(self):
        c = contrib_vm.chrony(CHRONY)
        self.assertEqual(c, dict(offset_ms=-0.412, last_offset_ms=-0.021, rms_offset_ms=0.034, stratum=3,
                                 leap='Normal', synced=True))
        self.assertFalse(contrib_vm.chrony('')['synced'])
        fast = contrib_vm.chrony(CHRONY.replace('0.000412000 seconds slow', '0.002000000 seconds fast'))
        self.assertEqual(fast['offset_ms'], 2.0)

    def test_clock_sync_marks_offsets_above_1ms(self):
        def meta(*offsets):
            return dict(path=dict(start=dict(clock=dict(src=dict(offset_ms=offsets[0]), dst=dict(offset_ms=offsets[1]))),
                                  end=dict(clock=dict(src=dict(offset_ms=offsets[2]), dst=dict(offset_ms=offsets[3])))))
        self.assertIsNone(report.clock_sync({}))
        ok = report.clock_sync(meta(0.2, -0.4, 0.1, -0.9))
        self.assertFalse(ok['disturbed'])
        self.assertEqual(ok['worst_offset_ms'], dict(src=0.2, dst=0.9))
        self.assertTrue(report.clock_sync(meta(0.2, -0.4, 0.1, -1.2))['disturbed'])
        self.assertTrue(report.clock_sync(meta(0.2, None, 0.1, 0.1))['disturbed'])

    def test_explain_moves_only_qlog_on_wall_clock_taps(self):
        conn = explain.Conn.__new__(explain.Conn)
        conn.ref, conn.sent, conn.lost, conn.metrics, conn.cc_changes = 0, [(10, 1, 1200, [])], [(20, 1)], [(30, 1, 2, 3, 4)], [40]
        conn.initial_t, conn.span = 10, (10, 10)
        conn.shift(1000)
        self.assertEqual((conn.sent[0][0], conn.lost[0][0], conn.metrics[0][0], conn.cc_changes, conn.initial_t, conn.span),
                         (1010, 1020, 1030, [1040], 1010, (1010, 1010)))
        ev = explain.Evidence.__new__(explain.Evidence)
        ev.run, ev.meta, ev.conns, ev.absent = Path('/nonexistent'), dict(wall_clock=True), [], []
        clock = ev.make_clock([])
        self.assertEqual((clock.method, clock.mono(123), clock.epoch(123)), ('wall-clock', 123, 123))


if __name__ == '__main__':
    unittest.main()
